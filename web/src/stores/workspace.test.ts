import { describe, expect, it, vi } from 'vitest'

import { createWorkspaceStore } from './workspace'
import type { Project, ProjectDocument } from '../types'

const api = {
  listProjects: vi.fn(async (assistantId: string) => [{
    project_id: `${assistantId}-project`,
    assistant_id: assistantId,
    name: `${assistantId} project`,
    root_path: '',
    entry_document_id: `${assistantId}-document`,
  }]),
  getDocument: vi.fn(async (assistantId: string, projectId: string, documentId: string) => ({
    document_id: documentId,
    project_id: projectId,
    assistant_id: assistantId,
    relative_path: 'article.md',
    version: 1,
    editable: true,
    content: '正文',
  })),
}

describe('workspace store', () => {
  it('clears open documents when switching assistants', async () => {
    const store = createWorkspaceStore(api)
    await store.switchAssistant('writer-a')
    await store.openDocument('writer-a-project', 'writer-a-document')
    expect(store.tabs).toHaveLength(1)

    await store.switchAssistant('writer-b')

    expect(store.assistantId).toBe('writer-b')
    expect(store.tabs).toHaveLength(0)
    expect(store.projects[0].assistant_id).toBe('writer-b')
  })

  it('tracks dirty editor content without mutating the server version', async () => {
    const store = createWorkspaceStore(api)
    await store.switchAssistant('writer-a')
    await store.openDocument('writer-a-project', 'writer-a-document')

    store.updateActiveContent('修改后的正文')

    expect(store.activeTab?.content).toBe('修改后的正文')
    expect(store.activeTab?.dirty).toBe(true)
    expect(store.activeTab?.version).toBe(1)
  })

  it('does not mark a tab dirty when the editor content is unchanged', async () => {
    const store = createWorkspaceStore(api)
    await store.switchAssistant('writer-a')
    await store.openDocument('writer-a-project', 'writer-a-document')

    store.updateActiveContent('正文')

    expect(store.activeTab?.dirty).toBe(false)
  })

  it('keeps the previous assistant workspace when loading the next assistant fails', async () => {
    const failingApi = {
      ...api,
      listProjects: vi.fn(async (assistantId: string) => {
        if (assistantId === 'writer-b') throw new Error('加载失败')
        return api.listProjects(assistantId)
      }),
    }
    const store = createWorkspaceStore(failingApi)
    await store.switchAssistant('writer-a')
    await store.openDocument('writer-a-project', 'writer-a-document')

    await expect(store.switchAssistant('writer-b')).rejects.toThrow('加载失败')

    expect(store.assistantId).toBe('writer-a')
    expect(store.tabs).toHaveLength(1)
    expect(store.projects[0].assistant_id).toBe('writer-a')
  })

  it('keeps same document ids separate when they belong to different projects', async () => {
    const store = createWorkspaceStore(api)
    await store.switchAssistant('writer-a')
    await store.openDocument('project-a', 'shared-document')
    await store.openDocument('project-b', 'shared-document')

    expect(store.tabs).toHaveLength(2)
    expect(store.tabs.map((tab) => tab.project_id)).toEqual(['project-a', 'project-b'])
  })

  it('ignores an older assistant switch response', async () => {
    const pending = new Map<string, (projects: Project[]) => void>()
    const racingApi = {
      ...api,
      listProjects: vi.fn((assistantId: string) => new Promise<Project[]>((resolve) => {
        pending.set(assistantId, resolve)
      })),
    }
    const store = createWorkspaceStore(racingApi)
    const first = store.switchAssistant('writer-a')
    const second = store.switchAssistant('writer-b')
    pending.get('writer-b')?.([{ project_id: 'writer-b-project', assistant_id: 'writer-b', name: 'b', root_path: '', entry_document_id: null }])
    pending.get('writer-a')?.([{ project_id: 'writer-a-project', assistant_id: 'writer-a', name: 'a', root_path: '', entry_document_id: null }])
    await Promise.all([first, second])

    expect(store.assistantId).toBe('writer-b')
    expect(store.projects[0].assistant_id).toBe('writer-b')
  })

  it('does not append an old document response after switching assistants', async () => {
    let resolveDocument: (document: ProjectDocument) => void = () => undefined
    const racingApi = {
      ...api,
      getDocument: vi.fn(() => new Promise<ProjectDocument>((resolve) => { resolveDocument = resolve })),
    }
    const store = createWorkspaceStore(racingApi)
    await store.switchAssistant('writer-a')
    const opening = store.openDocument('writer-a-project', 'writer-a-document')
    await store.switchAssistant('writer-b')
    resolveDocument({
      document_id: 'writer-a-document', project_id: 'writer-a-project', assistant_id: 'writer-a',
      relative_path: 'article.md', version: 1, editable: true, content: '旧助手正文',
    })
    await opening

    expect(store.tabs).toHaveLength(0)
    expect(store.assistantId).toBe('writer-b')
  })
})
