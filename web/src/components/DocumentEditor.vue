<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { basicSetup } from 'codemirror'
import { EditorView } from 'codemirror'
import { EditorState } from '@codemirror/state'
import { markdown } from '@codemirror/lang-markdown'

import { apiClient } from '../api/client'
import type { TaskStream } from '../api/client'
import { frozenSelectionField, setFrozenSelection } from '../editor/frozenSelection'
import { inlineDiffField, setInlineDiffs, type InlineDiff } from '../editor/inlineDiff'
import { isChangePreview, type ChangePreview, type EditorTab, type TaskEvent } from '../types'
import { codePointToUtf16Offset, utf16ToCodePointOffset } from '../utils/unicodeOffsets'
import MarkdownPreview from './MarkdownPreview.vue'
import SelectionToolbar from './SelectionToolbar.vue'

const props = defineProps<{
  assistantId: string
  projectId: string
  tab: EditorTab
  changes: ChangePreview[]
  reviewing: string[]
}>()
const emit = defineEmits<{
  update: [content: string]
  preview: [change: ChangePreview]
  apply: [change: ChangePreview]
  reject: [change: ChangePreview]
}>()

const editorHost = ref<HTMLElement>()
const editorView = ref<EditorView>()
const showPreview = ref(false)
const toolbar = ref<{ from: number; to: number; left: number; top: number; text: string } | null>(null)
const prompt = ref('')
const loading = ref(false)
const error = ref('')
let syncingExternalContent = false
let stream: TaskStream | null = null
let scopeGeneration = 0

/**
 * 内联 diff 依赖 change set 的原文位置，只有当标签页正文仍等于建议的基准版本
 * 且没有未保存修改时才能对齐；否则降级为提示，交给侧栏卡片处理（架构 §5.10）。
 */
const inlineDiffs = computed<InlineDiff[]>(() => {
  if (props.tab.dirty) return []
  const reviewing = new Set(props.reviewing)
  return props.changes
    .filter((change) => change.document_version === props.tab.version)
    .map((change) => ({
      changeSetId: change.change_set_id,
      from: codePointToUtf16Offset(props.tab.content, change.range.from),
      to: codePointToUtf16Offset(props.tab.content, change.range.to),
      replacement: change.replacement,
      busy: reviewing.has(change.change_set_id),
    }))
})

const staleChangeCount = computed(() => props.changes.length - inlineDiffs.value.length)

const handlers = {
  accept: (changeSetId: string) => {
    const change = props.changes.find((item) => item.change_set_id === changeSetId)
    if (change) emit('apply', change)
  },
  reject: (changeSetId: string) => {
    const change = props.changes.find((item) => item.change_set_id === changeSetId)
    if (change) emit('reject', change)
  },
}

function stopStream() {
  stream?.close()
  stream = null
}

function scopeMatches(generation: number, assistantId: string, projectId: string, documentId: string) {
  return generation === scopeGeneration
    && props.assistantId === assistantId
    && props.projectId === projectId
    && props.tab.project_id === projectId
    && props.tab.document_id === documentId
}

function pushInlineDiffs() {
  const view = editorView.value
  if (!view) return
  view.dispatch({ effects: setInlineDiffs.of(inlineDiffs.value) })
}

function pushFrozenSelection() {
  const view = editorView.value
  if (!view) return
  const range = toolbar.value ? { from: toolbar.value.from, to: toolbar.value.to } : null
  view.dispatch({ effects: setFrozenSelection.of(range) })
}

function updateSelection(view: EditorView) {
  const selection = view.state.selection.main
  if (selection.empty) {
    if (toolbar.value) {
      toolbar.value = null
      pushFrozenSelection()
    }
    return
  }
  const coords = view.coordsAtPos(selection.from)
  const host = editorHost.value?.getBoundingClientRect()
  if (!coords || !host) return
  toolbar.value = {
    from: selection.from,
    to: selection.to,
    left: Math.max(8, coords.left - host.left),
    top: coords.bottom - host.top + 8,
    text: view.state.sliceDoc(selection.from, selection.to),
  }
  pushFrozenSelection()
}

function closeToolbar() {
  toolbar.value = null
  prompt.value = ''
  pushFrozenSelection()
}

function createEditor() {
  if (!editorHost.value) return
  editorView.value = new EditorView({
    state: EditorState.create({
      doc: props.tab.content,
      extensions: [
        basicSetup,
        markdown(),
        frozenSelectionField,
        inlineDiffField(handlers),
        EditorView.updateListener.of((update) => {
          if (update.docChanged && !syncingExternalContent) emit('update', update.state.doc.toString())
          if (update.selectionSet && !syncingExternalContent) updateSelection(update.view)
        }),
      ],
    }),
    parent: editorHost.value,
  })
  pushInlineDiffs()
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
  loading.value = true
  error.value = ''
  try {
    const payload = await apiClient.rewriteSelection({
      assistant_id: assistantId,
      start: utf16ToCodePointOffset(content, selection.from),
      end: utf16ToCodePointOffset(content, selection.to),
      selected_text: selection.text,
      instruction: prompt.value,
      document_version: props.tab.version,
    }, projectId, documentId)
    if (!scopeMatches(generation, assistantId, projectId, documentId)) return
    stopStream()
    let gapped = false
    stream = apiClient.watchTask(assistantId, payload.task_id, (event: TaskEvent) => {
      if (!scopeMatches(generation, assistantId, projectId, documentId)) return
      if (event.type === 'reconnect_gap') {
        // 改写进度出现不可恢复缺口：建议可能已生成但未送达，保留提示直到终态。
        gapped = true
        error.value = '网络中断，改写进度不完整；修改建议可能未送达，可重新生成。'
        return
      }
      if (event.type === 'change_preview') {
        if (!isChangePreview(event.data)) {
          error.value = '任务返回了无效的修改预览'
          loading.value = false
          return
        }
        if (event.data.project_id !== projectId || event.data.document_id !== documentId) return
        emit('preview', event.data)
        closeToolbar()
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
  prompt.value = ''
  error.value = ''
  loading.value = false
  showPreview.value = false
  createEditor()
})
watch(() => [props.tab.version, props.tab.content] as const, () => {
  const view = editorView.value
  if (view && view.state.doc.toString() !== props.tab.content) {
    syncingExternalContent = true
    try {
      view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: props.tab.content } })
    } finally {
      syncingExternalContent = false
    }
  }
  pushInlineDiffs()
})
watch(inlineDiffs, pushInlineDiffs, { deep: true })
</script>

<template>
  <section class="editor-surface">
    <div ref="editorHost" class="code-editor" :class="{ hidden: showPreview }" />
    <MarkdownPreview v-if="showPreview" :content="tab.content" />
    <button class="preview-toggle" title="切换 Markdown 预览" @click="showPreview = !showPreview">{{ showPreview ? '编辑' : '预览' }}</button>
    <SelectionToolbar
      v-if="toolbar"
      v-model="prompt"
      :loading="loading"
      :style="{ left: `${toolbar.left}px`, top: `${toolbar.top}px` }"
      @submit="submitSelection"
      @cancel="closeToolbar"
    />
    <p v-if="staleChangeCount" class="editor-notice">
      有 {{ staleChangeCount }} 处修改建议基于旧版本正文，已停止内联预览，请在右侧 Agent 面板处理。
    </p>
    <p v-if="error" class="editor-error">{{ error }}</p>
  </section>
</template>
