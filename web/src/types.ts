export interface Assistant {
  id: string
  name: string
  description: string
}

export interface Project {
  project_id: string
  assistant_id: string
  name: string
  root_path: string
  entry_document_id: string | null
}

export interface ProjectDocument {
  document_id: string
  project_id: string
  assistant_id: string
  relative_path: string
  version: number
  editable: boolean
  content: string | null
}

export interface EditorTab extends ProjectDocument {
  content: string
  dirty: boolean
}

export interface ChangePreview {
  change_set_id: string
  project_id: string
  document_id: string
  range: { from: number; to: number }
  original: string
  replacement: string
  document_version: number
  source: 'selection' | 'chat'
}

export interface ChangeSetRecord {
  change_set_id: string
  assistant_id: string
  project_id: string
  document_id: string
  session_id: string | null
  source: 'selection' | 'chat'
  start: number
  end: number
  original_text: string
  replacement_text: string
  base_version: number
  status: 'pending' | 'applied' | 'rejected'
}

export interface TaskStatus {
  task_id: string
  status: 'running' | 'done' | 'failed'
  result: Record<string, unknown> | null
  error: string | null
}

export interface TaskEvent {
  type: string
  data: Record<string, unknown>
  task_id?: string
}

export function isChangePreview(value: unknown): value is ChangePreview {
  if (!value || typeof value !== 'object') return false
  const item = value as Partial<ChangePreview>
  return typeof item.change_set_id === 'string'
    && typeof item.project_id === 'string'
    && typeof item.document_id === 'string'
    && typeof item.original === 'string'
    && typeof item.replacement === 'string'
    && typeof item.document_version === 'number'
    && !!item.range
    && typeof item.range.from === 'number'
    && typeof item.range.to === 'number'
}
