import type {
  Assistant,
  ChangeSetRecord,
  Project,
  ProjectChatSession,
  ProjectChatSessionDetail,
  ProjectDocument,
  TaskEvent,
} from '../types'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init)
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`
    try {
      const body = await response.json() as { detail?: string }
      detail = body.detail || detail
    } catch {
      // Non-JSON server errors keep the HTTP status text.
    }
    throw new Error(detail)
  }
  return response.json() as Promise<T>
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

export const apiClient = {
  listAssistants: () => request<Assistant[]>('/api/assistants'),
  createAssistant: (id: string, name: string, description: string) => request<Assistant>('/api/assistants', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id, name, description }),
  }),
  deleteAssistant: (assistantId: string) => request<{ archived_path: string; purged: boolean }>(
    `/api/assistants/${encodeURIComponent(assistantId)}`,
    { method: 'DELETE' },
  ),
  listProjects: (assistantId: string) => request<Project[]>(`/api/projects?assistant_id=${encodeURIComponent(assistantId)}`),
  createProject: (assistantId: string, name: string) => request<Project>('/api/projects', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ assistant_id: assistantId, name }),
  }),
  getProjectTree: (assistantId: string, projectId: string) => request<ProjectDocument[]>(`/api/projects/${projectId}/tree?assistant_id=${encodeURIComponent(assistantId)}`),
  getDocument: (assistantId: string, projectId: string, documentId: string) => request<ProjectDocument>(`/api/projects/${projectId}/documents/${documentId}?assistant_id=${encodeURIComponent(assistantId)}`),
  saveDocument: (assistantId: string, projectId: string, documentId: string, content: string, version: number) => request<ProjectDocument>(`/api/projects/${projectId}/documents/${documentId}`, {
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
  applyChange: (assistantId: string, projectId: string, changeSetId: string, version: number) => request<{ document: ProjectDocument; change_set: ChangeSetRecord }>(`/api/projects/${projectId}/change-sets/${changeSetId}/apply`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ assistant_id: assistantId, document_version: version }),
  }),
  rejectChange: (assistantId: string, projectId: string, changeSetId: string) => request(`/api/projects/${projectId}/change-sets/${changeSetId}/reject`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ assistant_id: assistantId }),
  }),
  listProjectChatSessions: (assistantId: string, projectId: string) => request<ProjectChatSession[]>(`/api/projects/${projectId}/agent/sessions?assistant_id=${encodeURIComponent(assistantId)}`),
  getProjectChatSession: (assistantId: string, projectId: string, chatSessionId: string) => request<ProjectChatSessionDetail>(`/api/projects/${projectId}/agent/sessions/${chatSessionId}?assistant_id=${encodeURIComponent(assistantId)}`),
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
    let closed = false

    function stopSource() {
      source?.close()
      source = null
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
      source.onopen = () => { retries = 0 }
      source.onmessage = (message) => {
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
