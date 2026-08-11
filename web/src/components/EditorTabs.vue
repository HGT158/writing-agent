<script setup lang="ts">
import { X } from '@lucide/vue'
import type { EditorTab } from '../types'

defineProps<{ tabs: EditorTab[]; activeProjectId: string | null; activeDocumentId: string | null }>()
defineEmits<{ select: [projectId: string, documentId: string]; close: [projectId: string, documentId: string] }>()
</script>

<template>
  <div class="editor-tabs" role="tablist" aria-label="打开的文档">
    <button v-for="tab in tabs" :key="`${tab.project_id}:${tab.document_id}`" class="editor-tab" :class="{ active: tab.project_id === activeProjectId && tab.document_id === activeDocumentId }" role="tab" @click="$emit('select', tab.project_id, tab.document_id)">
      <span>{{ tab.relative_path }}</span><span v-if="tab.dirty" class="dirty-dot">●</span>
      <span class="tab-close" title="关闭文档" @click.stop="$emit('close', tab.project_id, tab.document_id)"><X :size="14" /></span>
    </button>
    <span v-if="!tabs.length" class="tabs-empty">选择一个文档开始写作</span>
  </div>
</template>
