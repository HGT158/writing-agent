import { flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const project = {
  project_id: 'project-1', assistant_id: 'default', name: '新项目',
  root_path: 'managed/project-1', entry_document_id: 'document-1',
}
const document = {
  document_id: 'document-1', project_id: 'project-1', assistant_id: 'default',
  relative_path: 'article.md', version: 1, editable: true, content: '',
}

// 与 mock 文档的 version 一致，编辑器才会内联渲染而不是降级提示。
const chatChange = {
  change_set_id: 'change-1', project_id: 'project-1', document_id: 'document-1',
  hunks: [{
    hunk_id: 'hunk-1', range: { from: 0, to: 0 },
    original: '', replacement: 'Agent 修改', status: 'pending' as const,
  }],
  document_version: 1, source: 'chat' as const,
}

const appliedRecord = {
  change_set_id: 'change-1', assistant_id: 'default', project_id: 'project-1',
  document_id: 'document-1', session_id: null, source: 'chat' as const,
  task_id: 'task-1', base_version: 1, status: 'applied',
  hunks: [{
    hunk_id: 'hunk-1', change_set_id: 'change-1', display_order: 0,
    start: 0, end: 0, original_text: '', new_text: 'Agent 修改',
    status: 'applied', created_at: '1', applied_at: '2',
  }],
}

const apiMocks = vi.hoisted(() => ({
  listAssistants: vi.fn(), createAssistant: vi.fn(), deleteAssistant: vi.fn(),
  listProjects: vi.fn(), createProject: vi.fn(),
  renameProject: vi.fn(), deleteProject: vi.fn(),
  renameDocument: vi.fn(), deleteDocument: vi.fn(),
  getProjectTree: vi.fn(), getDocument: vi.fn(), saveDocument: vi.fn(),
  acceptChangeHunk: vi.fn(), rejectChangeHunk: vi.fn(),
  acceptAllChangeHunks: vi.fn(), listChangeSets: vi.fn(),
  listProjectChatSessions: vi.fn(), getProjectChatSession: vi.fn(),
  deleteProjectChatSession: vi.fn(), chatProject: vi.fn(), watchTask: vi.fn(),
}))

vi.mock('./api/client', () => ({ apiClient: apiMocks }))

import App from './App.vue'
import type { ChangeSetPreview } from './types'

describe('App project creation', () => {
  beforeEach(() => {
    apiMocks.listAssistants.mockReset().mockResolvedValue([
      { id: 'default', name: '通用写作助手', description: '' },
    ])
    apiMocks.createAssistant.mockReset()
    apiMocks.deleteAssistant.mockReset()
    apiMocks.listProjects.mockReset().mockResolvedValueOnce([]).mockResolvedValue([project])
    apiMocks.createProject.mockReset().mockResolvedValue(project)
    apiMocks.renameProject.mockReset().mockResolvedValue(project)
    apiMocks.deleteProject.mockReset().mockResolvedValue({ archived_path: 'archive/x' })
    apiMocks.renameDocument.mockReset().mockResolvedValue({ ...document, relative_path: 'renamed.md' })
    apiMocks.deleteDocument.mockReset().mockResolvedValue({ deleted: true, entry_document_id: null })
    apiMocks.getProjectTree.mockReset().mockResolvedValue([document])
    apiMocks.getDocument.mockReset().mockResolvedValue(document)
    apiMocks.saveDocument.mockReset()
    apiMocks.acceptChangeHunk.mockReset()
    apiMocks.rejectChangeHunk.mockReset()
    apiMocks.acceptAllChangeHunks.mockReset()
    apiMocks.listChangeSets.mockReset().mockResolvedValue({
      items: [], total: 0, page: 1, page_size: 20,
    })
    apiMocks.listProjectChatSessions.mockReset().mockResolvedValue([])
    apiMocks.getProjectChatSession.mockReset()
    apiMocks.deleteProjectChatSession.mockReset()
    apiMocks.chatProject.mockReset()
    apiMocks.watchTask.mockReset()
  })

  it('disables the save command while a save request is running', async () => {
    let resolveSave: (value: typeof document) => void = () => undefined
    apiMocks.saveDocument.mockReturnValue(
      new Promise((resolve) => { resolveSave = resolve }),
    )
    const wrapper = mount(App)
    await flushPromises()
    await wrapper.get('button[title="新建空白项目"]').trigger('click')
    await wrapper.get('[role="dialog"] input').setValue('新项目')
    await wrapper.get('[role="dialog"] form').trigger('submit')
    await flushPromises()
    const vm = wrapper.vm as unknown as {
      workspace: { updateActiveContent: (content: string) => void }
    }
    vm.workspace.updateActiveContent('待保存正文')
    await nextTick()

    await wrapper.get('.save-button').trigger('click')
    await nextTick()

    expect(wrapper.get('.save-button').attributes('disabled')).toBeDefined()
    resolveSave({ ...document, version: 2, content: '待保存正文' })
    await flushPromises()
  })

  it('keeps keystrokes entered while save is in flight', async () => {
    let resolveSave: (value: typeof document) => void = () => undefined
    apiMocks.saveDocument.mockReturnValue(new Promise((resolve) => { resolveSave = resolve }))
    const wrapper = mount(App)
    await flushPromises()
    await wrapper.get('button[title="新建空白项目"]').trigger('click')
    await wrapper.get('[role="dialog"] input').setValue('新项目')
    await wrapper.get('[role="dialog"] form').trigger('submit')
    await flushPromises()
    const vm = wrapper.vm as unknown as {
      workspace: {
        updateActiveContent: (content: string) => void
        activeTab: { content: string; dirty: boolean; version: number }
      }
      saveActive: () => Promise<void>
    }
    vm.workspace.updateActiveContent('点击保存时的正文')
    const saving = vm.saveActive()
    vm.workspace.updateActiveContent('请求期间继续输入')
    resolveSave({ ...document, version: 2, content: '点击保存时的正文' })
    await saving

    expect(vm.workspace.activeTab.content).toBe('请求期间继续输入')
    expect(vm.workspace.activeTab.dirty).toBe(true)
    expect(vm.workspace.activeTab.version).toBe(1)
    expect(wrapper.get('.global-error').text()).toContain('已保留本地修改')
  })

  it('keeps in-flight keystrokes when accepting one hunk', async () => {
    let resolveApply: (value: Record<string, unknown>) => void = () => undefined
    apiMocks.acceptChangeHunk.mockReturnValue(new Promise((resolve) => { resolveApply = resolve }))
    const wrapper = mount(App)
    await flushPromises()
    await wrapper.get('button[title="新建空白项目"]').trigger('click')
    await wrapper.get('[role="dialog"] input').setValue('新项目')
    await wrapper.get('[role="dialog"] form').trigger('submit')
    await flushPromises()
    const vm = wrapper.vm as unknown as {
      workspace: {
        updateActiveContent: (content: string) => void
        activeTab: { content: string; dirty: boolean }
      }
      applyAgentHunk: (change: typeof chatChange, hunk: (typeof chatChange.hunks)[number]) => Promise<void>
    }
    const applying = vm.applyAgentHunk(chatChange, chatChange.hunks[0])
    vm.workspace.updateActiveContent('接受期间输入')
    resolveApply({
      document: { ...document, version: 2, content: 'Agent 修改' },
      change_set: appliedRecord,
      hunk: appliedRecord.hunks[0],
      staled_change_set_ids: [],
    })
    await applying

    expect(vm.workspace.activeTab.content).toBe('接受期间输入')
    expect(vm.workspace.activeTab.dirty).toBe(true)
  })

  it('keeps in-flight keystrokes when accepting a whole change set', async () => {
    let resolveApply: (value: Record<string, unknown>) => void = () => undefined
    apiMocks.acceptAllChangeHunks.mockReturnValue(new Promise((resolve) => { resolveApply = resolve }))
    const wrapper = mount(App)
    await flushPromises()
    await wrapper.get('button[title="新建空白项目"]').trigger('click')
    await wrapper.get('[role="dialog"] input').setValue('新项目')
    await wrapper.get('[role="dialog"] form').trigger('submit')
    await flushPromises()
    const vm = wrapper.vm as unknown as {
      workspace: {
        updateActiveContent: (content: string) => void
        activeTab: { content: string; dirty: boolean }
      }
      applyAgentChangeSet: (change: typeof chatChange) => Promise<void>
    }
    const applying = vm.applyAgentChangeSet(chatChange)
    vm.workspace.updateActiveContent('整组接受期间输入')
    resolveApply({
      document: { ...document, version: 2, content: 'Agent 修改' },
      change_set: appliedRecord,
      applied_hunk_ids: ['hunk-1'],
      stopped: null,
      staled_change_set_ids: [],
    })
    await applying

    expect(vm.workspace.activeTab.content).toBe('整组接受期间输入')
    expect(vm.workspace.activeTab.dirty).toBe(true)
  })

  it('reuses the same in-flight guard for project-level accept all', async () => {
    let resolveApply: (value: Record<string, unknown>) => void = () => undefined
    apiMocks.acceptAllChangeHunks.mockReturnValue(new Promise((resolve) => { resolveApply = resolve }))
    const wrapper = mount(App)
    await flushPromises()
    await wrapper.get('button[title="新建空白项目"]').trigger('click')
    await wrapper.get('[role="dialog"] input').setValue('新项目')
    await wrapper.get('[role="dialog"] form').trigger('submit')
    await flushPromises()
    const vm = wrapper.vm as unknown as {
      workspace: {
        updateActiveContent: (content: string) => void
        activeTab: { content: string; dirty: boolean }
      }
      pendingChanges: (typeof chatChange)[]
      applyAllChanges: (changes: (typeof chatChange)[]) => Promise<void>
    }
    vm.pendingChanges = [chatChange]
    const applying = vm.applyAllChanges([chatChange])
    vm.workspace.updateActiveContent('批量接受期间输入')
    resolveApply({
      document: { ...document, version: 2, content: 'Agent 修改' },
      change_set: appliedRecord,
      applied_hunk_ids: ['hunk-1'],
      stopped: null,
      staled_change_set_ids: [],
    })
    await applying

    expect(vm.workspace.activeTab.content).toBe('批量接受期间输入')
    expect(vm.workspace.activeTab.dirty).toBe(true)
  })

  it('creates a project through an in-app dialog', async () => {
    const wrapper = mount(App)
    await flushPromises()

    await wrapper.get('button[title="新建空白项目"]').trigger('click')
    await wrapper.get('[role="dialog"] input').setValue('  新项目  ')
    await wrapper.get('[role="dialog"] form').trigger('submit')
    await flushPromises()

    expect(apiMocks.createProject).toHaveBeenCalledWith('default', '新项目')
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('article.md')
  })

  it('ignores an older project-tree response', async () => {
    const resolvers = new Map<string, (documents: (typeof document)[]) => void>()
    apiMocks.getProjectTree.mockImplementation(
      (_assistantId: string, projectId: string) => new Promise((resolve) => {
        resolvers.set(projectId, resolve)
      }),
    )
    const wrapper = mount(App)
    await flushPromises()
    const vm = wrapper.vm as unknown as {
      selectProject: (projectId: string) => Promise<void>
      activeProjectId: string | null
      projectTree: (typeof document)[]
    }

    const first = vm.selectProject('project-old')
    const second = vm.selectProject('project-new')
    resolvers.get('project-new')?.([{ ...document, project_id: 'project-new', document_id: 'new-doc' }])
    resolvers.get('project-old')?.([{ ...document, project_id: 'project-old', document_id: 'old-doc' }])
    await Promise.all([first, second])

    expect(vm.activeProjectId).toBe('project-new')
    expect(vm.projectTree[0].project_id).toBe('project-new')
  })

  it('accepts every hunk of a chat change set when its target document is not open', async () => {
    apiMocks.acceptAllChangeHunks.mockResolvedValue({
      document: { ...document, version: 3, content: 'Agent 修改' },
      change_set: appliedRecord,
      applied_hunk_ids: ['hunk-1'],
      stopped: null,
      staled_change_set_ids: [],
    })
    const wrapper = mount(App)
    await flushPromises()
    const vm = wrapper.vm as unknown as {
      applyAgentChangeSet: (change: Record<string, unknown>) => Promise<void>
    }

    await vm.applyAgentChangeSet({
      change_set_id: 'change-1', project_id: 'project-1', document_id: 'document-1',
      hunks: [{
        hunk_id: 'hunk-1', range: { from: 0, to: 0 },
        original: '', replacement: 'Agent 修改', status: 'pending',
      }],
      document_version: 2, source: 'chat',
    })

    expect(apiMocks.acceptAllChangeHunks).toHaveBeenCalledWith(
      'default', 'project-1', 'change-1',
    )
  })

  it('confirms all dirty target documents before accepting any change set', async () => {
    const secondDocument = {
      ...document, document_id: 'document-2', relative_path: 'notes.md',
    }
    apiMocks.getDocument.mockImplementation(
      (_assistantId: string, _projectId: string, documentId: string) => Promise.resolve(
        documentId === 'document-2' ? secondDocument : document,
      ),
    )
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    const wrapper = mount(App)
    await flushPromises()
    const vm = wrapper.vm as unknown as {
      openDocument: (projectId: string, documentId: string) => Promise<void>
      applyAllChanges: (changes: Record<string, unknown>[]) => Promise<void>
      workspace: { updateActiveContent: (content: string) => void }
      pendingChanges: Record<string, unknown>[]
    }
    await vm.openDocument('project-1', 'document-1')
    vm.workspace.updateActiveContent('未保存正文')
    await vm.openDocument('project-1', 'document-2')
    vm.workspace.updateActiveContent('未保存笔记')
    const changes = [
      chatChange,
      { ...chatChange, change_set_id: 'change-2', document_id: 'document-2' },
    ]
    vm.pendingChanges = changes

    await vm.applyAllChanges(changes)

    expect(confirm).toHaveBeenCalledOnce()
    expect(confirm.mock.calls[0][0]).toContain('article.md')
    expect(confirm.mock.calls[0][0]).toContain('notes.md')
    expect(apiMocks.acceptAllChangeHunks).not.toHaveBeenCalled()
    confirm.mockRestore()
  })

  it('can dismiss a fully stale change set through reject-all', async () => {    apiMocks.rejectChangeHunk.mockResolvedValue({
      change_set: {
        ...appliedRecord, status: 'rejected',
        hunks: [{ ...appliedRecord.hunks[0], status: 'rejected' }],
      },
    })
    const wrapper = mount(App)
    await flushPromises()
    const vm = wrapper.vm as unknown as {
      rejectAgentChangeSet: (change: Record<string, unknown>) => Promise<void>
    }

    await vm.rejectAgentChangeSet({
      change_set_id: 'change-1', project_id: 'project-1', document_id: 'document-1',
      hunks: [{
        hunk_id: 'hunk-1', range: { from: 0, to: 2 },
        original: '原文', replacement: '改写', status: 'stale',
      }],
      document_version: 1, source: 'chat',
    })

    // stale hunk 也必须可放弃：失效建议无法接受，若连放弃都被过滤掉，
    // 卡片将永远留在侧栏（用户实测反馈的卡死）。
    expect(apiMocks.rejectChangeHunk).toHaveBeenCalledWith(
      'default', 'project-1', 'change-1', 'hunk-1',
    )
  })

  it('renames a document through the explorer channel and refreshes the tree', async () => {
    const wrapper = mount(App)
    await flushPromises()
    const vm = wrapper.vm as unknown as {
      selectProject: (projectId: string) => Promise<void>
      renameDocumentHandler: (projectId: string, documentId: string, path: string) => Promise<void>
    }
    await vm.selectProject('project-1')
    await flushPromises()

    await vm.renameDocumentHandler('project-1', 'document-1', 'renamed.md')

    expect(apiMocks.renameDocument).toHaveBeenCalledWith(
      'default', 'project-1', 'document-1', 'renamed.md',
    )
    expect(apiMocks.getProjectTree).toHaveBeenCalledWith('default', 'project-1')
  })

  it('deletes a document, closes its tab and refreshes the tree', async () => {
    const wrapper = mount(App)
    await flushPromises()
    const vm = wrapper.vm as unknown as {
      selectProject: (projectId: string) => Promise<void>
      openDocument: (projectId: string, documentId: string) => Promise<void>
      deleteDocumentHandler: (projectId: string, documentId: string) => Promise<void>
      workspace: { tabs: { document_id: string }[] }
    }
    await vm.selectProject('project-1')
    await vm.openDocument('project-1', 'document-1')
    await flushPromises()
    expect(vm.workspace.tabs.length).toBe(1)

    await vm.deleteDocumentHandler('project-1', 'document-1')
    await flushPromises()

    expect(apiMocks.deleteDocument).toHaveBeenCalledWith('default', 'project-1', 'document-1')
    expect(vm.workspace.tabs.length).toBe(0)
  })

  it('accepts a single hunk without requiring the open tab version', async () => {
    apiMocks.acceptChangeHunk.mockResolvedValue({
      document: { ...document, version: 3, content: 'Agent 修改' },
      change_set: appliedRecord,
      hunk: appliedRecord.hunks[0],
      staled_change_set_ids: [],
    })
    const wrapper = mount(App)
    await flushPromises()
    await wrapper.get('button[title="新建空白项目"]').trigger('click')
    await wrapper.get('[role="dialog"] input').setValue('新项目')
    await wrapper.get('[role="dialog"] form').trigger('submit')
    await flushPromises()
    const vm = wrapper.vm as unknown as {
      applyAgentHunk: (
        change: Record<string, unknown>, hunk: Record<string, unknown>,
      ) => Promise<void>
    }

    await vm.applyAgentHunk({
      change_set_id: 'change-1', project_id: 'project-1', document_id: 'document-1',
      hunks: [{
        hunk_id: 'hunk-1', range: { from: 0, to: 0 },
        original: '', replacement: 'Agent 修改', status: 'pending',
      }],
      document_version: 2, source: 'chat',
    }, { hunk_id: 'hunk-1' })

    expect(apiMocks.acceptChangeHunk).toHaveBeenCalledWith(
      'default', 'project-1', 'change-1', 'hunk-1',
    )
  })

  it('shows one pending change in both the editor and the agent panel', async () => {
    const wrapper = mount(App)
    await flushPromises()
    await wrapper.get('button[title="新建空白项目"]').trigger('click')
    await wrapper.get('[role="dialog"] input').setValue('新项目')
    await wrapper.get('[role="dialog"] form').trigger('submit')
    await flushPromises()

    wrapper.findComponent({ name: 'AgentPanel' }).vm.$emit('changeAdded', chatChange)
    await flushPromises()

    expect(wrapper.findAll('.change-diff')).toHaveLength(1)
    expect(wrapper.get('.cm-diff-inserted').text()).toBe('Agent 修改')
  })

  it('removes the change from both views once the parent applies it', async () => {
    apiMocks.acceptChangeHunk.mockResolvedValue({
      document: { ...document, version: 3, content: 'Agent 修改' },
      change_set: appliedRecord,
      hunk: appliedRecord.hunks[0],
      staled_change_set_ids: [],
    })
    const wrapper = mount(App)
    await flushPromises()
    await wrapper.get('button[title="新建空白项目"]').trigger('click')
    await wrapper.get('[role="dialog"] input').setValue('新项目')
    await wrapper.get('[role="dialog"] form').trigger('submit')
    await flushPromises()
    wrapper.findComponent({ name: 'AgentPanel' }).vm.$emit('changeAdded', chatChange)
    await flushPromises()

    await wrapper.get('.cm-diff-accept').trigger('click')
    await flushPromises()

    expect(apiMocks.acceptChangeHunk).toHaveBeenCalledWith('default', 'project-1', 'change-1', 'hunk-1')
    expect(wrapper.find('.change-diff').exists()).toBe(false)
    expect(wrapper.find('.cm-diff-inserted').exists()).toBe(false)
  })

  it('keeps the change in both views when applying fails', async () => {
    apiMocks.acceptChangeHunk.mockRejectedValue(new Error('修改位置已变化，该 hunk 已失效'))
    // 失败后对账以查询 API 为真相源：服务端仍保留 pending 建议。
    apiMocks.listChangeSets.mockResolvedValue({
      items: [chatChange], total: 1, page: 1, page_size: 20,
    })
    const wrapper = mount(App)
    await flushPromises()
    await wrapper.get('button[title="新建空白项目"]').trigger('click')
    await wrapper.get('[role="dialog"] input').setValue('新项目')
    await wrapper.get('[role="dialog"] form').trigger('submit')
    await flushPromises()
    wrapper.findComponent({ name: 'AgentPanel' }).vm.$emit('changeAdded', chatChange)
    await flushPromises()

    await wrapper.get('.cm-diff-accept').trigger('click')
    await flushPromises()

    expect(wrapper.findAll('.change-diff')).toHaveLength(1)
    expect(wrapper.get('.global-error').text()).toContain('已失效')
  })

  it('scopes pending change cards to the active agent project', async () => {
    const wrapper = mount(App)
    await flushPromises()
    await wrapper.get('button[title="新建空白项目"]').trigger('click')
    await wrapper.get('[role="dialog"] input').setValue('新项目')
    await wrapper.get('[role="dialog"] form').trigger('submit')
    await flushPromises()

    const otherProjectChange = {
      change_set_id: 'change-other', project_id: 'project-other', document_id: 'doc-other',
      hunks: [{
        hunk_id: 'h-other', range: { from: 0, to: 0 },
        original: '', replacement: '别处修改', status: 'pending',
      }],
      document_version: 1, source: 'chat',
    }
    wrapper.findComponent({ name: 'AgentPanel' }).vm.$emit('changeAdded', otherProjectChange)
    await flushPromises()
    expect(wrapper.find('.change-diff').exists()).toBe(false)  // 其他项目的卡片不显示

    wrapper.findComponent({ name: 'AgentPanel' }).vm.$emit('changeAdded', chatChange)
    await flushPromises()
    expect(wrapper.findAll('.change-diff')).toHaveLength(1)
  })

  it('keeps pending cards when the explorer selects another project', async () => {
    const wrapper = mount(App)
    await flushPromises()
    await wrapper.get('button[title="新建空白项目"]').trigger('click')
    await wrapper.get('[role="dialog"] input').setValue('新项目')
    await wrapper.get('[role="dialog"] form').trigger('submit')
    await flushPromises()
    wrapper.findComponent({ name: 'AgentPanel' }).vm.$emit('changeAdded', chatChange)
    await flushPromises()
    expect(wrapper.findAll('.change-diff')).toHaveLength(1)

    // 资源管理器切到其他项目：活动标签仍在 project-1，卡片不得被清空。
    const vm = wrapper.vm as unknown as { selectProject: (projectId: string) => Promise<void> }
    await vm.selectProject('project-other')
    await flushPromises()
    expect(wrapper.findAll('.change-diff')).toHaveLength(1)
  })

  it('forwards a sidebar hunk navigation request to the active editor', async () => {
    const wrapper = mount(App)
    await flushPromises()
    await wrapper.get('button[title="新建空白项目"]').trigger('click')
    await wrapper.get('[role="dialog"] input').setValue('新项目')
    await wrapper.get('[role="dialog"] form').trigger('submit')
    await flushPromises()

    const editor = wrapper.findComponent({ name: 'DocumentEditor' })
    const exposed = (editor.vm as unknown as { $: { exposed: { focusHunk: (id: string) => void } } }).$.exposed
    const focusHunk = vi.spyOn(
      exposed,
      'focusHunk',
    )
    wrapper.findComponent({ name: 'AgentPanel' }).vm.$emit(
      'openDocument', 'project-1', 'document-1', 'hunk-1',
    )
    await flushPromises()

    expect(focusHunk).toHaveBeenCalledWith('hunk-1')
  })

  it('creates an assistant and switches to it', async () => {
    apiMocks.createAssistant.mockResolvedValue({ id: 'marketing', name: '营销文案', description: '' })
    apiMocks.listAssistants
      .mockResolvedValueOnce([{ id: 'default', name: '通用写作助手', description: '' }])
      .mockResolvedValue([
        { id: 'default', name: '通用写作助手', description: '' },
        { id: 'marketing', name: '营销文案', description: '' },
      ])
    const wrapper = mount(App)
    await flushPromises()

    await wrapper.get('button[title="新建助手"]').trigger('click')
    await wrapper.get('#assistant-id').setValue('marketing')
    await wrapper.get('#assistant-name').setValue('营销文案')
    await wrapper.get('[role="dialog"] form').trigger('submit')
    await flushPromises()

    expect(apiMocks.createAssistant).toHaveBeenCalledWith('marketing', '营销文案', '')
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
    expect(apiMocks.listProjects).toHaveBeenLastCalledWith('marketing')
  })

  it('keeps the assistant dialog open and shows the server error', async () => {
    apiMocks.createAssistant.mockRejectedValue(new Error('助手已存在'))
    const wrapper = mount(App)
    await flushPromises()

    await wrapper.get('button[title="新建助手"]').trigger('click')
    await wrapper.get('#assistant-id').setValue('default')
    await wrapper.get('#assistant-name').setValue('重复助手')
    await wrapper.get('[role="dialog"] form').trigger('submit')
    await flushPromises()

    expect(wrapper.find('[role="dialog"]').exists()).toBe(true)
    expect(wrapper.get('[role="dialog"] .inline-error').text()).toBe('助手已存在')
  })

  it('refuses to delete the only assistant', async () => {
    const wrapper = mount(App)
    await flushPromises()

    expect(wrapper.get('button[title="删除当前助手（归档）"]').attributes('disabled')).toBeDefined()
    expect(apiMocks.deleteAssistant).not.toHaveBeenCalled()
  })

  it('archives the current assistant after confirmation', async () => {
    apiMocks.listAssistants.mockResolvedValue([
      { id: 'default', name: '通用写作助手', description: '' },
      { id: 'marketing', name: '营销文案', description: '' },
    ])
    apiMocks.deleteAssistant.mockResolvedValue({ archived_path: 'archive/default-1', purged: false })
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const wrapper = mount(App)
    await flushPromises()

    await wrapper.get('button[title="删除当前助手（归档）"]').trigger('click')
    await flushPromises()

    expect(apiMocks.deleteAssistant).toHaveBeenCalledWith('default')
    vi.mocked(window.confirm).mockRestore()
  })

  it('ends change-set reconciliation after every server item has been fetched', async () => {
    apiMocks.listChangeSets
      .mockResolvedValueOnce({
        items: [{ ...chatChange, change_set_id: 'old', hunks: [{ ...chatChange.hunks[0], status: 'applied' }] }],
        total: 2, page: 1, page_size: 1,
      })
      .mockResolvedValueOnce({ items: [chatChange], total: 2, page: 2, page_size: 1 })
    const wrapper = mount(App)
    await flushPromises()
    const vm = wrapper.vm as unknown as {
      reconcileChanges: (projectId: string, documentId: string) => Promise<void>
    }

    await vm.reconcileChanges('project-1', 'document-1')

    expect(apiMocks.listChangeSets).toHaveBeenCalledTimes(2)
  })

  it('replaces chat cards only within the loaded session scope', async () => {
    const wrapper = mount(App)
    await flushPromises()
    const vm = wrapper.vm as unknown as {
      pendingChanges: ChangeSetPreview[]
      setChatChanges: (changes: ChangeSetPreview[], sessionId: string) => void
    }
    vm.pendingChanges = [
      { ...chatChange, change_set_id: 'session-a', chat_session_id: 'a' },
      { ...chatChange, change_set_id: 'session-b', chat_session_id: 'b' },
    ]

    vm.setChatChanges([], 'a')

    expect(vm.pendingChanges.map((item) => item.change_set_id)).toEqual(['session-b'])
  })
})
