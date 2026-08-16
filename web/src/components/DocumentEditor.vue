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
import {
  isChangePreview,
  type ChangeHunkPreview,
  type ChangePreview,
  type ChangeSetPreview,
  type EditorTab,
  type TaskEvent,
} from '../types'
import { codePointToUtf16Offset, utf16ToCodePointOffset } from '../utils/unicodeOffsets'
import MarkdownPreview from './MarkdownPreview.vue'
import SelectionToolbar from './SelectionToolbar.vue'

const props = defineProps<{
  assistantId: string
  projectId: string
  tab: EditorTab
  changes: ChangeSetPreview[]
  reviewing: string[]
}>()
const emit = defineEmits<{
  update: [content: string]
  preview: [change: ChangeSetPreview]
  apply: [change: ChangeSetPreview, hunk: ChangeHunkPreview]
  reject: [change: ChangeSetPreview, hunk: ChangeHunkPreview]
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
 * 内联 diff 逐 hunk 定位（架构 §5.10 v1.20）：标签页正文等于基准版本时按存储
 * 范围对齐；版本推进后以 hunk 原文在当前正文唯一匹配重定位（服务端内容复检
 * 的客户端镜像）；dirty 或无法定位的 hunk 计入降级提示，交给侧栏处理。
 */
const locatedHunks = computed<{ diffs: InlineDiff[]; staleCount: number }>(() => {
  const reviewing = new Set(props.reviewing)
  const diffs: InlineDiff[] = []
  let staleCount = 0
  if (!props.tab.dirty) {
    const content = props.tab.content
    for (const change of props.changes) {
      for (const hunk of change.hunks) {
        if (hunk.status !== 'pending') {
          if (hunk.status === 'stale') staleCount += 1
          continue
        }
        let from: number
        let to: number
        if (change.document_version === props.tab.version) {
          from = codePointToUtf16Offset(content, hunk.range.from)
          to = codePointToUtf16Offset(content, hunk.range.to)
        } else {
          const index = content.indexOf(hunk.original)
          if (index < 0 || content.indexOf(hunk.original, index + 1) >= 0) {
            staleCount += 1
            continue
          }
          from = index
          to = index + hunk.original.length
        }
        diffs.push({
          changeSetId: change.change_set_id,
          hunkId: hunk.hunk_id,
          from,
          to,
          replacement: hunk.replacement,
          busy: reviewing.has(change.change_set_id),
        })
      }
    }
    diffs.sort((a, b) => a.from - b.from)
  } else {
    staleCount = props.changes.reduce(
      (total, change) => total + change.hunks.filter(
        (hunk) => hunk.status === 'pending' || hunk.status === 'stale',
      ).length,
      0,
    )
  }
  return { diffs, staleCount }
})

const inlineDiffs = computed(() => locatedHunks.value.diffs)
const staleChangeCount = computed(() => locatedHunks.value.staleCount)

const handlers = {
  accept: (changeSetId: string, hunkId: string) => {
    const change = props.changes.find((item) => item.change_set_id === changeSetId)
    const hunk = change?.hunks.find((item) => item.hunk_id === hunkId)
    if (change && hunk) emit('apply', change, hunk)
  },
  reject: (changeSetId: string, hunkId: string) => {
    const change = props.changes.find((item) => item.change_set_id === changeSetId)
    const hunk = change?.hunks.find((item) => item.hunk_id === hunkId)
    if (change && hunk) emit('reject', change, hunk)
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
      有 {{ staleChangeCount }} 处修改建议无法内联预览（已失效或正文已变化），请在右侧 Agent 面板处理。
    </p>
    <p v-if="error" class="editor-error">{{ error }}</p>
  </section>
</template>
