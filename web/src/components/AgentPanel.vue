<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { Bot, Send } from '@lucide/vue'

import { apiClient } from '../api/client'
import { isChangePreview, type ChangePreview, type TaskEvent } from '../types'
import ChangeDiff from './ChangeDiff.vue'

const props = defineProps<{ assistantId: string; projectId: string | null; documentId: string | null }>()
const emit = defineEmits<{
  preview: [change: ChangePreview]
  apply: [change: ChangePreview, complete: (success: boolean) => void]
  reject: [change: ChangePreview, complete: (success: boolean) => void]
}>()
const message = ref('')
const messages = ref<{ role: 'user' | 'assistant'; content: string }[]>([])
const activeChanges = ref<ChangePreview[]>([])
const sending = ref(false)
const error = ref('')
const lastInstruction = ref('')
const scrollHost = ref<HTMLElement>()
const reviewingChanges = ref(new Set<string>())
let stream: EventSource | null = null
let scopeGeneration = 0

function stopStream() {
  stream?.close()
  stream = null
}

async function send(content = message.value.trim(), appendUserMessage = true) {
  if (!props.projectId || !content || sending.value) return
  const generation = scopeGeneration
  const assistantId = props.assistantId
  const projectId = props.projectId
  const documentId = props.documentId
  lastInstruction.value = content
  if (appendUserMessage) {
    message.value = ''
    messages.value.push({ role: 'user', content })
  }
  stopStream()
  sending.value = true
  error.value = ''
  try {
    const { task_id } = await apiClient.chatProject(assistantId, projectId, content, documentId || undefined)
    if (
      generation !== scopeGeneration
      || props.assistantId !== assistantId
      || props.projectId !== projectId
      || props.documentId !== documentId
    ) return
    stream = apiClient.watchTask(assistantId, task_id, async (event: TaskEvent) => {
      if (
        generation !== scopeGeneration
        || props.assistantId !== assistantId
        || props.projectId !== projectId
        || props.documentId !== documentId
      ) return
      if (event.type === 'token') messages.value.push({ role: 'assistant', content: String(event.data.text || '') })
      if (event.type === 'change_preview') {
        if (!isChangePreview(event.data)) {
          error.value = '任务返回了无效的修改预览'
          return
        }
        const change = event.data
        activeChanges.value.push(change)
        emit('preview', change)
      }
      if (event.type === 'task_failed') {
        error.value = String(event.data.reason || '任务失败')
        sending.value = false
      }
      if (event.type === 'task_done') {
        sending.value = false
        await nextTick()
        scrollHost.value?.scrollTo({ top: scrollHost.value.scrollHeight })
      }
    }, (cause) => {
      if (
        generation !== scopeGeneration
        || props.assistantId !== assistantId
        || props.projectId !== projectId
        || props.documentId !== documentId
      ) return
      sending.value = false
      error.value = cause.message
    })
  } catch (cause) {
    if (
      generation !== scopeGeneration
      || props.assistantId !== assistantId
      || props.projectId !== projectId
      || props.documentId !== documentId
    ) return
    sending.value = false
    error.value = cause instanceof Error ? cause.message : String(cause)
  }
}

function regenerate() {
  if (lastInstruction.value) void send(lastInstruction.value, false)
}

function completeReview(change: ChangePreview, success: boolean) {
  const next = new Set(reviewingChanges.value)
  next.delete(change.change_set_id)
  reviewingChanges.value = next
  if (success) {
    activeChanges.value = activeChanges.value.filter((item) => item.change_set_id !== change.change_set_id)
  }
}

function applyChange(change: ChangePreview) {
  const next = new Set(reviewingChanges.value)
  next.add(change.change_set_id)
  reviewingChanges.value = next
  emit('apply', change, (success) => completeReview(change, success))
}

function rejectChange(change: ChangePreview) {
  const next = new Set(reviewingChanges.value)
  next.add(change.change_set_id)
  reviewingChanges.value = next
  emit('reject', change, (success) => completeReview(change, success))
}

watch(() => [props.assistantId, props.projectId, props.documentId] as const, () => {
  scopeGeneration += 1
  stopStream()
  messages.value = []
  activeChanges.value = []
  lastInstruction.value = ''
  sending.value = false
  error.value = ''
  reviewingChanges.value = new Set()
})
onBeforeUnmount(stopStream)
</script>

<template>
  <aside class="agent-panel">
    <div class="panel-heading"><span><Bot :size="16" /> Agent</span><span class="scope-label">{{ projectId ? '当前项目' : '未选择项目' }}</span></div>
    <div ref="scrollHost" class="agent-messages">
      <div v-if="!messages.length" class="agent-empty">选择项目后，让 Agent 解释、审校或修改正文。</div>
      <div v-for="(item, index) in messages" :key="index" class="message" :class="item.role"><span class="message-role">{{ item.role === 'user' ? '你' : 'AI' }}</span><p>{{ item.content }}</p></div>
      <ChangeDiff v-for="change in activeChanges" :key="change.change_set_id" :change="change" :busy="sending || reviewingChanges.has(change.change_set_id)" @apply="applyChange(change)" @reject="rejectChange(change)" @regenerate="regenerate" />
      <p v-if="error" class="inline-error">{{ error }}</p>
    </div>
    <form class="agent-composer" @submit.prevent="send()">
      <textarea v-model="message" :disabled="!projectId || sending" rows="3" placeholder="对当前项目下指令..." />
      <button class="send-button" title="发送消息" :disabled="!projectId || sending || !message.trim()"><Send :size="16" /></button>
    </form>
  </aside>
</template>
