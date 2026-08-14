import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const apiMocks = vi.hoisted(() => ({
  applyChange: vi.fn(),
  rejectChange: vi.fn(),
  rewriteSelection: vi.fn(),
  watchTask: vi.fn(),
}))

vi.mock('../api/client', () => ({ apiClient: apiMocks }))

import DocumentEditor from './DocumentEditor.vue'
import type { ChangePreview, EditorTab } from '../types'

const tab: EditorTab = {
  document_id: 'document-1', project_id: 'project-1', assistant_id: 'default',
  relative_path: 'article.md', version: 2, editable: true, content: '原文内容', dirty: false,
}

const change: ChangePreview = {
  change_set_id: 'change-1', project_id: 'project-1', document_id: 'document-1',
  range: { from: 0, to: 2 }, original: '原文', replacement: '改写文本',
  document_version: 2, source: 'chat',
}

function baseProps(overrides: Record<string, unknown> = {}) {
  return {
    assistantId: 'default',
    projectId: 'project-1',
    tab,
    changes: [] as ChangePreview[],
    reviewing: [] as string[],
    ...overrides,
  }
}

type EditorVm = {
  toolbar: { from: number; to: number; left: number; top: number; text: string } | null
  prompt: string
  submitSelection: () => Promise<void>
}

describe('DocumentEditor', () => {
  beforeEach(() => {
    apiMocks.applyChange.mockReset()
    apiMocks.rejectChange.mockReset()
    apiMocks.rewriteSelection.mockReset()
    apiMocks.watchTask.mockReset()
  })

  it('synchronizes CodeMirror when the tab is replaced externally', async () => {
    const wrapper = mount(DocumentEditor, { props: baseProps() })
    await flushPromises()

    await wrapper.setProps({ tab: { ...tab, version: 3, content: '改写内容' } })
    await flushPromises()

    expect(wrapper.find('.code-editor').text()).toContain('改写内容')
  })

  it('does not attach an old rewrite task after switching documents', async () => {
    let resolveRequest: (value: { task_id: string }) => void = () => undefined
    apiMocks.rewriteSelection.mockReturnValue(
      new Promise((resolve) => { resolveRequest = resolve }),
    )
    const wrapper = mount(DocumentEditor, { props: baseProps() })
    await flushPromises()
    const vm = wrapper.vm as unknown as EditorVm
    vm.toolbar = { from: 0, to: 2, left: 0, top: 0, text: '原文' }
    vm.prompt = '精简'
    void vm.submitSelection()

    await wrapper.setProps({ tab: { ...tab, document_id: 'document-2', content: '新文档' } })
    resolveRequest({ task_id: 'old-task' })
    await flushPromises()

    expect(apiMocks.watchTask).not.toHaveBeenCalled()
    expect(wrapper.emitted('preview')).toBeUndefined()
  })

  it('ignores a rewrite error from a previous document scope', async () => {
    let rejectRequest: (reason: Error) => void = () => undefined
    apiMocks.rewriteSelection.mockReturnValue(
      new Promise((_resolve, reject) => { rejectRequest = reject }),
    )
    const wrapper = mount(DocumentEditor, { props: baseProps() })
    await flushPromises()
    const vm = wrapper.vm as unknown as EditorVm
    vm.toolbar = { from: 0, to: 2, left: 0, top: 0, text: '原文' }
    vm.prompt = '精简'
    void vm.submitSelection()

    await wrapper.setProps({ tab: { ...tab, document_id: 'document-2', content: '新文档' } })
    rejectRequest(new Error('旧文档失败'))
    await flushPromises()

    expect(wrapper.find('.editor-error').exists()).toBe(false)
  })

  it('ignores old rewrite events after switching documents', async () => {
    let callback: (event: Record<string, unknown>) => void = () => undefined
    apiMocks.rewriteSelection.mockResolvedValue({ task_id: 'old-task' })
    apiMocks.watchTask.mockImplementation((_assistant, _task, handler) => {
      callback = handler
      return { close: vi.fn() }
    })
    const wrapper = mount(DocumentEditor, { props: baseProps() })
    await flushPromises()
    const vm = wrapper.vm as unknown as EditorVm
    vm.toolbar = { from: 0, to: 2, left: 0, top: 0, text: '原文' }
    vm.prompt = '精简'
    await vm.submitSelection()
    await wrapper.setProps({ tab: { ...tab, document_id: 'document-2', content: '新文档' } })

    callback({ type: 'change_preview', data: { ...change, source: 'selection' } })
    await flushPromises()

    expect(wrapper.emitted('preview')).toBeUndefined()
  })

  it('hands a selection rewrite preview to the parent and closes the toolbar', async () => {
    let callback: (event: Record<string, unknown>) => void = () => undefined
    apiMocks.rewriteSelection.mockResolvedValue({ task_id: 'task-1' })
    apiMocks.watchTask.mockImplementation((_assistant, _task, handler) => {
      callback = handler
      return { close: vi.fn() }
    })
    const wrapper = mount(DocumentEditor, { props: baseProps() })
    await flushPromises()
    const vm = wrapper.vm as unknown as EditorVm
    vm.toolbar = { from: 0, to: 2, left: 0, top: 0, text: '原文' }
    vm.prompt = '改写'
    await vm.submitSelection()

    callback({ type: 'change_preview', data: { ...change, source: 'selection' } })
    await flushPromises()

    expect(wrapper.emitted('preview')?.[0][0]).toMatchObject({ change_set_id: 'change-1' })
    expect(wrapper.find('.selection-toolbar').exists()).toBe(false)
  })

  it('renders an inline diff for a pending change on the current version', async () => {
    const wrapper = mount(DocumentEditor, { props: baseProps({ changes: [change] }) })
    await flushPromises()

    expect(wrapper.find('.cm-diff-removed').text()).toBe('原文')
    expect(wrapper.find('.cm-diff-inserted').text()).toBe('改写文本')
    expect(wrapper.find('.editor-notice').exists()).toBe(false)
  })

  it('routes the inline accept and reject buttons to the parent', async () => {
    const wrapper = mount(DocumentEditor, { props: baseProps({ changes: [change] }) })
    await flushPromises()

    await wrapper.get('.cm-diff-accept').trigger('click')
    await wrapper.get('.cm-diff-reject').trigger('click')

    expect(wrapper.emitted('apply')?.[0][0]).toEqual(change)
    expect(wrapper.emitted('reject')?.[0][0]).toEqual(change)
  })

  it('disables the inline controls while the parent is reviewing the change', async () => {
    const wrapper = mount(DocumentEditor, {
      props: baseProps({ changes: [change], reviewing: ['change-1'] }),
    })
    await flushPromises()

    expect(wrapper.get('.cm-diff-accept').attributes('disabled')).toBeDefined()
  })

  it('degrades to a notice when the document moved past the change version', async () => {
    const wrapper = mount(DocumentEditor, { props: baseProps({ changes: [change] }) })
    await flushPromises()

    await wrapper.setProps({ tab: { ...tab, version: 3, content: '别的正文' } })
    await flushPromises()

    expect(wrapper.find('.cm-diff-inserted').exists()).toBe(false)
    expect(wrapper.get('.editor-notice').text()).toContain('1 处修改建议')
  })

  it('degrades to a notice while the tab has unsaved edits', async () => {
    const wrapper = mount(DocumentEditor, {
      props: baseProps({ changes: [change], tab: { ...tab, dirty: true } }),
    })
    await flushPromises()

    expect(wrapper.find('.cm-diff-inserted').exists()).toBe(false)
    expect(wrapper.find('.editor-notice').exists()).toBe(true)
  })
})
