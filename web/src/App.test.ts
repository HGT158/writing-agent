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

const apiMocks = vi.hoisted(() => ({
  listAssistants: vi.fn(), listProjects: vi.fn(), createProject: vi.fn(),
  getProjectTree: vi.fn(), getDocument: vi.fn(), saveDocument: vi.fn(),
  applyChange: vi.fn(), rejectChange: vi.fn(),
}))

vi.mock('./api/client', () => ({ apiClient: apiMocks }))

import App from './App.vue'

describe('App project creation', () => {
  beforeEach(() => {
    apiMocks.listAssistants.mockReset().mockResolvedValue([
      { id: 'default', name: '通用写作助手', description: '' },
    ])
    apiMocks.listProjects.mockReset().mockResolvedValueOnce([]).mockResolvedValue([project])
    apiMocks.createProject.mockReset().mockResolvedValue(project)
    apiMocks.getProjectTree.mockReset().mockResolvedValue([document])
    apiMocks.getDocument.mockReset().mockResolvedValue(document)
    apiMocks.saveDocument.mockReset()
    apiMocks.applyChange.mockReset()
    apiMocks.rejectChange.mockReset()
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
})
