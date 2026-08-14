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
  range: { from: 0, to: 0 }, original: '', replacement: 'Agent 修改',
  document_version: 1, source: 'chat' as const,
}

const apiMocks = vi.hoisted(() => ({
  listAssistants: vi.fn(), createAssistant: vi.fn(), deleteAssistant: vi.fn(),
  listProjects: vi.fn(), createProject: vi.fn(),
  getProjectTree: vi.fn(), getDocument: vi.fn(), saveDocument: vi.fn(),
  applyChange: vi.fn(), rejectChange: vi.fn(),
  listProjectChatSessions: vi.fn(), getProjectChatSession: vi.fn(),
  deleteProjectChatSession: vi.fn(), chatProject: vi.fn(), watchTask: vi.fn(),
}))

vi.mock('./api/client', () => ({ apiClient: apiMocks }))

import App from './App.vue'

describe('App project creation', () => {
  beforeEach(() => {
    apiMocks.listAssistants.mockReset().mockResolvedValue([
      { id: 'default', name: '通用写作助手', description: '' },
    ])
    apiMocks.createAssistant.mockReset()
    apiMocks.deleteAssistant.mockReset()
    apiMocks.listProjects.mockReset().mockResolvedValueOnce([]).mockResolvedValue([project])
    apiMocks.createProject.mockReset().mockResolvedValue(project)
    apiMocks.getProjectTree.mockReset().mockResolvedValue([document])
    apiMocks.getDocument.mockReset().mockResolvedValue(document)
    apiMocks.saveDocument.mockReset()
    apiMocks.applyChange.mockReset()
    apiMocks.rejectChange.mockReset()
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

  it('applies a chat change when its target document is not open', async () => {
    apiMocks.applyChange.mockResolvedValue({
      document: { ...document, version: 3, content: 'Agent 修改' },
      change_set: { change_set_id: 'change-1', status: 'applied' },
    })
    const wrapper = mount(App)
    await flushPromises()
    const vm = wrapper.vm as unknown as {
      applyAgentChange: (
        change: Record<string, unknown>, complete: (success: boolean) => void,
      ) => Promise<void>
    }
    const complete = vi.fn()

    await vm.applyAgentChange({
      change_set_id: 'change-1', project_id: 'project-1', document_id: 'document-1',
      range: { from: 0, to: 0 }, original: '', replacement: 'Agent 修改',
      document_version: 2, source: 'chat',
    }, complete)

    expect(apiMocks.applyChange).toHaveBeenCalledWith(
      'default', 'project-1', 'change-1', 2,
    )
    expect(complete).toHaveBeenCalledWith(true)
  })

  it('uses the change version when an open tab has a stale cached version', async () => {
    apiMocks.applyChange.mockResolvedValue({
      document: { ...document, version: 3, content: 'Agent 修改' },
      change_set: { change_set_id: 'change-1', status: 'applied' },
    })
    const wrapper = mount(App)
    await flushPromises()
    await wrapper.get('button[title="新建空白项目"]').trigger('click')
    await wrapper.get('[role="dialog"] input').setValue('新项目')
    await wrapper.get('[role="dialog"] form').trigger('submit')
    await flushPromises()
    const vm = wrapper.vm as unknown as {
      applyAgentChange: (
        change: Record<string, unknown>, complete: (success: boolean) => void,
      ) => Promise<void>
    }

    await vm.applyAgentChange({
      change_set_id: 'change-1', project_id: 'project-1', document_id: 'document-1',
      range: { from: 0, to: 0 }, original: '', replacement: 'Agent 修改',
      document_version: 2, source: 'chat',
    }, vi.fn())

    expect(apiMocks.applyChange).toHaveBeenCalledWith(
      'default', 'project-1', 'change-1', 2,
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
    apiMocks.applyChange.mockResolvedValue({
      document: { ...document, version: 3, content: 'Agent 修改' },
      change_set: { change_set_id: 'change-1', status: 'applied' },
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

    expect(apiMocks.applyChange).toHaveBeenCalledWith('default', 'project-1', 'change-1', 1)
    expect(wrapper.find('.change-diff').exists()).toBe(false)
    expect(wrapper.find('.cm-diff-inserted').exists()).toBe(false)
  })

  it('keeps the change in both views when applying fails', async () => {
    apiMocks.applyChange.mockRejectedValue(new Error('版本冲突'))
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
    expect(wrapper.get('.global-error').text()).toBe('版本冲突')
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
})
