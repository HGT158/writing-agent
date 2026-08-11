import { reactive } from 'vue'

import { apiClient, type WorkspaceApi } from '../api/client'
import type { EditorTab, Project } from '../types'

export function createWorkspaceStore(api: WorkspaceApi = apiClient) {
  const state = reactive({
    assistantId: '',
    projects: [] as Project[],
    tabs: [] as EditorTab[],
    activeDocumentId: null as string | null,
    activeTabKey: null as string | null,
    busy: false,
    error: '',
  })
  let assistantGeneration = 0

  async function switchAssistant(assistantId: string) {
    const generation = ++assistantGeneration
    state.busy = true
    state.error = ''
    try {
      const projects = await api.listProjects(assistantId)
      if (generation !== assistantGeneration) return
      state.assistantId = assistantId
      state.projects = projects
      state.tabs = []
      state.activeDocumentId = null
      state.activeTabKey = null
    } catch (error) {
      if (generation !== assistantGeneration) return
      state.error = error instanceof Error ? error.message : String(error)
      throw error
    } finally {
      if (generation === assistantGeneration) state.busy = false
    }
  }

  async function openDocument(projectId: string, documentId: string) {
    const assistantId = state.assistantId
    const generation = assistantGeneration
    const existing = state.tabs.find((tab) => tab.project_id === projectId && tab.document_id === documentId)
    if (existing) {
      state.activeDocumentId = documentId
      state.activeTabKey = `${projectId}:${documentId}`
      return existing
    }
    const document = await api.getDocument(assistantId, projectId, documentId)
    if (generation !== assistantGeneration || state.assistantId !== assistantId) return null
    const tab: EditorTab = {
      ...document,
      content: document.content ?? '',
      dirty: false,
    }
    state.tabs.push(tab)
    state.activeDocumentId = documentId
    state.activeTabKey = `${projectId}:${documentId}`
    return tab
  }

  function updateActiveContent(content: string) {
    const tab = state.tabs.find((item) => `${item.project_id}:${item.document_id}` === state.activeTabKey)
    if (!tab) return
    if (tab.content === content) return
    tab.content = content
    tab.dirty = true
  }

  function closeTab(projectId: string, documentId: string) {
    const index = state.tabs.findIndex((tab) => tab.project_id === projectId && tab.document_id === documentId)
    if (index < 0) return
    state.tabs.splice(index, 1)
    if (state.activeTabKey === `${projectId}:${documentId}`) {
      const next = state.tabs.at(-1)
      state.activeDocumentId = next?.document_id ?? null
      state.activeTabKey = next ? `${next.project_id}:${next.document_id}` : null
    }
  }

  function activateTab(projectId: string, documentId: string) {
    if (state.tabs.some((tab) => tab.project_id === projectId && tab.document_id === documentId)) {
      state.activeDocumentId = documentId
      state.activeTabKey = `${projectId}:${documentId}`
    }
  }

  function replaceTab(document: EditorTab) {
    const index = state.tabs.findIndex((tab) => tab.project_id === document.project_id && tab.document_id === document.document_id)
    if (index >= 0) {
      state.tabs[index] = document
      if (state.activeTabKey === `${document.project_id}:${document.document_id}`) {
        state.activeDocumentId = document.document_id
      }
    }
  }

  async function refreshProjects() {
    if (!state.assistantId) return
    const assistantId = state.assistantId
    const generation = assistantGeneration
    const projects = await api.listProjects(assistantId)
    if (generation === assistantGeneration && state.assistantId === assistantId) {
      state.projects = projects
    }
  }

  return {
    state,
    get assistantId() { return state.assistantId },
    get projects() { return state.projects },
    get tabs() { return state.tabs },
    get activeTab() { return state.tabs.find((tab) => `${tab.project_id}:${tab.document_id}` === state.activeTabKey) ?? null },
    getTab(projectId: string, documentId: string) { return state.tabs.find((tab) => tab.project_id === projectId && tab.document_id === documentId) ?? null },
    get busy() { return state.busy },
    get error() { return state.error },
    switchAssistant,
    openDocument,
    updateActiveContent,
    closeTab,
    activateTab,
    replaceTab,
    refreshProjects,
  }
}

export type WorkspaceStore = ReturnType<typeof createWorkspaceStore>
