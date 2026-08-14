<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { Bot, CheckCheck, Loader, Plus, Send, Trash2 } from '@lucide/vue'

import { apiClient } from '../api/client'
import {
  isChangePreview,
  type ChangePreview,
  type ProjectChatSession,
  type TaskEvent,
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
}>()

const message = ref('')
const messages = ref<{ role: 'user' | 'assistant'; content: string }[]>([])
const sessions = ref<ProjectChatSession[]>([])
const activeSessionId = ref<string | null>(null)
const sending = ref(false)
const loadingSession = ref(false)
const deletingSession = ref(false)
const error = ref('')
const toolStatus = ref<{ state: 'running' | 'done' | 'failed'; text: string } | null>(null)
const lastInstruction = ref('')
const scrollHost = ref<HTMLElement>()
const followTail = ref(true)
let stream: EventSource | null = null
let scopeGeneration = 0
let assistantMessageIndex: number | null = null

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
  toolStatus.value = null
  followTail.value = true
  assistantMessageIndex = null
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

async function loadSession(
  assistantId: string,
  projectId: string,
  chatSessionId: string,
  generation: number,
) {
  loadingSession.value = true
  try {
    const detail = await apiClient.getProjectChatSession(assistantId, projectId, chatSessionId)
    if (!isProjectScope(generation, assistantId, projectId) || activeSessionId.value !== chatSessionId) return
    messages.value = detail.messages.map((item) => ({ role: item.role, content: item.content }))
    emit('changesLoaded', detail.pending_changes.filter(isChangePreview))
    lastInstruction.value = [...detail.messages].reverse().find((item) => item.role === 'user')?.content || ''
    void scrollToTail(true)
  } catch (cause) {
    if (!isProjectScope(generation, assistantId, projectId) || activeSessionId.value !== chatSessionId) return
    error.value = cause instanceof Error ? cause.message : String(cause)
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
  lastInstruction.value = content
  if (appendUserMessage) {
    message.value = ''
    messages.value.push({ role: 'user', content })
  }
  stopStream()
  sending.value = true
  error.value = ''
  toolStatus.value = null
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
    stream = apiClient.watchTask(assistantId, task_id, async (event: TaskEvent) => {
      if (!isProjectScope(generation, assistantId, projectId) || activeSessionId.value !== chat_session_id) return
      if (event.type === 'token') {
        appendAssistantDelta(String(event.data.text || ''))
        await scrollToTail()
      }
      if (event.type === 'tool_call' && event.data.tool === 'propose_project_edits') {
        toolStatus.value = { state: 'running', text: 'Agent 正在准备修改' }
      }
      if (event.type === 'tool_result' && event.data.tool === 'propose_project_edits') {
        toolStatus.value = event.data.ok
          ? { state: 'done', text: String(event.data.summary || '修改建议已生成') }
          : { state: 'failed', text: String(event.data.error || '修改建议生成失败') }
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
        toolStatus.value = null
        error.value = String(event.data.reason || '任务失败')
        sending.value = false
      }
      if (event.type === 'task_done') {
        sending.value = false
        toolStatus.value = null
        void refreshSessionList(assistantId, projectId, chat_session_id, generation)
        await scrollToTail()
      }
    }, (cause) => {
      if (!isProjectScope(generation, assistantId, projectId) || activeSessionId.value !== chat_session_id) return
      sending.value = false
      removeTransientAssistantMessage()
      toolStatus.value = null
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
      <div v-for="(item, index) in messages" :key="index" class="message" :class="item.role">
        <span class="message-role">{{ item.role === 'user' ? '你' : 'AI' }}</span>
        <p v-if="item.role === 'user'" class="message-text">{{ item.content }}</p>
        <MarkdownPreview v-else class="message-markdown" :content="item.content" />
      </div>
      <p v-if="toolStatus" class="tool-status" :class="toolStatus.state">
        <Loader v-if="toolStatus.state === 'running'" :size="13" class="spin" />
        <CheckCheck v-else-if="toolStatus.state === 'done'" :size="13" />
        {{ toolStatus.text }}
      </p>
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
