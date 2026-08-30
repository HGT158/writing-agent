<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { Bot, Brain, Pencil, Plus, Save, Trash2 } from '@lucide/vue'

import { apiClient } from './api/client'
import ActivityBar from './components/ActivityBar.vue'
import AgentPanel from './components/AgentPanel.vue'
import AssistantDialog from './components/AssistantDialog.vue'
import CreateProjectDialog from './components/CreateProjectDialog.vue'
import DocumentEditor from './components/DocumentEditor.vue'
import EditorTabs from './components/EditorTabs.vue'
import MemoryProfileDialog from './components/MemoryProfileDialog.vue'
import ProjectExplorer from './components/ProjectExplorer.vue'
import ThemePicker from './components/ThemePicker.vue'
import { createWorkspaceStore } from './stores/workspace'
import type { Assistant, ChangeHunkPreview, ChangeSetPreview, ProjectDocument } from './types'
import { toChangeSetPreview } from './types'

const workspace = createWorkspaceStore()
const assistants = ref<Assistant[]>([])
const activeProjectId = ref<string | null>(null)
const projectTree = ref<ProjectDocument[]>([])
const statusText = ref('就绪')
const globalError = ref('')
const activeSidePanel = ref<'projects' | 'agent' | null>('projects')
const createDialogOpen = ref(false)
const createBusy = ref(false)
const createError = ref('')
const assistantDialogOpen = ref(false)
const assistantBusy = ref(false)
const assistantError = ref('')
const editDialogOpen = ref(false)
const editBusy = ref(false)
const editError = ref('')
const editInitial = ref<{ id: string; name: string; description: string; persona: string } | null>(null)
const memoryDialogOpen = ref(false)
const saving = ref(false)
const documentEditor = ref<InstanceType<typeof DocumentEditor> | null>(null)
let projectRequestGeneration = 0

interface WritebackBaseline {
  projectId: string
  documentId: string
  version: number
  fingerprint: string
}

// 使用正文精确快照作为无碰撞指纹。请求期间只保留一个短生命周期引用，
// 避免散列碰撞把用户新输入误判成“未变化”。
function contentFingerprint(content: string) {
  return content
}

function captureWritebackBaseline(tab: ProjectDocument & { content?: string }): WritebackBaseline {
  return {
    projectId: tab.project_id,
    documentId: tab.document_id,
    version: tab.version,
    fingerprint: contentFingerprint(tab.content || ''),
  }
}

function writeBackServerSnapshot(
  baseline: WritebackBaseline | null,
  document: ProjectDocument,
) {
  if (!baseline) return true
  const current = workspace.getTab(baseline.projectId, baseline.documentId)
  if (!current) return true
  if (
    current.version !== baseline.version
    || contentFingerprint(current.content) !== baseline.fingerprint
  ) {
    globalError.value = '请求期间文档已继续编辑，已保留本地修改；请确认服务端结果后重试。'
    statusText.value = '本地修改已保留'
    return false
  }
  workspace.replaceTab({
    ...current,
    ...document,
    content: document.content || '',
    dirty: false,
  })
  return true
}

/**
 * 待确认 change set（hunk 容器）的唯一状态源：编辑器内联视图与 Agent 面板
 * 卡片都是它的视图；接受/放弃以 hunk 为最小单元，也只经过这里一条通道
 * （架构 §5.10 v1.20）。
 */
const pendingChanges = ref<ChangeSetPreview[]>([])
const reviewingIds = ref<string[]>([])

const activeTab = computed(() => workspace.activeTab)
const agentProjectId = computed(() => activeTab.value?.project_id || activeProjectId.value)
const activeTabChanges = computed(() => {
  const tab = activeTab.value
  if (!tab) return []
  return pendingChanges.value.filter(
    (change) => change.project_id === tab.project_id && change.document_id === tab.document_id,
  )
})
const documentLabels = computed(() => Object.fromEntries(
  projectTree.value.map((item) => [item.document_id, item.relative_path]),
))
/** Agent 面板只显示当前作用域项目的待审卡片（架构 §5.10 v1.21）。 */
const agentProjectChanges = computed(() => pendingChanges.value.filter(
  (change) => change.project_id === agentProjectId.value,
))

function setChatChanges(changes: ChangeSetPreview[], chatSessionId: string | null) {
  pendingChanges.value = [
    ...pendingChanges.value.filter((change) => (
      change.source !== 'chat' || change.chat_session_id !== chatSessionId
    )),
    ...changes,
  ]
}

function addChange(change: ChangeSetPreview) {
  if (pendingChanges.value.some((item) => item.change_set_id === change.change_set_id)) return
  pendingChanges.value = [...pendingChanges.value, change]
}

function markReviewing(changeSetId: string, active: boolean) {
  reviewingIds.value = active
    ? [...reviewingIds.value, changeSetId]
    : reviewingIds.value.filter((item) => item !== changeSetId)
}

async function switchAssistant(assistantId: string) {
  if (workspace.tabs.some((tab) => tab.dirty) && !window.confirm('当前有未保存文档，切换助手会关闭这些标签。继续吗？')) return
  projectRequestGeneration += 1
  activeProjectId.value = null
  projectTree.value = []
  pendingChanges.value = []
  reviewingIds.value = []
  try {
    await workspace.switchAssistant(assistantId)
  } catch (error) {
    globalError.value = error instanceof Error ? error.message : String(error)
  }
}

async function createAssistant(payload: { id: string; name: string; description: string; persona: string }) {
  assistantBusy.value = true
  assistantError.value = ''
  try {
    const created = await apiClient.createAssistant(
      payload.id, payload.name, payload.description, payload.persona,
    )
    assistants.value = await apiClient.listAssistants()
    assistantDialogOpen.value = false
    await switchAssistant(created.id)
  } catch (error) {
    assistantError.value = error instanceof Error ? error.message : String(error)
  } finally {
    assistantBusy.value = false
  }
}

/** 编辑当前助手：先取完整定义（含 persona）再打开对话框；服务端拒绝原样提示。 */
async function openEditAssistant() {
  const current = assistants.value.find((item) => item.id === workspace.assistantId)
  if (!current) return
  editError.value = ''
  try {
    const detail = await apiClient.getAssistant(current.id)
    editInitial.value = {
      id: detail.id,
      name: detail.name,
      description: detail.description,
      persona: detail.persona,
    }
    editDialogOpen.value = true
  } catch (error) {
    globalError.value = error instanceof Error ? error.message : String(error)
  }
}

async function updateAssistant(payload: { id: string; name: string; description: string; persona: string }) {
  editBusy.value = true
  editError.value = ''
  try {
    await apiClient.updateAssistant(payload.id, {
      name: payload.name,
      description: payload.description,
      persona: payload.persona,
    })
    assistants.value = await apiClient.listAssistants()
    editDialogOpen.value = false
    statusText.value = '助手已更新'
  } catch (error) {
    editError.value = error instanceof Error ? error.message : String(error)
  } finally {
    editBusy.value = false
  }
}

async function deleteAssistant() {
  const current = assistants.value.find((item) => item.id === workspace.assistantId)
  if (!current || assistants.value.length < 2) return
  if (!window.confirm(`确定删除助手「${current.name}」吗？其目录会被归档到 data/archive，可手动恢复。`)) return
  try {
    await apiClient.deleteAssistant(current.id)
    assistants.value = await apiClient.listAssistants()
    const next = assistants.value[0]
    if (next) await switchAssistant(next.id)
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
    // 不清空 pending 集合：作用域由活动标签/选中项目决定，卡片按 agentProjectId
    // 过滤展示，切回即恢复（架构 §5.10 v1.21）。
  } catch (error) {
    globalError.value = error instanceof Error ? error.message : String(error)
  }
}

/** 文档重命名/删除成功后刷新活动项目的资源树；失败不阻断，以服务端为准。 */
async function refreshProjectTree(projectId: string) {
  if (activeProjectId.value !== projectId) return
  try {
    projectTree.value = await apiClient.getProjectTree(workspace.assistantId, projectId)
  } catch {
    // 刷新失败不打断操作结果展示。
  }
}

async function renameProjectHandler(projectId: string, name: string) {
  try {
    await apiClient.renameProject(workspace.assistantId, projectId, name)
    await workspace.refreshProjects()
    statusText.value = '项目已重命名'
  } catch (error) {
    globalError.value = error instanceof Error ? error.message : String(error)
  }
}

async function deleteProjectHandler(projectId: string) {
  try {
    await apiClient.deleteProject(workspace.assistantId, projectId)
    for (const tab of workspace.tabs.filter((item) => item.project_id === projectId)) {
      workspace.closeTab(projectId, tab.document_id)
    }
    if (activeProjectId.value === projectId) {
      activeProjectId.value = null
      projectTree.value = []
    }
    await workspace.refreshProjects()
    statusText.value = '项目已归档删除'
  } catch (error) {
    globalError.value = error instanceof Error ? error.message : String(error)
  }
}

async function renameDocumentHandler(projectId: string, documentId: string, relativePath: string) {
  try {
    const updated = await apiClient.renameDocument(workspace.assistantId, projectId, documentId, relativePath)
    await refreshProjectTree(projectId)
    const tab = workspace.getTab(projectId, documentId)
    if (tab) workspace.replaceTab({ ...tab, relative_path: updated.relative_path })
    statusText.value = '文件已重命名'
  } catch (error) {
    globalError.value = error instanceof Error ? error.message : String(error)
  }
}

async function deleteDocumentHandler(projectId: string, documentId: string) {
  try {
    await apiClient.deleteDocument(workspace.assistantId, projectId, documentId)
    workspace.closeTab(projectId, documentId)
    pendingChanges.value = pendingChanges.value.filter(
      (item) => !(item.project_id === projectId && item.document_id === documentId),
    )
    await refreshProjectTree(projectId)
    statusText.value = '文件已删除'
  } catch (error) {
    globalError.value = error instanceof Error ? error.message : String(error)
  }
}

async function openDocument(projectId: string, documentId: string, hunkId?: string) {
  try {
    await workspace.openDocument(projectId, documentId)
    activeSidePanel.value = null
    if (hunkId) {
      await nextTick()
      documentEditor.value?.focusHunk(hunkId)
    }
    void reconcileChanges(projectId, documentId)
  } catch (error) {
    globalError.value = error instanceof Error ? error.message : String(error)
  }
}

/** 页面加载/打开文档时按查询 API 全量分页对账 hunk 级状态（架构 §5.9 v1.20）。 */
async function reconcileChanges(projectId: string, documentId: string) {
  const assistantId = workspace.assistantId
  try {
    const collected: ChangeSetPreview[] = []
    let fetchedCount = 0
    let page = 1
    for (;;) {
      const result = await apiClient.listChangeSets(assistantId, projectId, documentId, page)
      if (workspace.assistantId !== assistantId) return
      fetchedCount += result.items.length
      collected.push(
        ...result.items.filter((set) => set.hunks.some((hunk) => hunk.status === 'pending' || hunk.status === 'stale')),
      )
      if (fetchedCount >= result.total || result.items.length === 0) break
      page += 1
    }
    pendingChanges.value = [
      ...pendingChanges.value.filter(
        (item) => !(item.project_id === projectId && item.document_id === documentId),
      ),
      ...collected,
    ]
  } catch {
    // 对账失败不阻断打开文档；后续操作仍以服务端校验为准。
  }
}

function openCreateProject() {
  createError.value = ''
  createDialogOpen.value = true
}

function openCreateAssistant() {
  assistantError.value = ''
  assistantDialogOpen.value = true
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
  const baseline = captureWritebackBaseline(tab)
  saving.value = true
  statusText.value = '保存中...'
  globalError.value = ''
  try {
    const document = await apiClient.saveDocument(
      workspace.assistantId, tab.project_id, tab.document_id, tab.content, tab.version,
    )
    if (writeBackServerSnapshot(baseline, document)) {
      statusText.value = '已保存'
    }
    void reconcileChanges(tab.project_id, tab.document_id)
  } catch (error) {
    globalError.value = error instanceof Error ? error.message : String(error)
    statusText.value = '保存失败'
  } finally {
    saving.value = false
  }
}

function upsertChangeSet(set: ChangeSetPreview) {
  const openable = set.hunks.some((hunk) => hunk.status === 'pending' || hunk.status === 'stale')
  pendingChanges.value = [
    ...pendingChanges.value.filter((item) => item.change_set_id !== set.change_set_id),
    ...(openable ? [set] : []),
  ]
}

function markChangeSetsStaled(changeSetIds: string[]) {
  if (!changeSetIds.length) return
  pendingChanges.value = pendingChanges.value.map((item) => (
    changeSetIds.includes(item.change_set_id)
      ? {
          ...item,
          hunks: item.hunks.map((hunk) => (
            hunk.status === 'pending' ? { ...hunk, status: 'stale' as const } : hunk
          )),
        }
      : item
  ))
}

function syncAfterMutation(projectId: string, documentId: string) {
  void reconcileChanges(projectId, documentId)
}

async function applyAgentHunk(change: ChangeSetPreview, hunk: ChangeHunkPreview) {
  const tab = workspace.getTab(change.project_id, change.document_id)
  if (tab?.dirty && !window.confirm('当前文档有未保存修改，接受 AI 修改会丢弃这些修改。继续吗？')) return
  const baseline = tab ? captureWritebackBaseline(tab) : null
  markReviewing(change.change_set_id, true)
  try {
    const result = await apiClient.acceptChangeHunk(
      workspace.assistantId,
      change.project_id,
      change.change_set_id,
      hunk.hunk_id,
    )
    const wroteBack = writeBackServerSnapshot(baseline, result.document)
    upsertChangeSet(toChangeSetPreview(result.change_set))
    markChangeSetsStaled(result.staled_change_set_ids)
    if (wroteBack) statusText.value = '已应用修改'
  } catch (error) {
    globalError.value = error instanceof Error ? error.message : String(error)
    syncAfterMutation(change.project_id, change.document_id)
  } finally {
    markReviewing(change.change_set_id, false)
  }
}

async function rejectAgentHunk(change: ChangeSetPreview, hunk: ChangeHunkPreview) {
  markReviewing(change.change_set_id, true)
  try {
    const result = await apiClient.rejectChangeHunk(
      workspace.assistantId,
      change.project_id,
      change.change_set_id,
      hunk.hunk_id,
    )
    upsertChangeSet(toChangeSetPreview(result.change_set))
  } catch (error) {
    globalError.value = error instanceof Error ? error.message : String(error)
    syncAfterMutation(change.project_id, change.document_id)
  } finally {
    markReviewing(change.change_set_id, false)
  }
}

/** 侧栏卡片"全部接受"：服务端按范围倒序串行应用，失配即停、已应用不回滚。 */
async function applyAgentChangeSet(change: ChangeSetPreview) {
  const tab = workspace.getTab(change.project_id, change.document_id)
  if (tab?.dirty && !window.confirm('当前文档有未保存修改，接受 AI 修改会丢弃这些修改。继续吗？')) return
  const baseline = tab ? captureWritebackBaseline(tab) : null
  markReviewing(change.change_set_id, true)
  try {
    const result = await apiClient.acceptAllChangeHunks(
      workspace.assistantId,
      change.project_id,
      change.change_set_id,
    )
    const wroteBack = writeBackServerSnapshot(baseline, result.document)
    upsertChangeSet(toChangeSetPreview(result.change_set))
    markChangeSetsStaled(result.staled_change_set_ids)
    if (result.stopped) {
      // 报告第 P3-3 项：原文案删掉计数变量后残留"第 部分修改建议已失效"。
      globalError.value = '部分修改建议已失效或无法应用，其余建议请逐处确认。'
    } else if (wroteBack) {
      statusText.value = '已应用全部修改'
    }
  } catch (error) {
    globalError.value = error instanceof Error ? error.message : String(error)
    syncAfterMutation(change.project_id, change.document_id)
  } finally {
    markReviewing(change.change_set_id, false)
  }
}

/** 侧栏卡片"全部放弃"：逐个放弃剩余 pending/stale hunk，仅元数据变更。
 *  stale 也必须可放弃：失效建议无法接受，过滤掉 stale 会让失效卡片永远留在侧栏。 */
async function rejectAgentChangeSet(change: ChangeSetPreview) {
  markReviewing(change.change_set_id, true)
  try {
    for (const hunk of change.hunks.filter(
      (item) => item.status === 'pending' || item.status === 'stale',
    )) {
      const result = await apiClient.rejectChangeHunk(
        workspace.assistantId,
        change.project_id,
        change.change_set_id,
        hunk.hunk_id,
      )
      upsertChangeSet(toChangeSetPreview(result.change_set))
    }
  } catch (error) {
    globalError.value = error instanceof Error ? error.message : String(error)
    syncAfterMutation(change.project_id, change.document_id)
  } finally {
    markReviewing(change.change_set_id, false)
  }
}

async function applyAllChanges(changes: ChangeSetPreview[]) {
  const dirtyDocuments = changes.reduce<{ projectId: string; documentId: string; label: string }[]>(
    (items, change) => {
      const tab = workspace.getTab(change.project_id, change.document_id)
      if (!tab?.dirty || items.some((item) => (
        item.projectId === change.project_id && item.documentId === change.document_id
      ))) return items
      items.push({
        projectId: change.project_id,
        documentId: change.document_id,
        label: tab.relative_path,
      })
      return items
    },
    [],
  )
  if (dirtyDocuments.length) {
    const list = dirtyDocuments.map((item) => `- ${item.label}`).join('\n')
    const confirmed = window.confirm(
      `以下文档有未保存修改，全部接受会丢弃这些修改：\n${list}\n\n继续吗？`,
    )
    if (!confirmed) return
  }

  // 一旦某个 change set 失败（例如建议已因版本递增失效）就停下，
  // 避免连续 409 把错误提示刷成噪音，剩余卡片保留以便用户重新生成。
  for (const change of changes) {
    const current = pendingChanges.value.find((item) => item.change_set_id === change.change_set_id)
    if (!current) continue
    let ok = true
    markReviewing(change.change_set_id, true)
    try {
      const tab = workspace.getTab(change.project_id, change.document_id)
      const baseline = tab ? captureWritebackBaseline(tab) : null
      const result = await apiClient.acceptAllChangeHunks(
        workspace.assistantId,
        change.project_id,
        change.change_set_id,
      )
      if (!writeBackServerSnapshot(baseline, result.document)) ok = false
      upsertChangeSet(toChangeSetPreview(result.change_set))
      markChangeSetsStaled(result.staled_change_set_ids)
      if (result.stopped) ok = false
    } catch (error) {
      globalError.value = error instanceof Error ? error.message : String(error)
      syncAfterMutation(change.project_id, change.document_id)
      ok = false
    } finally {
      markReviewing(change.change_set_id, false)
    }
    if (!ok) return
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
      <div class="assistant-picker">
        <span>助手</span>
        <select :value="workspace.assistantId" @change="switchAssistant(($event.target as HTMLSelectElement).value)">
          <option v-for="assistant in assistants" :key="assistant.id" :value="assistant.id">{{ assistant.name }}</option>
        </select>
        <button class="icon-action compact" title="新建助手" @click="openCreateAssistant"><Plus :size="14" /></button>
        <button class="icon-action compact" title="编辑当前助手" @click="openEditAssistant"><Pencil :size="14" /></button>
        <button class="icon-action compact" title="记忆画像（profile.md）" @click="memoryDialogOpen = true"><Brain :size="14" /></button>
        <button
          class="icon-action compact danger"
          title="删除当前助手（归档）"
          :disabled="assistants.length < 2"
          @click="deleteAssistant"
        ><Trash2 :size="14" /></button>
      </div>
      <div class="title-actions">
        <span class="status-text">{{ statusText }}</span>
        <ThemePicker />
        <button class="save-button" title="保存当前文档" :disabled="saving || !activeTab?.dirty" @click="saveActive"><Save :size="15" /> 保存</button>
      </div>
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
        @rename-project="renameProjectHandler"
        @delete-project="deleteProjectHandler"
        @rename-document="renameDocumentHandler"
        @delete-document="deleteDocumentHandler"
        @create-project="openCreateProject"
      />
      <main class="editor-column">
        <EditorTabs :tabs="workspace.tabs" :active-project-id="activeTab?.project_id || null" :active-document-id="activeTab?.document_id || null" @select="workspace.activateTab" @close="closeTab" />
        <DocumentEditor
          v-if="activeTab"
          ref="documentEditor"
          :assistant-id="workspace.assistantId"
          :project-id="activeTab.project_id"
          :tab="activeTab"
          :changes="activeTabChanges"
          :reviewing="reviewingIds"
          @update="workspace.updateActiveContent"
          @preview="addChange"
          @apply="applyAgentHunk"
          @reject="rejectAgentHunk"
        />
        <div v-else class="editor-welcome">
          <h1>写作工作区</h1>
          <p>从左侧新建或导入项目，然后打开 Markdown 或文本文件。</p>
        </div>
      </main>
      <AgentPanel
        :class="{ 'mobile-open': activeSidePanel === 'agent' }"
        :assistant-id="workspace.assistantId"
        :project-id="agentProjectId"
        :document-id="activeTab?.document_id || null"
        :changes="agentProjectChanges"
        :reviewing="reviewingIds"
        :document-labels="documentLabels"
        @apply="applyAgentChangeSet"
        @reject="rejectAgentChangeSet"
        @apply-all="applyAllChanges"
        @changes-loaded="setChatChanges"
        @change-added="addChange"
        @open-document="openDocument"
      />
    </div>
    <CreateProjectDialog v-if="createDialogOpen" :busy="createBusy" :error="createError" @submit="createProject" @cancel="createDialogOpen = false" />
    <AssistantDialog v-if="assistantDialogOpen" :busy="assistantBusy" :error="assistantError" @submit="createAssistant" @cancel="assistantDialogOpen = false" />
    <AssistantDialog
      v-if="editDialogOpen && editInitial"
      mode="edit"
      :busy="editBusy"
      :error="editError"
      :initial="editInitial"
      @submit="updateAssistant"
      @cancel="editDialogOpen = false"
    />
    <MemoryProfileDialog
      v-if="memoryDialogOpen"
      :assistant-id="workspace.assistantId"
      @close="memoryDialogOpen = false"
    />
    <div v-if="globalError || workspace.error" class="global-error">{{ globalError || workspace.error }}</div>
  </div>
</template>
