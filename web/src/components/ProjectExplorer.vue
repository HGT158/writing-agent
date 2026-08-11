<script setup lang="ts">
import { computed, ref } from 'vue'
import { FilePlus2, FolderArchive, FolderPlus, Upload } from '@lucide/vue'

import { apiClient } from '../api/client'
import type { Project, ProjectDocument } from '../types'

const props = defineProps<{
  assistantId: string
  projects: Project[]
  activeProjectId: string | null
  tree: ProjectDocument[]
}>()

const emit = defineEmits<{
  selectProject: [projectId: string]
  openDocument: [projectId: string, documentId: string]
  imported: [projectId: string]
  createProject: []
}>()

const fileInput = ref<HTMLInputElement>()
const folderInput = ref<HTMLInputElement>()
const importBusy = ref(false)
const importError = ref('')
const editableTree = computed(() => props.tree.filter((item) => item.editable))

async function importFile(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  importBusy.value = true
  importError.value = ''
  try {
    const project = await apiClient.importFile(props.assistantId, file)
    emit('imported', project.project_id)
  } catch (error) {
    importError.value = error instanceof Error ? error.message : String(error)
  } finally {
    importBusy.value = false
    input.value = ''
  }
}

async function importFolder(event: Event) {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files || [])
  if (!files.length) return
  const firstPath = (files[0] as File & { webkitRelativePath?: string }).webkitRelativePath || files[0].name
  const name = firstPath.split('/')[0] || files[0].name
  importBusy.value = true
  importError.value = ''
  try {
    const project = await apiClient.importFolder(props.assistantId, name, files)
    emit('imported', project.project_id)
  } catch (error) {
    importError.value = error instanceof Error ? error.message : String(error)
  } finally {
    importBusy.value = false
    input.value = ''
  }
}
</script>

<template>
  <aside class="explorer-panel">
    <div class="panel-heading">
      <span>项目</span>
      <div class="heading-actions">
        <button title="新建空白项目" :disabled="importBusy" @click="emit('createProject')"><FilePlus2 :size="15" /></button>
        <button title="导入文本文件" :disabled="importBusy" @click="fileInput?.click()"><Upload :size="15" /></button>
        <button title="导入文件夹" :disabled="importBusy" @click="folderInput?.click()"><FolderPlus :size="15" /></button>
      </div>
    </div>
    <input ref="fileInput" class="visually-hidden" type="file" accept=".md,.markdown,.txt" @change="importFile" />
    <input ref="folderInput" class="visually-hidden" type="file" multiple webkitdirectory directory @change="importFolder" />
    <p v-if="importError" class="inline-error">{{ importError }}</p>
    <div class="project-list">
      <button
        v-for="project in projects"
        :key="project.project_id"
        class="project-row"
        :class="{ selected: project.project_id === activeProjectId }"
        @click="emit('selectProject', project.project_id)"
      >
        <span class="tree-chevron">{{ project.project_id === activeProjectId ? '⌄' : '›' }}</span>
        <span class="project-name">{{ project.name }}</span>
        <span v-if="project.project_id === activeProjectId" class="project-count">{{ editableTree.length }}</span>
      </button>
      <div v-if="activeProjectId" class="file-tree">
        <button v-for="document in tree" :key="document.document_id" class="file-row" :disabled="!document.editable" @click="document.editable && emit('openDocument', activeProjectId!, document.document_id)">
          <span class="file-indent">{{ document.relative_path.includes('/') ? '└' : '' }}</span>
          <span class="file-label">{{ document.relative_path }}</span>
          <span v-if="!document.editable" class="readonly-mark" title="只读文件"><FolderArchive :size="13" /></span>
        </button>
        <p v-if="!tree.length" class="empty-hint">项目为空</p>
      </div>
    </div>
    <p v-if="!projects.length" class="empty-hint">新建或导入第一个项目</p>
  </aside>
</template>
