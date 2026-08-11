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
import type { EditorTab } from '../types'

const tab: EditorTab = {
  document_id: 'document-1', project_id: 'project-1', assistant_id: 'default',
  relative_path: 'article.md', version: 2, editable: true, content: '原文内容', dirty: false,
}
describe('DocumentEditor', () => {
  beforeEach(() => {
    apiMocks.applyChange.mockReset()
    apiMocks.rejectChange.mockReset()
    apiMocks.rewriteSelection.mockReset()
    apiMocks.watchTask.mockReset()
  })

  it('synchronizes CodeMirror when the tab is replaced externally', async () => {
    const wrapper = mount(DocumentEditor, {
      props: {
        assistantId: 'default', projectId: 'project-1', tab,
        externalChange: null,
      },
    })
    await flushPromises()

    await wrapper.setProps({ tab: { ...tab, version: 3, content: '改写内容' } })
    await flushPromises()
    const editor = wrapper.find('.code-editor')
    expect(editor.text()).toContain('改写内容')
  })

  it('does not attach an old rewrite task after switching documents', async () => {
    let resolveRequest: (value: { task_id: string }) => void = () => undefined
    apiMocks.rewriteSelection.mockReturnValue(
      new Promise((resolve) => { resolveRequest = resolve }),
    )
    const wrapper = mount(DocumentEditor, {
      props: { assistantId: 'default', projectId: 'project-1', tab, externalChange: null },
    })
    await flushPromises()
    const vm = wrapper.vm as unknown as {
      toolbar: { from: number; to: number; left: number; top: number; text: string } | null
      prompt: string
      submitSelection: () => Promise<void>
    }
    vm.toolbar = { from: 0, to: 2, left: 0, top: 0, text: '原文' }
    vm.prompt = '精简'
    void vm.submitSelection()

    await wrapper.setProps({
      tab: { ...tab, document_id: 'document-2', content: '新文档' },
    })
    resolveRequest({ task_id: 'old-task' })
    await flushPromises()

    expect(apiMocks.watchTask).not.toHaveBeenCalled()
    expect(wrapper.find('.change-diff').exists()).toBe(false)
  })

  it('ignores a rewrite error from a previous document scope', async () => {
    let rejectRequest: (reason: Error) => void = () => undefined
    apiMocks.rewriteSelection.mockReturnValue(
      new Promise((_resolve, reject) => { rejectRequest = reject }),
    )
    const wrapper = mount(DocumentEditor, {
      props: { assistantId: 'default', projectId: 'project-1', tab, externalChange: null },
    })
    await flushPromises()
    const vm = wrapper.vm as unknown as {
      toolbar: { from: number; to: number; left: number; top: number; text: string } | null
      prompt: string
      submitSelection: () => Promise<void>
    }
    vm.toolbar = { from: 0, to: 2, left: 0, top: 0, text: '原文' }
    vm.prompt = '精简'
    void vm.submitSelection()

    await wrapper.setProps({
      tab: { ...tab, document_id: 'document-2', content: '新文档' },
    })
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
    const wrapper = mount(DocumentEditor, {
      props: { assistantId: 'default', projectId: 'project-1', tab, externalChange: null },
    })
    await flushPromises()
    const vm = wrapper.vm as unknown as {
      toolbar: { from: number; to: number; left: number; top: number; text: string } | null
      prompt: string
      submitSelection: () => Promise<void>
    }
    vm.toolbar = { from: 0, to: 2, left: 0, top: 0, text: '原文' }
    vm.prompt = '精简'
    await vm.submitSelection()
    await wrapper.setProps({
      tab: { ...tab, document_id: 'document-2', content: '新文档' },
    })

    callback({
      type: 'change_preview',
      data: {
        change_set_id: 'old-change', project_id: 'project-1', document_id: 'document-1',
        range: { from: 0, to: 2 }, original: '原文', replacement: '改文',
        document_version: 2, source: 'selection',
      },
    })
    await flushPromises()

    expect(wrapper.find('.change-diff').exists()).toBe(false)
  })

  it('maps a code-point change range back to the CodeMirror UTF-16 selection', async () => {
    let callback: (event: Record<string, unknown>) => void = () => undefined
    apiMocks.rewriteSelection.mockResolvedValue({ task_id: 'task-1' })
    apiMocks.watchTask.mockImplementation((_assistant, _task, handler) => {
      callback = handler
      return { close: vi.fn() }
    })
    const unicodeTab = { ...tab, content: 'A😀B' }
    const wrapper = mount(DocumentEditor, {
      props: { assistantId: 'default', projectId: 'project-1', tab: unicodeTab, externalChange: null },
    })
    await flushPromises()
    const vm = wrapper.vm as unknown as {
      toolbar: { from: number; to: number; left: number; top: number; text: string } | null
      prompt: string
      submitSelection: () => Promise<void>
      editorView: { state: { selection: { main: { from: number; to: number } } } }
    }
    vm.toolbar = { from: 0, to: 3, left: 0, top: 0, text: 'A😀' }
    vm.prompt = '改写'
    await vm.submitSelection()

    callback({
      type: 'change_preview',
      data: {
        change_set_id: 'change-1', project_id: 'project-1', document_id: 'document-1',
        range: { from: 0, to: 2 }, original: 'A😀', replacement: 'AB',
        document_version: 2, source: 'selection',
      },
    })
    await flushPromises()

    expect(vm.editorView.state.selection.main.from).toBe(0)
    expect(vm.editorView.state.selection.main.to).toBe(3)
  })

  it('ignores an apply response after switching documents', async () => {
    let resolveApply: (value: { document: typeof tab }) => void = () => undefined
    apiMocks.applyChange.mockReturnValue(new Promise((resolve) => { resolveApply = resolve }))
    const wrapper = mount(DocumentEditor, {
      props: {
        assistantId: 'default', projectId: 'project-1', tab,
        externalChange: {
          change_set_id: 'change-1', project_id: 'project-1', document_id: 'document-1',
          range: { from: 0, to: 2 }, original: '原文', replacement: '改文',
          document_version: 2, source: 'selection',
        },
      },
    })
    await flushPromises()
    const vm = wrapper.vm as unknown as { applyChange: () => Promise<void> }
    void vm.applyChange()
    await wrapper.setProps({ tab: { ...tab, document_id: 'document-2', content: '新文档' }, externalChange: null })
    resolveApply({ document: { ...tab, content: '旧文档已应用', version: 3 } })
    await flushPromises()

    expect(wrapper.find('.code-editor').text()).toContain('新文档')
    expect(wrapper.emitted('saved')).toBeUndefined()
  })

  it('ignores a reject response after switching documents', async () => {
    let resolveReject: (value?: unknown) => void = () => undefined
    apiMocks.rejectChange.mockReturnValue(new Promise<unknown>((resolve) => { resolveReject = resolve }))
    const wrapper = mount(DocumentEditor, {
      props: {
        assistantId: 'default', projectId: 'project-1', tab,
        externalChange: {
          change_set_id: 'change-1', project_id: 'project-1', document_id: 'document-1',
          range: { from: 0, to: 2 }, original: '原文', replacement: '改文',
          document_version: 2, source: 'selection',
        },
      },
    })
    await flushPromises()
    const vm = wrapper.vm as unknown as { rejectChange: () => Promise<void> }
    void vm.rejectChange()
    await wrapper.setProps({ tab: { ...tab, document_id: 'document-2', content: '新文档' }, externalChange: null })
    resolveReject()
    await flushPromises()

    expect(wrapper.find('.code-editor').text()).toContain('新文档')
    expect(wrapper.emitted('clearPreview')).toBeUndefined()
  })
})
