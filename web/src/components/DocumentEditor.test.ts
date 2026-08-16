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
import type { ChangeSetPreview, EditorTab } from '../types'

const tab: EditorTab = {
  document_id: 'document-1', project_id: 'project-1', assistant_id: 'default',
  relative_path: 'article.md', version: 2, editable: true, content: '原文内容', dirty: false,
}

const change: ChangeSetPreview = {
  change_set_id: 'change-1', project_id: 'project-1', document_id: 'document-1',
  hunks: [{
    hunk_id: 'hunk-1', range: { from: 0, to: 2 },
    original: '原文', replacement: '改写文本', status: 'pending',
  }],
  document_version: 2, source: 'chat',
}

function baseProps(overrides: Record<string, unknown> = {}) {
  return {
    assistantId: 'default',
    projectId: 'project-1',
    tab,
    changes: [] as ChangeSetPreview[],
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

  it('reports an interrupted rewrite stream and keeps the notice after the task ends', async () => {
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

    callback({ type: 'reconnect_gap', data: { after_seq: 0, available_from: 2 } })
    await flushPromises()
    expect(wrapper.get('.editor-error').text()).toContain('网络中断')

    callback({ type: 'task_done', data: {} })
    await flushPromises()
    expect(wrapper.get('.editor-error').text()).toContain('网络中断')
    expect(wrapper.emitted('preview')).toBeUndefined()
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

    expect(wrapper.emitted('apply')?.[0]).toEqual([change, change.hunks[0]])
    expect(wrapper.emitted('reject')?.[0]).toEqual([change, change.hunks[0]])
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

  it('renders multiple hunks of one change set with independent controls', async () => {
    const multi: ChangeSetPreview = {
      change_set_id: 'change-multi', project_id: 'project-1', document_id: 'document-1',
      hunks: [
        { hunk_id: 'hunk-a', range: { from: 0, to: 5 }, original: 'AAAA。', replacement: '【A】。', status: 'pending' },
        { hunk_id: 'hunk-c', range: { from: 10, to: 15 }, original: 'CCCC。', replacement: '【C】。', status: 'pending' },
      ],
      document_version: 2, source: 'chat',
    }
    const wrapper = mount(DocumentEditor, {
      props: baseProps({ changes: [multi], tab: { ...tab, content: 'AAAA。BBBB。CCCC。' } }),
    })
    await flushPromises()

    expect(wrapper.findAll('.cm-diff-removed').map((node) => node.text())).toEqual(['AAAA。', 'CCCC。'])
    expect(wrapper.findAll('.cm-diff-inserted').map((node) => node.text())).toEqual(['【A】。', '【C】。'])
    expect(wrapper.findAll('.cm-diff-accept')).toHaveLength(2)

    await wrapper.findAll('.cm-diff-accept')[0].trigger('click')
    expect(wrapper.emitted('apply')?.[0]).toEqual([multi, multi.hunks[0]])
  })

  it('relocates remaining hunks by content after a sibling was accepted', async () => {
    const partial: ChangeSetPreview = {
      change_set_id: 'change-partial', project_id: 'project-1', document_id: 'document-1',
      hunks: [
        { hunk_id: 'hunk-a', range: { from: 0, to: 5 }, original: 'AAAA。', replacement: '【A】。', status: 'applied' },
        { hunk_id: 'hunk-c', range: { from: 10, to: 15 }, original: 'CCCC。', replacement: '【C】。', status: 'pending' },
      ],
      document_version: 2, source: 'chat',
    }
    const wrapper = mount(DocumentEditor, {
      // 标签页版本已推进（第一个 hunk 已应用），剩余 hunk 按原文内容重定位。
      props: baseProps({
        changes: [partial],
        tab: { ...tab, version: 3, content: '【A】。BBBB。CCCC。' },
      }),
    })
    await flushPromises()

    expect(wrapper.findAll('.cm-diff-removed').map((node) => node.text())).toEqual(['CCCC。'])
    expect(wrapper.get('.cm-diff-inserted').text()).toBe('【C】。')
  })

  it('counts unlocatable hunks in the degraded notice after edits', async () => {
    const lost: ChangeSetPreview = {
      change_set_id: 'change-lost', project_id: 'project-1', document_id: 'document-1',
      hunks: [
        { hunk_id: 'hunk-gone', range: { from: 0, to: 5 }, original: '已删除。', replacement: '新。', status: 'pending' },
      ],
      document_version: 2, source: 'chat',
    }
    const wrapper = mount(DocumentEditor, {
      props: baseProps({
        changes: [lost],
        tab: { ...tab, version: 3, content: '完全不同的正文。' },
      }),
    })
    await flushPromises()

    expect(wrapper.find('.cm-diff-inserted').exists()).toBe(false)
    expect(wrapper.get('.editor-notice').text()).toContain('1 处')
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
