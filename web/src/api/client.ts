import type { Assistant, ChangeSetRecord, Project, ProjectDocument, TaskEvent } from '../types'

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

export const apiClient = {
  listAssistants: () => request<Assistant[]>('/api/assistants'),
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
  chatProject: (assistantId: string, projectId: string, message: string, currentDocumentId?: string) => request<{ task_id: string }>(`/api/projects/${projectId}/agent/messages`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ assistant_id: assistantId, message, current_document_id: currentDocumentId }),
  }),
  watchTask(
    assistantId: string,
    taskId: string,
    onEvent: (event: TaskEvent) => void,
    onError?: (error: Error) => void,
  ): EventSource {
    const source = new EventSource(`/api/tasks/${taskId}/stream?assistant_id=${encodeURIComponent(assistantId)}`)
    source.onmessage = (message) => {
      try {
        const event = parseTaskEvent(JSON.parse(message.data))
        onEvent(event)
        if (event.type === 'task_done' || event.type === 'task_failed') source.close()
      } catch (cause) {
        source.close()
        onError?.(cause instanceof Error ? cause : new Error(String(cause)))
      }
    }
    source.onerror = () => {
      source.close()
      onError?.(new Error('任务事件流连接失败'))
    }
    return source
  },
}

export type WorkspaceApi = Pick<typeof apiClient, 'listProjects' | 'getDocument'>
