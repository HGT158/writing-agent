<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { basicSetup } from 'codemirror'
import { EditorView } from 'codemirror'
import { EditorState } from '@codemirror/state'
import { markdown } from '@codemirror/lang-markdown'

import { apiClient } from '../api/client'
import { isChangePreview, type ChangePreview, type EditorTab, type TaskEvent } from '../types'
import { codePointToUtf16Offset, utf16ToCodePointOffset } from '../utils/unicodeOffsets'
import ChangeDiff from './ChangeDiff.vue'
import MarkdownPreview from './MarkdownPreview.vue'
import SelectionToolbar from './SelectionToolbar.vue'

const props = defineProps<{
  assistantId: string
  projectId: string
  tab: EditorTab
  externalChange: ChangePreview | null
}>()
const emit = defineEmits<{ update: [content: string]; saved: [document: EditorTab]; preview: [change: ChangePreview]; clearPreview: [] }>()

const editorHost = ref<HTMLElement>()
const editorView = ref<EditorView>()
const showPreview = ref(false)
const toolbar = ref<{ from: number; to: number; left: number; top: number; text: string } | null>(null)
const prompt = ref('')
const loading = ref(false)
const error = ref('')
const localChange = ref<ChangePreview | null>(null)
let syncingExternalContent = false
let selectingChangeRange = false
let stream: EventSource | null = null
let scopeGeneration = 0

function stopStream() {
  stream?.close()
  stream = null
}

function currentChange() {
  const local = localChange.value
  if (local && local.project_id === props.tab.project_id && local.document_id === props.tab.document_id) {
    return local
  }
  const change = props.externalChange
  return change && change.project_id === props.tab.project_id && change.document_id === props.tab.document_id
    ? change
    : null
}

function scopeMatches(
  generation: number,
  assistantId: string,
  projectId: string,
  documentId: string,
) {
  return generation === scopeGeneration
    && props.assistantId === assistantId
    && props.projectId === projectId
    && props.tab.project_id === projectId
    && props.tab.document_id === documentId
}

function selectChangeRange(change: ChangePreview) {
  const view = editorView.value
  if (!view || change.project_id !== props.tab.project_id || change.document_id !== props.tab.document_id) return
  const from = codePointToUtf16Offset(props.tab.content, change.range.from)
  const to = codePointToUtf16Offset(props.tab.content, change.range.to)
  selectingChangeRange = true
  try {
    view.dispatch({ selection: { anchor: from, head: to }, scrollIntoView: true })
  } finally {
    selectingChangeRange = false
  }
  toolbar.value = null
}

function updateSelection(view: EditorView) {
  const selection = view.state.selection.main
  if (selection.empty) {
    toolbar.value = null
    return
  }
  const coords = view.coordsAtPos(selection.from)
  const host = editorHost.value?.getBoundingClientRect()
  if (!coords || !host) return
  toolbar.value = {
    from: selection.from,
    to: selection.to,
    left: coords.left - host.left,
    top: coords.bottom - host.top + 8,
    text: view.state.sliceDoc(selection.from, selection.to),
  }
}

function createEditor() {
  if (!editorHost.value) return
  editorView.value = new EditorView({
    state: EditorState.create({
      doc: props.tab.content,
      extensions: [
        basicSetup,
        markdown(),
        EditorView.updateListener.of((update) => {
          if (update.docChanged && !syncingExternalContent) emit('update', update.state.doc.toString())
          if (update.selectionSet && !selectingChangeRange) updateSelection(update.view)
        }),
      ],
    }),
    parent: editorHost.value,
  })
}

function destroyEditor() {
  editorView.value?.destroy()
  editorView.value = undefined
}

async function submitSelection() {
  if (!toolbar.value || !prompt.value.trim()) return
  const selection = toolbar.value
  const generation = scopeGeneration
  const assistantId = props.assistantId
  const projectId = props.projectId
  const documentId = props.tab.document_id
  const content = props.tab.content
  const documentVersion = props.tab.version
  loading.value = true
  error.value = ''
  try {
    const payload = await apiClient.rewriteSelection({
      assistant_id: assistantId,
      start: utf16ToCodePointOffset(content, selection.from),
      end: utf16ToCodePointOffset(content, selection.to),
      selected_text: selection.text,
      instruction: prompt.value,
      document_version: documentVersion,
    }, projectId, documentId)
    if (!scopeMatches(generation, assistantId, projectId, documentId)) return
    stopStream()
    stream = apiClient.watchTask(assistantId, payload.task_id, (event: TaskEvent) => {
      if (!scopeMatches(generation, assistantId, projectId, documentId)) return
      if (event.type === 'change_preview') {
        if (!isChangePreview(event.data)) {
          error.value = '任务返回了无效的修改预览'
          loading.value = false
          return
        }
        if (event.data.project_id !== projectId || event.data.document_id !== documentId) return
        localChange.value = event.data
        selectChangeRange(localChange.value)
        emit('preview', localChange.value)
      }
      if (event.type === 'task_failed') {
        error.value = String(event.data.reason || '任务失败')
        loading.value = false
      }
      if (event.type === 'task_done') loading.value = false
    }, (cause) => {
      if (!scopeMatches(generation, assistantId, projectId, documentId)) return
      loading.value = false
      error.value = cause.message
    })
  } catch (cause) {
    if (!scopeMatches(generation, assistantId, projectId, documentId)) return
    error.value = cause instanceof Error ? cause.message : String(cause)
    loading.value = false
  }
}

async function applyChange() {
  const change = currentChange()
  if (!change) return
  const generation = scopeGeneration
  const assistantId = props.assistantId
  const projectId = change.project_id
  const documentId = change.document_id
  const version = props.tab.version
  if (props.tab.dirty && !window.confirm('当前文档有未保存修改，接受 AI 修改会丢弃这些修改。继续吗？')) return
  loading.value = true
  try {
    const result = await apiClient.applyChange(assistantId, projectId, change.change_set_id, version)
    if (!scopeMatches(generation, assistantId, projectId, documentId)) return
    editorView.value?.dispatch({ changes: { from: 0, to: editorView.value.state.doc.length, insert: result.document.content || '' } })
    emit('saved', { ...props.tab, ...result.document, content: result.document.content || '', dirty: false })
    localChange.value = null
    emit('clearPreview')
  } catch (cause) {
    if (!scopeMatches(generation, assistantId, projectId, documentId)) return
    error.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    if (scopeMatches(generation, assistantId, projectId, documentId)) loading.value = false
  }
}

async function rejectChange() {
  const change = currentChange()
  if (!change) return
  const generation = scopeGeneration
  const assistantId = props.assistantId
  const projectId = change.project_id
  const documentId = change.document_id
  try {
    await apiClient.rejectChange(assistantId, projectId, change.change_set_id)
    if (!scopeMatches(generation, assistantId, projectId, documentId)) return
    localChange.value = null
    emit('clearPreview')
  } catch (cause) {
    if (!scopeMatches(generation, assistantId, projectId, documentId)) return
    error.value = cause instanceof Error ? cause.message : String(cause)
  }
}

onMounted(createEditor)
onBeforeUnmount(() => {
  stopStream()
  destroyEditor()
})
watch(() => [props.assistantId, props.tab.project_id, props.tab.document_id] as const, () => {
  scopeGeneration += 1
  stopStream()
  destroyEditor()
  toolbar.value = null
  localChange.value = null
  error.value = ''
  loading.value = false
  showPreview.value = false
  createEditor()
})
watch(() => props.externalChange, (change) => {
  if (change) selectChangeRange(change)
})
watch(() => [props.tab.version, props.tab.content] as const, () => {
  const view = editorView.value
  if (!view || view.state.doc.toString() === props.tab.content) return
  syncingExternalContent = true
  try {
    view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: props.tab.content } })
  } finally {
    syncingExternalContent = false
  }
})
</script>

<template>
  <section class="editor-surface">
    <div ref="editorHost" class="code-editor" :class="{ hidden: showPreview }" />
    <MarkdownPreview v-if="showPreview" :content="tab.content" />
    <button class="preview-toggle" title="切换 Markdown 预览" @click="showPreview = !showPreview">{{ showPreview ? '编辑' : '预览' }}</button>
    <SelectionToolbar v-if="toolbar" v-model="prompt" :loading="loading" :style="{ left: `${toolbar.left}px`, top: `${toolbar.top}px` }" @submit="submitSelection" @cancel="toolbar = null" />
    <ChangeDiff :change="currentChange()" :busy="loading" @apply="applyChange" @reject="rejectChange" @regenerate="submitSelection" />
    <p v-if="error" class="editor-error">{{ error }}</p>
  </section>
</template>
