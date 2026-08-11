import { flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { ChangePreview, TaskEvent } from '../types'

const apiMocks = vi.hoisted(() => ({
  chatProject: vi.fn(),
  watchTask: vi.fn(),
}))

vi.mock('../api/client', () => ({ apiClient: apiMocks }))

import AgentPanel from './AgentPanel.vue'

const change: ChangePreview = {
  change_set_id: 'change-1',
  project_id: 'project-1',
  document_id: 'document-1',
  range: { from: 0, to: 4 },
  original: '原文',
  replacement: '改文',
  document_version: 2,
  source: 'chat',
}

function taskEvent(type: string, data: Record<string, unknown> = {}): TaskEvent {
  return { type, data, task_id: 'task-1' }
}

describe('AgentPanel', () => {
  beforeEach(() => {
    HTMLElement.prototype.scrollTo = vi.fn()
    apiMocks.chatProject.mockReset()
    apiMocks.watchTask.mockReset()
    apiMocks.chatProject.mockResolvedValue({ task_id: 'task-1' })
  })

  it('retries the last instruction without adding a duplicate user message', async () => {
    const callbacks: Array<(event: TaskEvent) => void | Promise<void>> = []
    apiMocks.watchTask.mockImplementation((_assistantId, _taskId, callback) => {
      callbacks.push(callback)
      return { close: vi.fn() }
    })
    const wrapper = mount(AgentPanel, {
      props: { assistantId: 'default', projectId: 'project-1', documentId: 'document-1' },
    })

    await wrapper.get('textarea').setValue('调整语气')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    await callbacks[0](taskEvent('change_preview', change as unknown as Record<string, unknown>))
    await callbacks[0](taskEvent('task_done'))
    await nextTick()

    await wrapper.get('.secondary-action').trigger('click')
    await flushPromises()

    expect(apiMocks.chatProject).toHaveBeenCalledTimes(2)
    expect(apiMocks.chatProject).toHaveBeenLastCalledWith(
      'default', 'project-1', '调整语气', 'document-1',
    )
    expect(wrapper.findAll('.message.user')).toHaveLength(1)
  })

  it('keeps a reviewed change until the parent confirms success', async () => {
    let callback: (event: TaskEvent) => void | Promise<void> = () => undefined
    apiMocks.watchTask.mockImplementation((_assistantId, _taskId, handler) => {
      callback = handler
      return { close: vi.fn() }
    })
    const wrapper = mount(AgentPanel, {
      props: { assistantId: 'default', projectId: 'project-1', documentId: 'document-1' },
    })

    await wrapper.get('textarea').setValue('修改这段')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    await callback(taskEvent('change_preview', change as unknown as Record<string, unknown>))
    await callback(taskEvent('task_done'))
    await nextTick()

    await wrapper.get('.primary-action').trigger('click')
    const applyEvent = wrapper.emitted('apply')?.[0]
    expect(applyEvent?.[0]).toEqual(change)
    expect(wrapper.find('.change-diff').exists()).toBe(true)

    const complete = applyEvent?.[1] as (success: boolean) => void
    complete(false)
    await nextTick()
    expect(wrapper.find('.change-diff').exists()).toBe(true)

    complete(true)
    await nextTick()
    expect(wrapper.find('.change-diff').exists()).toBe(false)
  })

  it('keeps pending changes when sending another instruction in the same scope', async () => {
    const callbacks: Array<(event: TaskEvent) => void | Promise<void>> = []
    apiMocks.watchTask.mockImplementation((_assistantId, _taskId, callback) => {
      callbacks.push(callback)
      return { close: vi.fn() }
    })
    const wrapper = mount(AgentPanel, {
      props: { assistantId: 'default', projectId: 'project-1', documentId: 'document-1' },
    })

    await wrapper.get('textarea').setValue('第一条修改')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    await callbacks[0](taskEvent('change_preview', change as unknown as Record<string, unknown>))
    await callbacks[0](taskEvent('task_done'))
    await nextTick()

    await wrapper.get('textarea').setValue('第二条修改')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(wrapper.find('.change-diff').exists()).toBe(true)
  })

  it('ignores a chat POST that resolves after the project scope changes', async () => {
    let resolveRequest: (value: { task_id: string }) => void = () => undefined
    apiMocks.chatProject.mockReturnValue(new Promise((resolve) => { resolveRequest = resolve }))
    const wrapper = mount(AgentPanel, {
      props: { assistantId: 'default', projectId: 'project-1', documentId: 'document-1' },
    })

    await wrapper.get('textarea').setValue('修改')
    await wrapper.get('form').trigger('submit')
    await wrapper.setProps({ projectId: 'project-2', documentId: 'document-2' })
    resolveRequest({ task_id: 'old-task' })
    await flushPromises()

    expect(apiMocks.watchTask).not.toHaveBeenCalled()
    expect(wrapper.findAll('.message')).toHaveLength(0)
  })

  it('ignores a chat error from a previous project scope', async () => {
    let rejectRequest: (reason: Error) => void = () => undefined
    apiMocks.chatProject.mockReturnValue(
      new Promise((_resolve, reject) => { rejectRequest = reject }),
    )
    const wrapper = mount(AgentPanel, {
      props: { assistantId: 'default', projectId: 'project-1', documentId: 'document-1' },
    })

    await wrapper.get('textarea').setValue('修改')
    await wrapper.get('form').trigger('submit')
    await wrapper.setProps({ projectId: 'project-2', documentId: 'document-2' })
    rejectRequest(new Error('旧项目失败'))
    await flushPromises()

    expect(wrapper.find('.inline-error').exists()).toBe(false)
  })

  it('leaves the sending state when the task fails', async () => {
    let callback: (event: TaskEvent) => void | Promise<void> = () => undefined
    apiMocks.watchTask.mockImplementation((_assistantId, _taskId, handler) => {
      callback = handler
      return { close: vi.fn() }
    })
    const wrapper = mount(AgentPanel, {
      props: { assistantId: 'default', projectId: 'project-1', documentId: 'document-1' },
    })

    await wrapper.get('textarea').setValue('执行失败的请求')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    await callback(taskEvent('task_failed', { reason: '模型不可用' }))
    await nextTick()

    expect(wrapper.get('textarea').attributes('disabled')).toBeUndefined()
    expect(wrapper.get('.inline-error').text()).toContain('模型不可用')
  })

  it('closes the old stream and clears the session when the project changes', async () => {
    const close = vi.fn()
    apiMocks.watchTask.mockImplementation((_assistantId, _taskId, _handler) => ({ close }))
    const wrapper = mount(AgentPanel, {
      props: { assistantId: 'default', projectId: 'project-1', documentId: 'document-1' },
    })

    await wrapper.get('textarea').setValue('先问一个问题')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(apiMocks.watchTask).toHaveBeenCalled()

    await wrapper.setProps({ projectId: 'project-2', documentId: 'document-2' })

    expect(close).toHaveBeenCalledOnce()
    expect(wrapper.findAll('.message')).toHaveLength(0)
  })
})
