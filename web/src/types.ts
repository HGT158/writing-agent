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

/** 单个修改片段（v1.20 hunk 模型）；range 为 Unicode code point 半开区间。 */
export interface ChangeHunkPreview {
  hunk_id: string
  range: { from: number; to: number }
  original: string
  replacement: string
  status: 'pending' | 'applied' | 'rejected' | 'stale'
}

/** change set 是 hunk 容器；接受/放弃以 hunk 为最小单元（TRAE 式逐处审查）。 */
export interface ChangeSetPreview {
  change_set_id: string
  project_id: string
  document_id: string
  hunks: ChangeHunkPreview[]
  document_version: number
  source: 'selection' | 'chat'
  status?: string
}

/** 兼容旧名：change set 预览。 */
export type ChangePreview = ChangeSetPreview

export interface ChangeHunkRecord {
  hunk_id: string
  change_set_id: string
  display_order: number
  start: number
  end: number
  original_text: string
  new_text: string
  status: string
  created_at: string
  applied_at: string | null
}

export interface ChangeSetRecord {
  change_set_id: string
  assistant_id: string
  project_id: string
  document_id: string
  session_id: string | null
  source: 'selection' | 'chat'
  task_id: string
  base_version: number
  status: string
  hunks: ChangeHunkRecord[]
}

export interface ProjectChatSession {
  chat_session_id: string
  assistant_id?: string
  project_id?: string
  title: string
  created_at: string
  updated_at: string
  message_count: number
}

export interface ProjectChatMessage {
  message_id: number
  role: 'user' | 'assistant'
  content: string
  created_at: string
}

export interface WorkEventRecord {
  event_id: number
  task_id: string
  user_message_id: number
  event_seq: number
  kind: 'progress' | 'tool' | 'warning' | 'changes' | 'task'
  status: 'succeeded' | 'failed' | 'interrupted'
  change_set_id: string | null
  document_id: string | null
  title: string
  detail: string
  tool_name: string | null
  args_summary: string | null
  result_summary: string | null
  created_at: string
  completed_at: string | null
}

export interface ProjectChatSessionDetail {
  session: ProjectChatSession
  messages: ProjectChatMessage[]
  pending_changes: ChangePreview[]
  work_events: WorkEventRecord[]
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
  seq?: number
}

export function isChangePreview(value: unknown): value is ChangeSetPreview {
  if (!value || typeof value !== 'object') return false
  const item = value as Partial<ChangeSetPreview>
  return typeof item.change_set_id === 'string'
    && typeof item.project_id === 'string'
    && typeof item.document_id === 'string'
    && typeof item.document_version === 'number'
    && Array.isArray(item.hunks)
    && item.hunks.length > 0
    && item.hunks.every((hunk) =>
      typeof hunk.hunk_id === 'string'
      && typeof hunk.original === 'string'
      && typeof hunk.replacement === 'string'
      && typeof hunk.status === 'string'
      && !!hunk.range
      && typeof hunk.range.from === 'number'
      && typeof hunk.range.to === 'number',
    )
}

/** 服务端 change set 记录（asdict 形态）→ 前端预览形态。 */
export function toChangeSetPreview(record: ChangeSetRecord): ChangeSetPreview {
  return {
    change_set_id: record.change_set_id,
    project_id: record.project_id,
    document_id: record.document_id,
    hunks: record.hunks.map((hunk) => ({
      hunk_id: hunk.hunk_id,
      range: { from: hunk.start, to: hunk.end },
      original: hunk.original_text,
      replacement: hunk.new_text,
      status: hunk.status as ChangeHunkPreview['status'],
    })),
    document_version: record.base_version,
    source: record.source,
    status: record.status,
  }
}
