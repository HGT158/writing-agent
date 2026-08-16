<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { Bot, CheckCheck, Loader, Plus, Send, Trash2 } from '@lucide/vue'

import { apiClient } from '../api/client'
import type { TaskStream } from '../api/client'
import {
  isChangePreview,
  type ChangePreview,
  type ProjectChatSession,
  type TaskEvent,
  type WorkEventRecord,
} from '../types'
import ChangeDiff from './ChangeDiff.vue'
import MarkdownPreview from './MarkdownPreview.vue'

const props = defineProps<{
  assistantId: string
  projectId: string | null
  documentId: string | null
  changes: ChangePreview[]
  reviewing: string[]
  documentLabels: Record<string, string>
}>()
const emit = defineEmits<{
  apply: [change: ChangePreview]
  reject: [change: ChangePreview]
  applyAll: [changes: ChangePreview[]]
  changesLoaded: [changes: ChangePreview[]]
  changeAdded: [change: ChangePreview]
  openDocument: [projectId: string, documentId: string]
}>()

interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
  message_id?: number
}

interface WorkItemView {
  workId: string
  kind: string
  status: string
  title: string
  delta: string
  changeSetId: string | null
  documentId: string | null
}

interface WorkRecordView {
  taskId: string
  userMessageId: number | null
  terminal: string
  collapsed: boolean
  items: WorkItemView[]
  startedAt: number
  endedAt: number | null
}

const TERMINAL_LABELS: Record<string, string> = {
  running: '运行中',
  succeeded: '已完成',
  failed: '失败',
  interrupted: '已中断',
}

const message = ref('')
const messages = ref<ChatMessage[]>([])
const sessions = ref<ProjectChatSession[]>([])
const activeSessionId = ref<string | null>(null)
const sending = ref(false)
const loadingSession = ref(false)
const deletingSession = ref(false)
const error = ref('')
const workRecords = ref<WorkRecordView[]>([])
const liveWork = ref<WorkRecordView | null>(null)
const lastInstruction = ref('')
const scrollHost = ref<HTMLElement>()
const followTail = ref(true)
let stream: TaskStream | null = null
let scopeGeneration = 0
let assistantMessageIndex: number | null = null
let liveUserIndex: number | null = null

const reviewingIds = computed(() => new Set(props.reviewing))
const busy = computed(() => sending.value || loadingSession.value)
// 只有本会话产生的 diff 会阻止删除会话；选区改写的建议与会话生命周期无关。
const chatChanges = computed(() => props.changes.filter((change) => change.source === 'chat'))

function stopStream() {
  stream?.close()
  stream = null
}

function isProjectScope(generation: number, assistantId: string, projectId: string) {
  return generation === scopeGeneration
    && props.assistantId === assistantId
    && props.projectId === projectId
}

/** 用户主动上滚查看历史时停止跟随，回到底部后恢复（架构 §5.10）。 */
function onScroll() {
  const host = scrollHost.value
  if (!host) return
  followTail.value = host.scrollHeight - host.scrollTop - host.clientHeight < 48
}

async function scrollToTail(force = false) {
  if (!force && !followTail.value) return
  await nextTick()
  scrollHost.value?.scrollTo({ top: scrollHost.value.scrollHeight })
}

function clearConversation() {
  messages.value = []
  lastInstruction.value = ''
  sending.value = false
  loadingSession.value = false
  error.value = ''
  workRecords.value = []
  liveWork.value = null
  followTail.value = true
  assistantMessageIndex = null
  liveUserIndex = null
}

function appendAssistantDelta(text: string) {
  if (!text) return
  const current = assistantMessageIndex === null ? null : messages.value[assistantMessageIndex]
  if (!current || current.role !== 'assistant') {
    messages.value.push({ role: 'assistant', content: text })
    assistantMessageIndex = messages.value.length - 1
    return
  }
  current.content += text
}

function removeTransientAssistantMessage() {
  if (assistantMessageIndex !== null && messages.value[assistantMessageIndex]?.role === 'assistant') {
    messages.value.splice(assistantMessageIndex, 1)
  }
  assistantMessageIndex = null
}

/** 把持久化工作事件按 task_id 分组为可展开记录；历史默认折叠（架构 §5.10）。 */
function recordsFromEvents(events: WorkEventRecord[]): WorkRecordView[] {
  const groups = new Map<string, WorkEventRecord[]>()
  for (const item of events) {
    const group = groups.get(item.task_id) || []
    group.push(item)
    groups.set(item.task_id, group)
  }
  const records: WorkRecordView[] = []
  for (const [taskId, group] of groups) {
    const terminal = group.find((item) => item.kind === 'task')
    records.push({
      taskId,
      userMessageId: group[0]?.user_message_id ?? null,
      // 服务端对账只放行仍在运行的组：无终态即运行中，不得显示为已中断。
      terminal: terminal?.status ?? 'running',
      collapsed: true,
      items: group.filter((item) => item.kind !== 'task').map((item) => ({
        workId: `event-${item.event_id}`,
        kind: item.kind,
        status: item.status,
        title: item.title,
        delta: item.result_summary || item.detail || '',
        changeSetId: item.change_set_id,
        documentId: item.document_id,
      })),
      startedAt: Date.parse(terminal?.created_at ?? group[0]?.created_at ?? '') || 0,
      endedAt: terminal ? Date.parse(terminal.completed_at ?? terminal.created_at) || null : null,
    })
  }
  return records
}

function ensureLiveWork(): WorkRecordView {
  if (!liveWork.value) {
    liveWork.value = {
      taskId: 'live',
      userMessageId: null,
      terminal: 'running',
      collapsed: false,
      items: [],
      startedAt: Date.now(),
      endedAt: null,
    }
  }
  return liveWork.value
}

function handleWorkEvent(event: TaskEvent) {
  const data = event.data
  const workId = String(data.work_id || '')
  if (!workId) return
  const record = ensureLiveWork()
  if (event.type === 'work_item_start') {
    record.items.push({
      workId,
      kind: String(data.kind || 'progress'),
      status: 'running',
      title: String(data.title || ''),
      delta: '',
      changeSetId: typeof data.change_set_id === 'string' ? data.change_set_id : null,
      documentId: typeof data.document_id === 'string' ? data.document_id : null,
    })
    void scrollToTail()
    return
  }
  if (event.type === 'work_item_delta') {
    const item = record.items.find((entry) => entry.workId === workId)
    if (item) item.delta = `${item.delta}${String(data.text || '')}`.slice(-500)
    return
  }
  if (event.type === 'work_item_done') {
    const item = record.items.find((entry) => entry.workId === workId)
    if (item) {
      item.status = String(data.status || 'succeeded')
      const summary = typeof data.result_summary === 'string' && data.result_summary
        ? data.result_summary
        : typeof data.detail === 'string' ? data.detail : ''
      if (summary) item.delta = summary
    }
  }
}

function finishLiveWork(terminal: string) {
  const record = liveWork.value
  if (!record) return
  record.terminal = terminal
  record.endedAt = Date.now()
  record.collapsed = true
}

function toggleWorkRecord(record: WorkRecordView) {
  record.collapsed = !record.collapsed
}

function workRecordTitle(record: WorkRecordView): string {
  const tools = record.items.filter((item) => item.kind === 'tool').length
  const changes = record.items.filter((item) => item.kind === 'changes').length
  const end = record.endedAt ?? Date.now()
  const seconds = Math.max(0, Math.round((end - record.startedAt) / 1000))
  const parts = [`工具 ${tools}`]
  if (changes) parts.push(`建议 ${changes}`)
  parts.push(`耗时 ${seconds}s`)
  return `${TERMINAL_LABELS[record.terminal] ?? record.terminal} · ${parts.join(' · ')}`
}

function workItemTitle(item: WorkItemView): string {
  const labels: Record<string, string> = {
    progress: '进度', tool: '工具', warning: '警告', changes: '修改建议',
  }
  return `${labels[item.kind] ?? item.kind}：${item.title}`
}

function openWorkDocument(item: WorkItemView) {
  if (item.kind !== 'changes' || !item.documentId || !props.projectId) return
  emit('openDocument', props.projectId, item.documentId)
}

/** 消息与工作记录的交错视图：记录跟在触发它的 user 消息之后（架构 §5.10）。 */
const conversation = computed<Array<
  { type: 'message'; index: number } | { type: 'record'; record: WorkRecordView }
>>(() => {
  const rows: Array<{ type: 'message'; index: number } | { type: 'record'; record: WorkRecordView }> = []
  messages.value.forEach((item, index) => {
    rows.push({ type: 'message', index })
    if (item.role !== 'user') return
    const record = workRecords.value.find((entry) => entry.userMessageId === item.message_id)
    if (record) {
      rows.push({ type: 'record', record })
      return
    }
    if (liveWork.value && index === liveUserIndex) {
      rows.push({ type: 'record', record: liveWork.value })
    }
  })
  return rows
})

async function loadSession(
  assistantId: string,
  projectId: string,
  chatSessionId: string,
  generation: number,
): Promise<boolean> {
  loadingSession.value = true
  try {
    const detail = await apiClient.getProjectChatSession(assistantId, projectId, chatSessionId)
    if (!isProjectScope(generation, assistantId, projectId) || activeSessionId.value !== chatSessionId) return false
    messages.value = detail.messages.map((item) => ({
      role: item.role,
      content: item.content,
      message_id: item.message_id,
    }))
    workRecords.value = recordsFromEvents(detail.work_events || [])
    liveWork.value = null
    emit('changesLoaded', detail.pending_changes.filter(isChangePreview))
    lastInstruction.value = [...detail.messages].reverse().find((item) => item.role === 'user')?.content || ''
    void scrollToTail(true)
    return true
  } catch (cause) {
    if (!isProjectScope(generation, assistantId, projectId) || activeSessionId.value !== chatSessionId) return false
    error.value = cause instanceof Error ? cause.message : String(cause)
    return false
  } finally {
    if (isProjectScope(generation, assistantId, projectId) && activeSessionId.value === chatSessionId) {
      loadingSession.value = false
    }
  }
}

async function loadProjectSessions(assistantId: string, projectId: string, generation: number) {
  loadingSession.value = true
  try {
    const loaded = await apiClient.listProjectChatSessions(assistantId, projectId)
    if (!isProjectScope(generation, assistantId, projectId)) return
    sessions.value = loaded
    const latest = loaded[0]
    activeSessionId.value = latest?.chat_session_id || null
    if (latest) {
      await loadSession(assistantId, projectId, latest.chat_session_id, generation)
    }
  } catch (cause) {
    if (!isProjectScope(generation, assistantId, projectId)) return
    error.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    if (isProjectScope(generation, assistantId, projectId)) loadingSession.value = false
  }
}

function startNewSession() {
  if (!props.projectId || busy.value) return
  scopeGeneration += 1
  stopStream()
  activeSessionId.value = null
  clearConversation()
  emit('changesLoaded', [])
}

function selectSession(event: Event) {
  const chatSessionId = (event.target as HTMLSelectElement).value || null
  if (chatSessionId === activeSessionId.value || !props.projectId || loadingSession.value) return
  scopeGeneration += 1
  const generation = scopeGeneration
  const assistantId = props.assistantId
  const projectId = props.projectId
  stopStream()
  clearConversation()
  emit('changesLoaded', [])
  activeSessionId.value = chatSessionId
  if (chatSessionId) void loadSession(assistantId, projectId, chatSessionId, generation)
}

async function deleteSession() {
  const projectId = props.projectId
  const chatSessionId = activeSessionId.value
  if (!projectId || !chatSessionId || chatChanges.value.length || busy.value || deletingSession.value) return
  const generation = scopeGeneration
  const assistantId = props.assistantId
  deletingSession.value = true
  error.value = ''
  try {
    await apiClient.deleteProjectChatSession(assistantId, projectId, chatSessionId)
    if (!isProjectScope(generation, assistantId, projectId) || activeSessionId.value !== chatSessionId) return
    sessions.value = sessions.value.filter((item) => item.chat_session_id !== chatSessionId)
    scopeGeneration += 1
    const nextGeneration = scopeGeneration
    stopStream()
    clearConversation()
    const next = sessions.value[0]
    activeSessionId.value = next?.chat_session_id || null
    if (next) void loadSession(assistantId, projectId, next.chat_session_id, nextGeneration)
  } catch (cause) {
    if (!isProjectScope(generation, assistantId, projectId) || activeSessionId.value !== chatSessionId) return
    error.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    deletingSession.value = false
  }
}

function registerServerSession(chatSessionId: string, titleSource: string) {
  activeSessionId.value = chatSessionId
  if (sessions.value.some((item) => item.chat_session_id === chatSessionId)) return
  const now = new Date().toISOString()
  const titleLine = titleSource.split(/\r?\n/).find((line) => line.trim())?.trim() || '新对话'
  sessions.value.unshift({
    chat_session_id: chatSessionId,
    title: Array.from(titleLine).slice(0, 80).join(''),
    created_at: now,
    updated_at: now,
    message_count: 1,
  })
}

async function send(content = message.value.trim(), appendUserMessage = true) {
  if (!props.projectId || !content || busy.value) return
  const generation = scopeGeneration
  const assistantId = props.assistantId
  const projectId = props.projectId
  const requestedSessionId = activeSessionId.value
  const documentId = props.documentId
  const optimisticUserIndex = appendUserMessage ? messages.value.length : null
  assistantMessageIndex = null
  liveUserIndex = optimisticUserIndex
  lastInstruction.value = content
  if (appendUserMessage) {
    message.value = ''
    messages.value.push({ role: 'user', content })
  }
  stopStream()
  sending.value = true
  error.value = ''
  liveWork.value = null
  followTail.value = true
  void scrollToTail(true)
  try {
    const { task_id, chat_session_id } = await apiClient.chatProject(
      assistantId,
      projectId,
      content,
      requestedSessionId,
      documentId || undefined,
    )
    if (!isProjectScope(generation, assistantId, projectId) || activeSessionId.value !== requestedSessionId) return
    registerServerSession(chat_session_id, content)
    let gapped = false
    stream = apiClient.watchTask(assistantId, task_id, async (event: TaskEvent) => {
      if (!isProjectScope(generation, assistantId, projectId) || activeSessionId.value !== chat_session_id) return
      if (event.type === 'token') {
        appendAssistantDelta(String(event.data.text || ''))
        await scrollToTail()
      }
      if (event.type === 'work_item_start' || event.type === 'work_item_delta'
        || event.type === 'work_item_done') {
        handleWorkEvent(event)
        return
      }
      if (event.type === 'reconnect_gap') {
        // 回复流出现不可恢复缺口：丢弃半截回复，等待终态后从服务器恢复完整会话。
        gapped = true
        removeTransientAssistantMessage()
        error.value = '网络中断，回复流不完整；任务仍在后台运行，完成后将自动从服务器恢复完整内容。'
        return
      }
      if (event.type === 'change_preview') {
        if (!isChangePreview(event.data)) {
          error.value = '任务返回了无效的修改预览'
          return
        }
        emit('changeAdded', event.data)
        await scrollToTail()
      }
      if (event.type === 'task_failed') {
        removeTransientAssistantMessage()
        finishLiveWork('failed')
        error.value = String(event.data.reason || '任务失败')
        sending.value = false
        if (gapped) void loadSession(assistantId, projectId, chat_session_id, generation)
      }
      if (event.type === 'task_done') {
        sending.value = false
        finishLiveWork('succeeded')
        void refreshSessionList(assistantId, projectId, chat_session_id, generation)
        if (gapped) {
          const restored = await loadSession(assistantId, projectId, chat_session_id, generation)
          // 恢复失败时保留 loadSession 自己写入的错误，不清除为空。
          if (restored) error.value = ''
        } else {
          await scrollToTail()
        }
      }
    }, (cause) => {
      if (!isProjectScope(generation, assistantId, projectId) || activeSessionId.value !== chat_session_id) return
      sending.value = false
      removeTransientAssistantMessage()
      finishLiveWork('interrupted')
      error.value = `${cause.message}。任务仍可能在后台完成，刷新可恢复会话。`
    })
  } catch (cause) {
    if (!isProjectScope(generation, assistantId, projectId) || activeSessionId.value !== requestedSessionId) return
    if (optimisticUserIndex !== null) {
      const optimistic = messages.value[optimisticUserIndex]
      if (optimistic?.role === 'user' && optimistic.content === content) {
        messages.value.splice(optimisticUserIndex, 1)
      }
    }
    sending.value = false
    error.value = cause instanceof Error ? cause.message : String(cause)
  }
}

function regenerate() {
  if (lastInstruction.value) void send(lastInstruction.value)
}

async function refreshSessionList(
  assistantId: string,
  projectId: string,
  chatSessionId: string,
  generation: number,
) {
  try {
    const loaded = await apiClient.listProjectChatSessions(assistantId, projectId)
    if (!isProjectScope(generation, assistantId, projectId) || activeSessionId.value !== chatSessionId) return
    const active = sessions.value.find((item) => item.chat_session_id === chatSessionId)
    sessions.value = active && !loaded.some((item) => item.chat_session_id === chatSessionId)
      ? [active, ...loaded]
      : loaded
  } catch {
    // Conversation completion is not failed by a list refresh.
  }
}

watch(() => [props.assistantId, props.projectId] as const, ([assistantId, projectId]) => {
  scopeGeneration += 1
  const generation = scopeGeneration
  stopStream()
  sessions.value = []
  activeSessionId.value = null
  clearConversation()
  if (projectId) void loadProjectSessions(assistantId, projectId, generation)
}, { immediate: true })
onBeforeUnmount(stopStream)
</script>

<template>
  <aside class="agent-panel">
    <div class="panel-heading">
      <span><Bot :size="15" /> Agent</span>
      <span class="scope-label">{{ projectId ? '当前项目' : '未选择项目' }}</span>
    </div>
    <div class="chat-session-toolbar">
      <select
        class="chat-session-select"
        :value="activeSessionId || ''"
        :disabled="!projectId || busy"
        title="聊天历史"
        @change="selectSession"
      >
        <option value="">新对话</option>
        <option v-for="session in sessions" :key="session.chat_session_id" :value="session.chat_session_id">{{ session.title }}</option>
      </select>
      <button class="new-chat-button" title="新建对话" :disabled="!projectId || busy" @click="startNewSession"><Plus :size="15" /></button>
      <button
        class="delete-chat-button"
        title="删除当前对话"
        :disabled="!activeSessionId || !!chatChanges.length || busy || deletingSession"
        @click="deleteSession"
      ><Trash2 :size="15" /></button>
    </div>
    <div ref="scrollHost" class="agent-messages" @scroll="onScroll">
      <p v-if="loadingSession" class="agent-loading"><Loader :size="14" class="spin" /> 正在加载会话…</p>
      <div v-else-if="!messages.length" class="agent-empty">
        {{ projectId ? '在当前会话中让 Agent 解释、审校或修改正文。修改会以 diff 形式等待你确认。' : '选择项目后，让 Agent 解释、审校或修改正文。' }}
      </div>
      <template v-for="(row, rowIndex) in conversation" :key="row.type === 'message' ? `m${row.index}` : `w${rowIndex}`">
        <div v-if="row.type === 'message'" class="message" :class="messages[row.index].role">
          <span class="message-role">{{ messages[row.index].role === 'user' ? '你' : 'AI' }}</span>
          <p v-if="messages[row.index].role === 'user'" class="message-text">{{ messages[row.index].content }}</p>
          <MarkdownPreview v-else class="message-markdown" :content="messages[row.index].content" />
        </div>
        <div v-else class="work-record" :class="row.record.terminal">
          <button type="button" class="work-record-header" @click="toggleWorkRecord(row.record)">
            <Loader v-if="row.record.terminal === 'running'" :size="13" class="spin" />
            <CheckCheck v-else-if="row.record.terminal === 'succeeded'" :size="13" />
            {{ workRecordTitle(row.record) }}
          </button>
          <ul v-if="!row.record.collapsed" class="work-record-items">
            <li
              v-for="item in row.record.items"
              :key="item.workId"
              class="work-item"
              :class="[item.status, item.kind]"
              @click="openWorkDocument(item)"
            >
              <span class="work-item-title">{{ workItemTitle(item) }}</span>
              <span v-if="item.delta" class="work-item-detail">{{ item.delta }}</span>
            </li>
          </ul>
        </div>
      </template>
      <div v-if="changes.length" class="change-review">
        <div class="change-review-heading">
          <span>{{ changes.length }} 处待确认修改</span>
          <button
            v-if="changes.length > 1"
            class="link-action"
            :disabled="sending || !!reviewing.length"
            @click="emit('applyAll', [...changes])"
          >全部接受</button>
        </div>
        <ChangeDiff
          v-for="change in changes"
          :key="change.change_set_id"
          :change="change"
          :label="documentLabels[change.document_id]"
          :busy="sending || reviewingIds.has(change.change_set_id)"
          :regenerable="change.source === 'chat'"
          @apply="emit('apply', change)"
          @reject="emit('reject', change)"
          @regenerate="regenerate"
        />
      </div>
      <p v-if="error" class="inline-error">{{ error }}</p>
    </div>
    <form class="agent-composer" @submit.prevent="send()">
      <textarea
        v-model="message"
        :disabled="!projectId || busy"
        rows="3"
        placeholder="对当前项目下指令，Enter 发送 / Shift+Enter 换行"
        @keydown.enter.exact.prevent="send()"
      />
      <button class="send-button" title="发送消息" :disabled="!projectId || busy || !message.trim()"><Send :size="15" /></button>
    </form>
  </aside>
</template>
