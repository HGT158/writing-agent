<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { Bot, Save } from '@lucide/vue'

import { apiClient } from './api/client'
import ActivityBar from './components/ActivityBar.vue'
import AgentPanel from './components/AgentPanel.vue'
import CreateProjectDialog from './components/CreateProjectDialog.vue'
import DocumentEditor from './components/DocumentEditor.vue'
import EditorTabs from './components/EditorTabs.vue'
import ProjectExplorer from './components/ProjectExplorer.vue'
import { createWorkspaceStore } from './stores/workspace'
import type { Assistant, ChangePreview, EditorTab, ProjectDocument } from './types'

const workspace = createWorkspaceStore()
const assistants = ref<Assistant[]>([])
const activeProjectId = ref<string | null>(null)
const projectTree = ref<ProjectDocument[]>([])
const externalChange = ref<ChangePreview | null>(null)
const statusText = ref('就绪')
const globalError = ref('')
const activeSidePanel = ref<'projects' | 'agent' | null>('projects')
const createDialogOpen = ref(false)
const createBusy = ref(false)
const createError = ref('')
const saving = ref(false)
let projectRequestGeneration = 0

const activeTab = computed(() => workspace.activeTab)
const agentProjectId = computed(() => activeTab.value?.project_id || activeProjectId.value)
const visibleExternalChange = computed(() => {
  const change = externalChange.value
  const tab = activeTab.value
  return change && tab && change.project_id === tab.project_id && change.document_id === tab.document_id ? change : null
})

async function switchAssistant(assistantId: string) {
  if (workspace.tabs.some((tab) => tab.dirty) && !window.confirm('当前有未保存文档，切换助手会关闭这些标签。继续吗？')) return
  projectRequestGeneration += 1
  activeProjectId.value = null
  projectTree.value = []
  externalChange.value = null
  try {
    await workspace.switchAssistant(assistantId)
  } catch (error) {
    globalError.value = error instanceof Error ? error.message : String(error)
  }
}

async function selectProject(projectId: string) {
  const generation = ++projectRequestGeneration
  const assistantId = workspace.assistantId
  try {
    const tree = await apiClient.getProjectTree(assistantId, projectId)
    if (generation !== projectRequestGeneration || workspace.assistantId !== assistantId) return
    activeProjectId.value = projectId
    projectTree.value = tree
    externalChange.value = null
  } catch (error) {
    globalError.value = error instanceof Error ? error.message : String(error)
  }
}

async function openDocument(projectId: string, documentId: string) {
  try {
    await workspace.openDocument(projectId, documentId)
    activeSidePanel.value = null
  } catch (error) {
    globalError.value = error instanceof Error ? error.message : String(error)
  }
}

function openCreateProject() {
  createError.value = ''
  createDialogOpen.value = true
}

async function createProject(name: string) {
  createBusy.value = true
  createError.value = ''
  try {
    const project = await apiClient.createProject(workspace.assistantId, name)
    await workspace.refreshProjects()
    await selectProject(project.project_id)
    if (project.entry_document_id) await openDocument(project.project_id, project.entry_document_id)
    createDialogOpen.value = false
  } catch (error) {
    createError.value = error instanceof Error ? error.message : String(error)
  } finally {
    createBusy.value = false
  }
}

function toggleSidePanel(section: 'projects' | 'agent') {
  activeSidePanel.value = activeSidePanel.value === section ? null : section
}

async function imported(projectId: string) {
  try {
    await workspace.refreshProjects()
    await selectProject(projectId)
    const entry = projectTree.value.find((item) => item.editable)
    if (entry) await openDocument(projectId, entry.document_id)
  } catch (error) {
    globalError.value = error instanceof Error ? error.message : String(error)
  }
}

async function saveActive() {
  const tab = workspace.activeTab
  if (!tab || !tab.dirty || saving.value) return
  saving.value = true
  statusText.value = '保存中...'
  globalError.value = ''
  try {
    const document = await apiClient.saveDocument(
      workspace.assistantId, tab.project_id, tab.document_id, tab.content, tab.version,
    )
    workspace.replaceTab({ ...tab, ...document, content: document.content || '', dirty: false })
    statusText.value = '已保存'
  } catch (error) {
    globalError.value = error instanceof Error ? error.message : String(error)
    statusText.value = '保存失败'
  } finally {
    saving.value = false
  }
}

async function applyAgentChange(change: ChangePreview, complete: (success: boolean) => void = () => undefined) {
  const tab = workspace.getTab(change.project_id, change.document_id)
  if (tab?.dirty && !window.confirm('当前文档有未保存修改，接受 AI 修改会丢弃这些修改。继续吗？')) {
    complete(false)
    return
  }
  try {
    const result = await apiClient.applyChange(
      workspace.assistantId,
      change.project_id,
      change.change_set_id,
      change.document_version,
    )
    if (tab) {
      workspace.replaceTab({ ...tab, ...result.document, content: result.document.content || '', dirty: false })
    }
    if (externalChange.value?.change_set_id === change.change_set_id) externalChange.value = null
    complete(true)
  } catch (error) {
    globalError.value = error instanceof Error ? error.message : String(error)
    complete(false)
  }
}

async function rejectAgentChange(change: ChangePreview, complete: (success: boolean) => void = () => undefined) {
  try {
    await apiClient.rejectChange(workspace.assistantId, change.project_id, change.change_set_id)
    if (externalChange.value?.change_set_id === change.change_set_id) externalChange.value = null
    complete(true)
  } catch (error) {
    globalError.value = error instanceof Error ? error.message : String(error)
    complete(false)
  }
}

function closeTab(projectId: string, documentId: string) {
  const tab = workspace.getTab(projectId, documentId)
  if (tab?.dirty && !window.confirm('当前文档有未保存修改，确定关闭吗？')) return
  workspace.closeTab(projectId, documentId)
}

function protectUnload(event: BeforeUnloadEvent) {
  if (!workspace.tabs.some((tab) => tab.dirty)) return
  event.preventDefault()
  event.returnValue = ''
}

onMounted(async () => {
  window.addEventListener('beforeunload', protectUnload)
  try {
    assistants.value = await apiClient.listAssistants()
    if (assistants.value.length) await switchAssistant(assistants.value[0].id)
  } catch (error) {
    globalError.value = error instanceof Error ? error.message : String(error)
  }
})
onBeforeUnmount(() => window.removeEventListener('beforeunload', protectUnload))
</script>

<template>
  <div class="app-shell">
    <header class="title-bar">
      <div class="brand"><Bot :size="17" /><strong>个人写作 Agent</strong></div>
      <label class="assistant-picker"><span>助手</span><select :value="workspace.assistantId" @change="switchAssistant(($event.target as HTMLSelectElement).value)"><option v-for="assistant in assistants" :key="assistant.id" :value="assistant.id">{{ assistant.name }}</option></select></label>
      <div class="title-actions"><span class="status-text">{{ statusText }}</span><button class="save-button" title="保存当前文档 (Ctrl+S)" :disabled="saving || !activeTab?.dirty" @click="saveActive"><Save :size="15" /> 保存</button></div>
    </header>
    <div class="workspace-grid">
      <ActivityBar :active-section="activeSidePanel" @select="toggleSidePanel" />
      <ProjectExplorer
        :class="{ 'mobile-open': activeSidePanel === 'projects' }"
        :assistant-id="workspace.assistantId"
        :projects="workspace.projects"
        :active-project-id="activeProjectId"
        :tree="projectTree"
        @select-project="selectProject"
        @open-document="openDocument"
        @imported="imported"
        @create-project="openCreateProject"
      />
      <main class="editor-column">
        <EditorTabs :tabs="workspace.tabs" :active-project-id="activeTab?.project_id || null" :active-document-id="activeTab?.document_id || null" @select="workspace.activateTab" @close="closeTab" />
        <DocumentEditor
          v-if="activeTab"
          :assistant-id="workspace.assistantId"
          :project-id="activeTab.project_id"
          :tab="activeTab"
          :external-change="visibleExternalChange"
          @update="workspace.updateActiveContent"
          @saved="workspace.replaceTab"
          @preview="externalChange = $event"
          @clear-preview="externalChange = null"
        />
        <div v-else class="editor-welcome"><h1>写作工作区</h1><p>从左侧新建或导入项目，然后打开 Markdown 或文本文件。</p></div>
      </main>
      <AgentPanel
        :class="{ 'mobile-open': activeSidePanel === 'agent' }"
        :assistant-id="workspace.assistantId"
        :project-id="agentProjectId"
        :document-id="activeTab?.document_id || null"
        @apply="applyAgentChange"
        @reject="rejectAgentChange"
      />
    </div>
    <CreateProjectDialog v-if="createDialogOpen" :busy="createBusy" :error="createError" @submit="createProject" @cancel="createDialogOpen = false" />
    <div v-if="globalError || workspace.error" class="global-error">{{ globalError || workspace.error }}</div>
  </div>
</template>
