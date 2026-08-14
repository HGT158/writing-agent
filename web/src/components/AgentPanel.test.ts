import { flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { ChangePreview, TaskEvent } from '../types'

const apiMocks = vi.hoisted(() => ({
  chatProject: vi.fn(),
  watchTask: vi.fn(),
  listProjectChatSessions: vi.fn(),
  getProjectChatSession: vi.fn(),
  deleteProjectChatSession: vi.fn(),
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

function mountPanel(overrides: Record<string, unknown> = {}) {
  return mount(AgentPanel, {
    props: {
      assistantId: 'default',
      projectId: 'project-1',
      documentId: 'document-1',
      changes: [] as ChangePreview[],
      reviewing: [] as string[],
      documentLabels: {} as Record<string, string>,
      ...overrides,
    },
  })
}

describe('AgentPanel', () => {
  beforeEach(() => {
    HTMLElement.prototype.scrollTo = vi.fn()
    apiMocks.chatProject.mockReset()
    apiMocks.watchTask.mockReset()
    apiMocks.listProjectChatSessions.mockReset().mockResolvedValue([])
    apiMocks.getProjectChatSession.mockReset()
    apiMocks.deleteProjectChatSession.mockReset().mockResolvedValue({ deleted: true })
    apiMocks.chatProject.mockResolvedValue({ task_id: 'task-1', chat_session_id: 'session-1' })
  })

  it('loads the most recent session and keeps it when the document changes', async () => {
    apiMocks.listProjectChatSessions.mockResolvedValue([{
      chat_session_id: 'session-recent', title: '最近会话',
      created_at: '2026-08-11T00:00:00Z', updated_at: '2026-08-11T01:00:00Z',
      message_count: 2,
    }])
    apiMocks.getProjectChatSession.mockResolvedValue({
      session: {
        chat_session_id: 'session-recent', title: '最近会话',
        created_at: '2026-08-11T00:00:00Z', updated_at: '2026-08-11T01:00:00Z',
        message_count: 2,
      },
      messages: [
        { message_id: 1, role: 'user', content: '历史问题', created_at: '2026-08-11T00:00:00Z' },
        { message_id: 2, role: 'assistant', content: '历史回答', created_at: '2026-08-11T00:00:01Z' },
      ],
      pending_changes: [],
    })
    const wrapper = mountPanel()
    await flushPromises()

    expect(wrapper.get('.chat-session-select').element).toHaveProperty('value', 'session-recent')
    expect(wrapper.get('.message.user').text()).toContain('历史问题')
    expect(wrapper.get('.message.assistant').text()).toContain('历史回答')

    await wrapper.setProps({ documentId: 'document-2' })
    await flushPromises()
    expect(wrapper.get('.message.user').text()).toContain('历史问题')
    expect(apiMocks.getProjectChatSession).toHaveBeenCalledTimes(1)
  })

  it('disables sending while the session list is loading', async () => {
    let resolveSessions: (sessions: unknown[]) => void = () => undefined
    apiMocks.listProjectChatSessions.mockReturnValue(
      new Promise((resolve) => { resolveSessions = resolve }),
    )
    const wrapper = mountPanel()
    await nextTick()

    expect(wrapper.get('textarea').attributes('disabled')).toBeDefined()
    expect(wrapper.get('.send-button').attributes('disabled')).toBeDefined()

    resolveSessions([])
    await flushPromises()
    expect(wrapper.get('textarea').attributes('disabled')).toBeUndefined()
  })

  it('publishes the pending changes of the session it switches to', async () => {
    const sessions = [
      { chat_session_id: 'session-2', title: '会话二', created_at: '2', updated_at: '2', message_count: 1 },
      { chat_session_id: 'session-1', title: '会话一', created_at: '1', updated_at: '1', message_count: 1 },
    ]
    apiMocks.listProjectChatSessions.mockResolvedValue(sessions)
    apiMocks.getProjectChatSession.mockImplementation(
      (_assistantId: string, _projectId: string, sessionId: string) => Promise.resolve({
        session: sessions.find((item) => item.chat_session_id === sessionId),
        messages: [{ message_id: 1, role: 'user', content: sessionId, created_at: '1' }],
        pending_changes: sessionId === 'session-1' ? [change] : [],
      }),
    )
    const wrapper = mountPanel()
    await flushPromises()

    await wrapper.get('.chat-session-select').setValue('session-1')
    await flushPromises()

    expect(wrapper.get('.message.user').text()).toContain('session-1')
    expect(wrapper.emitted('changesLoaded')?.at(-1)).toEqual([[change]])
  })

  it('renders parent-owned changes and blocks session deletion while they are pending', async () => {
    const wrapper = mountPanel({ changes: [change], documentLabels: { 'document-1': 'chapters/01.md' } })
    await flushPromises()

    expect(wrapper.get('.change-review-heading').text()).toContain('1 处待确认修改')
    expect(wrapper.get('.diff-target').text()).toContain('chapters/01.md')
    expect(wrapper.get('.delete-chat-button').attributes('disabled')).toBeDefined()
  })

  it('routes accept and reject to the parent without removing the card itself', async () => {
    const wrapper = mountPanel({ changes: [change] })
    await flushPromises()

    await wrapper.get('.primary-action').trigger('click')
    expect(wrapper.emitted('apply')?.[0]).toEqual([change])
    expect(wrapper.find('.change-diff').exists()).toBe(true)

    await wrapper.get('.icon-action').trigger('click')
    expect(wrapper.emitted('reject')?.[0]).toEqual([change])
    expect(wrapper.find('.change-diff').exists()).toBe(true)

    await wrapper.setProps({ changes: [] })
    expect(wrapper.find('.change-diff').exists()).toBe(false)
  })

  it('offers accepting every pending change at once', async () => {
    const second = { ...change, change_set_id: 'change-2' }
    const wrapper = mountPanel({ changes: [change, second] })
    await flushPromises()

    await wrapper.get('.link-action').trigger('click')

    expect(wrapper.emitted('applyAll')?.[0]).toEqual([[change, second]])
  })

  it('starts a new local session and adopts the server session id on send', async () => {
    apiMocks.listProjectChatSessions.mockResolvedValue([{
      chat_session_id: 'session-old', title: '旧会话', created_at: '1', updated_at: '1', message_count: 1,
    }])
    apiMocks.getProjectChatSession.mockResolvedValue({
      session: { chat_session_id: 'session-old', title: '旧会话', created_at: '1', updated_at: '1', message_count: 1 },
      messages: [{ message_id: 1, role: 'user', content: '旧消息', created_at: '1' }],
      pending_changes: [],
    })
    apiMocks.chatProject.mockResolvedValue({ task_id: 'task-new', chat_session_id: 'session-new' })
    apiMocks.watchTask.mockReturnValue({ close: vi.fn() })
    const wrapper = mountPanel()
    await flushPromises()

    await wrapper.get('.new-chat-button').trigger('click')
    expect(wrapper.findAll('.message')).toHaveLength(0)
    await wrapper.get('textarea').setValue('新问题')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(apiMocks.chatProject).toHaveBeenCalledWith(
      'default', 'project-1', '新问题', null, 'document-1',
    )
    expect(wrapper.get('.chat-session-select').element).toHaveProperty('value', 'session-new')
  })

  it('continues the same session stream after the document changes', async () => {
    let callback: (event: TaskEvent) => void | Promise<void> = () => undefined
    apiMocks.watchTask.mockImplementation((_assistantId, _taskId, handler) => {
      callback = handler
      return { close: vi.fn() }
    })
    const wrapper = mountPanel()
    await flushPromises()
    await wrapper.get('textarea').setValue('跨文档分析')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    await wrapper.setProps({ documentId: 'document-2' })
    await callback(taskEvent('token', { text: '继续输出' }))
    await nextTick()

    expect(wrapper.get('.message.assistant').text()).toContain('继续输出')
  })

  it('appends streamed tokens to one assistant message rendered as markdown', async () => {
    let callback: (event: TaskEvent) => void | Promise<void> = () => undefined
    apiMocks.watchTask.mockImplementation((_assistantId, _taskId, handler) => {
      callback = handler
      return { close: vi.fn() }
    })
    const wrapper = mountPanel()
    await flushPromises()

    await wrapper.get('textarea').setValue('分析正文')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    await callback(taskEvent('token', { text: '## 结论\n\n' }))
    await callback(taskEvent('token', { text: '**很好**' }))
    await nextTick()

    expect(wrapper.findAll('.message.assistant')).toHaveLength(1)
    expect(wrapper.get('.message.assistant h2').text()).toBe('结论')
    expect(wrapper.get('.message.assistant strong').text()).toBe('很好')
    expect(HTMLElement.prototype.scrollTo).toHaveBeenCalled()
  })

  it('stops following the tail once the user scrolls up', async () => {
    let callback: (event: TaskEvent) => void | Promise<void> = () => undefined
    apiMocks.watchTask.mockImplementation((_assistantId, _taskId, handler) => {
      callback = handler
      return { close: vi.fn() }
    })
    const wrapper = mountPanel()
    await flushPromises()
    await wrapper.get('textarea').setValue('长回复')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    const host = wrapper.get('.agent-messages').element as HTMLElement
    Object.defineProperty(host, 'scrollHeight', { value: 1000, configurable: true })
    Object.defineProperty(host, 'clientHeight', { value: 200, configurable: true })
    Object.defineProperty(host, 'scrollTop', { value: 100, configurable: true, writable: true })
    await wrapper.get('.agent-messages').trigger('scroll')
    vi.mocked(HTMLElement.prototype.scrollTo).mockClear()

    await callback(taskEvent('token', { text: '继续' }))
    await nextTick()

    expect(HTMLElement.prototype.scrollTo).not.toHaveBeenCalled()
  })

  it('removes the optimistic user message when the chat POST fails', async () => {
    apiMocks.chatProject.mockRejectedValue(new Error('助手忙碌'))
    const wrapper = mountPanel()
    await flushPromises()

    await wrapper.get('textarea').setValue('未送达消息')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(wrapper.find('.message.user').exists()).toBe(false)
    expect(wrapper.get('.inline-error').text()).toBe('助手忙碌')
  })

  it('shows project edit tool progress', async () => {
    let callback: (event: TaskEvent) => void | Promise<void> = () => undefined
    apiMocks.watchTask.mockImplementation((_assistantId, _taskId, handler) => {
      callback = handler
      return { close: vi.fn() }
    })
    const wrapper = mountPanel()
    await flushPromises()

    await wrapper.get('textarea').setValue('精简正文')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    await callback(taskEvent('tool_call', { tool: 'propose_project_edits' }))
    await nextTick()
    expect(wrapper.get('.tool-status').text()).toContain('正在准备修改')

    await callback(taskEvent('tool_result', {
      tool: 'propose_project_edits',
      ok: true,
      summary: '已生成 1 处修改建议',
    }))
    await nextTick()
    expect(wrapper.get('.tool-status').text()).toContain('已生成 1 处修改建议')

    await callback(taskEvent('task_done'))
    await nextTick()
    expect(wrapper.find('.tool-status').exists()).toBe(false)
  })

  it('shows only the terminal error when the edit tool fails', async () => {
    let callback: (event: TaskEvent) => void | Promise<void> = () => undefined
    apiMocks.watchTask.mockImplementation((_assistantId, _taskId, handler) => {
      callback = handler
      return { close: vi.fn() }
    })
    const wrapper = mountPanel()
    await flushPromises()

    await wrapper.get('textarea').setValue('生成首稿')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    await callback(taskEvent('tool_result', {
      tool: 'propose_project_edits',
      ok: false,
      error: '修改建议参数无效，请重试',
    }))
    await callback(taskEvent('task_failed', { reason: '修改建议参数无效，请重试' }))
    await nextTick()

    expect(wrapper.find('.tool-status').exists()).toBe(false)
    expect(wrapper.findAll('.inline-error')).toHaveLength(1)
    expect(wrapper.get('.inline-error').text()).toBe('修改建议参数无效，请重试')
  })

  it('removes partial assistant text when the task fails', async () => {
    let callback: (event: TaskEvent) => void | Promise<void> = () => undefined
    apiMocks.watchTask.mockImplementation((_assistantId, _taskId, handler) => {
      callback = handler
      return { close: vi.fn() }
    })
    const wrapper = mountPanel()
    await flushPromises()

    await wrapper.get('textarea').setValue('分析失败')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    await callback(taskEvent('token', { text: '未完成回答' }))
    await callback(taskEvent('task_failed', { reason: 'stream down' }))
    await nextTick()

    expect(wrapper.find('.message.assistant').exists()).toBe(false)
    expect(wrapper.get('.inline-error').text()).toBe('stream down')
  })

  it('publishes a streamed change preview to the parent', async () => {
    let callback: (event: TaskEvent) => void | Promise<void> = () => undefined
    apiMocks.watchTask.mockImplementation((_assistantId, _taskId, handler) => {
      callback = handler
      return { close: vi.fn() }
    })
    const wrapper = mountPanel()
    await flushPromises()

    await wrapper.get('textarea').setValue('修改这段')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    await callback(taskEvent('change_preview', change as unknown as Record<string, unknown>))

    expect(wrapper.emitted('changeAdded')?.[0]).toEqual([change])
  })

  it('retries the last instruction as a new visible user message', async () => {
    const callbacks: Array<(event: TaskEvent) => void | Promise<void>> = []
    apiMocks.watchTask.mockImplementation((_assistantId, _taskId, callback) => {
      callbacks.push(callback)
      return { close: vi.fn() }
    })
    const wrapper = mountPanel()
    await flushPromises()

    await wrapper.get('textarea').setValue('调整语气')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    await callbacks[0](taskEvent('task_done'))
    await wrapper.setProps({ changes: [change] })
    await nextTick()

    await wrapper.get('.secondary-action').trigger('click')
    await flushPromises()

    expect(apiMocks.chatProject).toHaveBeenCalledTimes(2)
    expect(apiMocks.chatProject).toHaveBeenLastCalledWith(
      'default', 'project-1', '调整语气', 'session-1', 'document-1',
    )
    expect(wrapper.findAll('.message.user')).toHaveLength(2)
  })

  it('does not reset the pending set when sending another instruction in the same scope', async () => {
    apiMocks.watchTask.mockReturnValue({ close: vi.fn() })
    const wrapper = mountPanel({ changes: [change] })
    await flushPromises()

    await wrapper.get('textarea').setValue('第二条修改')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(wrapper.emitted('changesLoaded')).toBeUndefined()
    expect(wrapper.find('.change-diff').exists()).toBe(true)
  })

  it('ignores a chat POST that resolves after the project scope changes', async () => {
    let resolveRequest: (value: { task_id: string }) => void = () => undefined
    apiMocks.chatProject.mockReturnValue(new Promise((resolve) => { resolveRequest = resolve }))
    const wrapper = mountPanel()
    await flushPromises()

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
    const wrapper = mountPanel()
    await flushPromises()

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
    const wrapper = mountPanel()
    await flushPromises()

    await wrapper.get('textarea').setValue('执行失败的请求')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    await callback(taskEvent('task_failed', { reason: '模型不可用' }))
    await nextTick()

    expect(wrapper.get('textarea').attributes('disabled')).toBeUndefined()
    expect(wrapper.get('.inline-error').text()).toContain('模型不可用')
  })

  it('tells the user that a disconnected stream can be recovered by refreshing', async () => {
    let callback: (event: TaskEvent) => void | Promise<void> = () => undefined
    let onError: (error: Error) => void = () => undefined
    apiMocks.watchTask.mockImplementation((_assistantId, _taskId, handler, errorHandler) => {
      callback = handler
      onError = errorHandler
      return { close: vi.fn() }
    })
    const wrapper = mountPanel()
    await flushPromises()

    await wrapper.get('textarea').setValue('长回复')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    await callback(taskEvent('token', { text: '未完成回答' }))
    await nextTick()
    expect(wrapper.find('.message.assistant').exists()).toBe(true)
    onError(new Error('任务事件流连接失败'))
    await nextTick()

    expect(wrapper.find('.message.assistant').exists()).toBe(false)
    expect(wrapper.get('.inline-error').text()).toContain('刷新可恢复')
  })

  it('closes the old stream and clears the session when the project changes', async () => {
    const close = vi.fn()
    apiMocks.watchTask.mockImplementation((_assistantId, _taskId, _handler) => ({ close }))
    const wrapper = mountPanel()
    await flushPromises()

    await wrapper.get('textarea').setValue('先问一个问题')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(apiMocks.watchTask).toHaveBeenCalled()

    await wrapper.setProps({ projectId: 'project-2', documentId: 'document-2' })

    expect(close).toHaveBeenCalledOnce()
    expect(wrapper.findAll('.message')).toHaveLength(0)
  })
})
