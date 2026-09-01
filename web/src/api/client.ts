import type {
  Assistant,
  AssistantDetail,
  ChangeSetPreview,
  ChangeSetRecord,
  LLMProviderCreatePayload,
  LLMProvidersPayload,
  Project,
  ProjectChatSession,
  ProjectChatSessionDetail,
  ProjectDocument,
  TaskEvent,
} from '../types'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const controller = new AbortController()
  const timeout = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS)
  try {
    const response = await fetch(path, { ...init, signal: controller.signal })
    if (!response.ok) {
      let detail = `${response.status} ${response.statusText}`
      try {
        const body = await response.json() as {
          detail?: string | { code?: string; message?: string }
        }
        if (typeof body.detail === 'string') {
          detail = body.detail || detail
        } else if (body.detail && typeof body.detail === 'object' && body.detail.message) {
          // 稳定错误码（stale / already_applied / already_rejected / conflict）取可读消息。
          detail = body.detail.message
        }
      } catch {
        // Non-JSON server errors keep the HTTP status text.
      }
      throw new Error(detail)
    }
    return await response.json() as T
  } catch (cause) {
    if (controller.signal.aborted) throw new Error('请求超时，请稍后重试')
    throw cause
  } finally {
    clearTimeout(timeout)
  }
}

function parseTaskEvent(value: unknown): TaskEvent {
  if (!value || typeof value !== 'object') throw new Error('任务事件格式非法')
  const event = value as Partial<TaskEvent>
  if (typeof event.type !== 'string' || !event.data || typeof event.data !== 'object') {
    throw new Error('任务事件格式非法')
  }
  return event as TaskEvent
}

/** 任务事件流的可恢复订阅句柄：close 后不再重连，终态自动关闭。 */
export interface TaskStream {
  close: () => void
}

const RECONNECT_DELAYS_MS = [500, 1000, 2000, 4000, 8000, 8000]
const TERMINAL_TASK_EVENTS = new Set(['task_done', 'task_failed'])
const FETCH_TIMEOUT_MS = 60_000
const SSE_IDLE_TIMEOUT_MS = 60_000

export const apiClient = {
  listAssistants: () => request<Assistant[]>('/api/assistants'),
  getAssistant: (assistantId: string) => request<AssistantDetail>(`/api/assistants/${encodeURIComponent(assistantId)}`),
  createAssistant: (id: string, name: string, description: string, persona: string) => request<Assistant>('/api/assistants', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id, name, description, persona }),
  }),
  updateAssistant: (assistantId: string, payload: { name: string; description: string; persona: string }) => request<AssistantDetail>(`/api/assistants/${encodeURIComponent(assistantId)}`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }),
  deleteAssistant: (assistantId: string) => request<{ archived_path: string; purged: boolean }>(
    `/api/assistants/${encodeURIComponent(assistantId)}`,
    { method: 'DELETE' },
  ),
  getMemoryProfile: (assistantId: string) => request<{ content: string }>(
    `/api/assistants/${encodeURIComponent(assistantId)}/memory/profile`,
  ),
  replaceMemoryProfile: (assistantId: string, content: string) => request<{ content: string }>(
    `/api/assistants/${encodeURIComponent(assistantId)}/memory/profile`,
    {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content }),
    },
  ),
  listProjects: (assistantId: string) => request<Project[]>(`/api/projects?assistant_id=${encodeURIComponent(assistantId)}`),
  createProject: (assistantId: string, name: string) => request<Project>('/api/projects', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ assistant_id: assistantId, name }),
  }),
  renameProject: (assistantId: string, projectId: string, name: string) => request<Project>(`/api/projects/${projectId}`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ assistant_id: assistantId, name }),
  }),
  deleteProject: (assistantId: string, projectId: string) => request<{ archived_path: string }>(`/api/projects/${projectId}?assistant_id=${encodeURIComponent(assistantId)}`, {
    method: 'DELETE',
  }),
  renameDocument: (assistantId: string, projectId: string, documentId: string, relativePath: string) => request<ProjectDocument>(`/api/projects/${projectId}/documents/${documentId}`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ assistant_id: assistantId, relative_path: relativePath }),
  }),
  deleteDocument: (assistantId: string, projectId: string, documentId: string) => request<{ deleted: boolean; entry_document_id: string | null }>(`/api/projects/${projectId}/documents/${documentId}?assistant_id=${encodeURIComponent(assistantId)}`, {
    method: 'DELETE',
  }),
  getProjectTree: (assistantId: string, projectId: string) => request<ProjectDocument[]>(`/api/projects/${projectId}/tree?assistant_id=${encodeURIComponent(assistantId)}`),
  getDocument: (assistantId: string, projectId: string, documentId: string) => request<ProjectDocument>(`/api/projects/${projectId}/documents/${documentId}?assistant_id=${encodeURIComponent(assistantId)}`),
  saveDocument: (assistantId: string, projectId: string, documentId: string, content: string, version: number) => request<ProjectDocument & { staled_change_set_ids?: string[] }>(`/api/projects/${projectId}/documents/${documentId}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ assistant_id: assistantId, content, document_version: version }),
  }),
  importFile: async (assistantId: string, file: File) => {
    const form = new FormData()
    form.append('assistant_id', assistantId)
    form.append('file', file)
    return request<Project>('/api/projects/import-file', { method: 'POST', body: form })
  },
  importFolder: async (assistantId: string, name: string, files: File[]) => {
    const form = new FormData()
    form.append('assistant_id', assistantId)
    form.append('name', name)
    for (const file of files) {
      form.append('paths', file.webkitRelativePath || file.name)
      form.append('files', file)
    }
    return request<Project>('/api/projects/import-folder', { method: 'POST', body: form })
  },
  rewriteSelection: (payload: Record<string, unknown>, projectId: string, documentId: string) => request<{ task_id: string }>(`/api/projects/${projectId}/documents/${documentId}/selection-rewrites`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  }),
  acceptChangeHunk: (assistantId: string, projectId: string, changeSetId: string, hunkId: string) => request<{
    document: ProjectDocument
    change_set: ChangeSetRecord
    hunk: ChangeSetRecord['hunks'][number]
    staled_change_set_ids: string[]
  }>(`/api/projects/${projectId}/change-sets/${changeSetId}/hunks/${hunkId}/accept`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ assistant_id: assistantId }),
  }),
  rejectChangeHunk: (assistantId: string, projectId: string, changeSetId: string, hunkId: string) => request<{ change_set: ChangeSetRecord }>(`/api/projects/${projectId}/change-sets/${changeSetId}/hunks/${hunkId}/reject`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ assistant_id: assistantId }),
  }),
  acceptAllChangeHunks: (assistantId: string, projectId: string, changeSetId: string) => request<{
    document: ProjectDocument
    change_set: ChangeSetRecord
    applied_hunk_ids: string[]
    stopped: { hunk_id: string; reason: string } | null
    staled_change_set_ids: string[]
  }>(`/api/projects/${projectId}/change-sets/${changeSetId}/accept-all`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ assistant_id: assistantId }),
  }),
  listChangeSets: (assistantId: string, projectId: string, documentId: string, page = 1, pageSize = 20) => request<{
    items: ChangeSetPreview[]
    total: number
    page: number
    page_size: number
  }>(`/api/projects/${projectId}/change-sets?assistant_id=${encodeURIComponent(assistantId)}&document_id=${encodeURIComponent(documentId)}&page=${page}&page_size=${pageSize}`),
  listLlmProviders: () => request<LLMProvidersPayload>('/api/llm/providers'),
  addLlmProvider: (payload: LLMProviderCreatePayload) => request<LLMProvidersPayload>('/api/llm/providers', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }),
  selectLlmProvider: (providerId: string, model: string) => request<LLMProvidersPayload>('/api/llm/providers/current', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ provider_id: providerId, model }),
  }),
  listProjectChatSessions: (assistantId: string, projectId: string) => request<ProjectChatSession[]>(`/api/projects/${projectId}/agent/sessions?assistant_id=${encodeURIComponent(assistantId)}`),
  getProjectChatSession: async (assistantId: string, projectId: string, chatSessionId: string) => {
    const scope = `/api/projects/${projectId}/agent/sessions/${chatSessionId}`
    await request<{ reconciled_task_ids: string[] }>(`${scope}/reconcile?assistant_id=${encodeURIComponent(assistantId)}`, { method: 'POST' })
    return request<ProjectChatSessionDetail>(`${scope}?assistant_id=${encodeURIComponent(assistantId)}`)
  },
  deleteProjectChatSession: (assistantId: string, projectId: string, chatSessionId: string) => request<{ deleted: boolean }>(`/api/projects/${projectId}/agent/sessions/${chatSessionId}?assistant_id=${encodeURIComponent(assistantId)}`, {
    method: 'DELETE',
  }),
  chatProject: (
    assistantId: string,
    projectId: string,
    message: string,
    chatSessionId: string | null,
    currentDocumentId?: string,
  ) => request<{ task_id: string; chat_session_id: string }>(`/api/projects/${projectId}/agent/messages`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      assistant_id: assistantId,
      message,
      chat_session_id: chatSessionId,
      current_document_id: currentDocumentId,
    }),
  }),
  watchTask(
    assistantId: string,
    taskId: string,
    onEvent: (event: TaskEvent) => void,
    onError?: (error: Error) => void,
  ): TaskStream {
    let source: EventSource | null = null
    let lastSeq = -1
    let gapped = false
    let retries = 0
    let timer: ReturnType<typeof setTimeout> | null = null
    let idleTimer: ReturnType<typeof setTimeout> | null = null
    let closed = false

    function clearIdleWatchdog() {
      if (idleTimer !== null) {
        clearTimeout(idleTimer)
        idleTimer = null
      }
    }

    function stopSource() {
      clearIdleWatchdog()
      source?.close()
      source = null
    }

    function resetIdleWatchdog() {
      clearIdleWatchdog()
      if (closed) return
      idleTimer = setTimeout(() => {
        if (closed) return
        stopSource()
        scheduleReconnect()
      }, SSE_IDLE_TIMEOUT_MS)
    }

    function markStreamAlive() {
      // 退避复位以首个事件到达为准：服务端开连即断时永远送不出事件，
      // onopen 清零会让重连以 500ms 无限循环（phase7 P3-10）。
      retries = 0
      resetIdleWatchdog()
    }

    function finish(error?: Error) {
      stopSource()
      if (timer !== null) {
        clearTimeout(timer)
        timer = null
      }
      if (error && !closed) onError?.(error)
      closed = true
    }

    function scheduleReconnect() {
      if (retries >= RECONNECT_DELAYS_MS.length) {
        finish(new Error('任务事件流连接失败，多次重连未成功'))
        return
      }
      const delay = RECONNECT_DELAYS_MS[retries]
      retries += 1
      timer = setTimeout(connect, delay)
    }

    function connect() {
      if (closed) return
      const cursor = lastSeq >= 0 ? `&after_seq=${lastSeq}` : ''
      source = new EventSource(`/api/tasks/${taskId}/stream?assistant_id=${encodeURIComponent(assistantId)}${cursor}`)
      source.onopen = () => {
        resetIdleWatchdog()
      }
      source.addEventListener('heartbeat', markStreamAlive)
      source.onmessage = (message) => {
        markStreamAlive()
        try {
          const event = parseTaskEvent(JSON.parse(message.data))
          if (event.type === 'reconnect_gap') {
            // 游标落后于服务端重放窗口：转发缺口信号，之后只放行终态事件，
            // 不再拼接残缺回复（架构 §5.9/§5.10）。
            gapped = true
            onEvent(event)
            return
          }
          if (typeof event.seq === 'number') {
            if (event.seq <= lastSeq) return
            lastSeq = event.seq
          }
          if (gapped && !TERMINAL_TASK_EVENTS.has(event.type)) return
          onEvent(event)
          if (TERMINAL_TASK_EVENTS.has(event.type)) finish()
        } catch (cause) {
          finish(cause instanceof Error ? cause : new Error(String(cause)))
        }
      }
      source.onerror = () => {
        if (closed) return
        stopSource()
        scheduleReconnect()
      }
    }

    connect()
    return { close: () => finish() }
  },
}

export type WorkspaceApi = Pick<typeof apiClient, 'listProjects' | 'getDocument'>
