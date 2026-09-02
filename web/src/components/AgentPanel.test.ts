import { flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { ChangeSetPreview, TaskEvent } from '../types'

const apiMocks = vi.hoisted(() => ({
  chatProject: vi.fn(),
  watchTask: vi.fn(),
  listProjectChatSessions: vi.fn(),
  getProjectChatSession: vi.fn(),
  deleteProjectChatSession: vi.fn(),
  listLlmProviders: vi.fn(),
  addLlmProvider: vi.fn(),
  selectLlmProvider: vi.fn(),
}))

vi.mock('../api/client', () => ({ apiClient: apiMocks }))

import AgentPanel from './AgentPanel.vue'

const providersPayload = {
  current: { provider_id: 'default', model: 'test-chat' },
  providers: [
    {
      id: 'default', name: '默认提供商', base_url: 'https://api.example.com',
      models: ['test-chat'], temperature: 0.3, api_key_hint: 'sk-***7890',
    },
    {
      id: 'p-other', name: '备选厂商', base_url: 'https://api.other.com',
      models: ['other-chat', 'other-reasoner'], temperature: 0.7, api_key_hint: 'sk-***9876',
    },
  ],
}

const providersSwitched = {
  current: { provider_id: 'p-other', model: 'other-reasoner' },
  providers: providersPayload.providers,
}

const providerAdded = {
  current: { provider_id: 'default', model: 'test-chat' },
  providers: [
    ...providersPayload.providers,
    {
      id: 'p-new', name: '新建厂商', base_url: 'https://api.new.com',
      models: ['new-chat'], temperature: 0.3, api_key_hint: 'sk-****1111',
    },
  ],
}

const change: ChangeSetPreview = {
  change_set_id: 'change-1',
  project_id: 'project-1',
  document_id: 'document-1',
  hunks: [{
    hunk_id: 'hunk-1',
    range: { from: 0, to: 2 },
    original: '原文',
    replacement: '改文',
    status: 'pending',
  }],
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
      changes: [] as ChangeSetPreview[],
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
    apiMocks.listLlmProviders.mockReset().mockResolvedValue(providersPayload)
    apiMocks.addLlmProvider.mockReset()
    apiMocks.selectLlmProvider.mockReset().mockResolvedValue(providersSwitched)
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
    expect(wrapper.emitted('changesLoaded')?.at(-1)).toEqual([[
      { ...change, chat_session_id: 'session-1' },
    ], 'session-1'])
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

  it('shows edit tool progress as an expanding work record', async () => {
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
    await callback(taskEvent('work_item_start', {
      work_id: 'w1', kind: 'tool', title: '正在准备修改', tool_name: 'propose_project_edits',
    }))
    await nextTick()
    const item = wrapper.get('.work-item')
    expect(item.text()).toContain('正在准备修改')
    expect(item.classes()).toContain('running')

    await callback(taskEvent('work_item_delta', { work_id: 'w1', text: '正在校验修改范围' }))
    await nextTick()
    expect(wrapper.get('.work-item').text()).toContain('正在校验修改范围')

    await callback(taskEvent('work_item_done', {
      work_id: 'w1', kind: 'tool', status: 'succeeded', result_summary: '已生成 1 处修改建议',
    }))
    await nextTick()
    expect(wrapper.findAll('.work-item')).toHaveLength(1)
    expect(wrapper.get('.work-item').classes()).toContain('succeeded')

    await callback(taskEvent('task_done'))
    await nextTick()
    expect(wrapper.find('.work-item').exists()).toBe(false)  // 终态自动折叠
    expect(wrapper.get('.work-record-header').text()).toContain('工具 1')
  })

  it('ticks the elapsed time of a running work record every second', async () => {
    vi.useFakeTimers()
    try {
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
      await callback(taskEvent('work_item_start', {
        work_id: 'w1', kind: 'progress', title: '正在读取当前文档与历史上下文',
      }))
      await nextTick()
      expect(wrapper.get('.work-record-header').text()).toContain('耗时 0s')

      // 运行中耗时应随时间自动跳动，而不是等下一次交互才刷新。
      await vi.advanceTimersByTimeAsync(2_000)
      await nextTick()
      expect(wrapper.get('.work-record-header').text()).toContain('耗时 2s')

      await vi.advanceTimersByTimeAsync(1_000)
      await nextTick()
      expect(wrapper.get('.work-record-header').text()).toContain('耗时 3s')
    } finally {
      vi.useRealTimers()
    }
  })

  it('keeps the completed work record after a new turn starts (phase7 P2-4)', async () => {
    let callback: (event: TaskEvent) => void | Promise<void> = () => undefined
    apiMocks.watchTask.mockImplementation((_assistantId, _taskId, handler) => {
      callback = handler
      return { close: vi.fn() }
    })
    const wrapper = mountPanel()
    await flushPromises()

    // 第一轮：发送 → 工作记录 → 终态折叠
    await wrapper.get('textarea').setValue('第一轮指令')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    await callback(taskEvent('work_item_start', {
      work_id: 'w1', kind: 'tool', title: '正在准备修改', tool_name: 'propose_project_edits',
    }))
    await nextTick()
    await callback(taskEvent('work_item_done', {
      work_id: 'w1', kind: 'tool', status: 'succeeded', result_summary: '已生成 1 处修改建议',
    }))
    await nextTick()
    await callback(taskEvent('task_done'))
    await nextTick()
    expect(wrapper.find('.work-record').exists()).toBe(true)

    // 第二轮：发送清空 liveWork，但第一轮记录不得消失（历史归档到 workRecords）
    await wrapper.get('textarea').setValue('第二轮指令')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    await callback(taskEvent('work_item_start', {
      work_id: 'w2', kind: 'progress', title: '正在处理第二轮',
    }))
    await nextTick()

    const records = wrapper.findAll('.work-record')
    expect(records).toHaveLength(2)
    expect(records[0].text()).toContain('工具 1')
    expect(records[1].text()).toContain('正在处理第二轮')
    const conversation = wrapper.findAll('.message.user, .work-record')
    expect(conversation.map((item) => item.text())).toEqual([
      expect.stringContaining('第一轮指令'),
      expect.stringContaining('工具 1'),
      expect.stringContaining('第二轮指令'),
      expect.stringContaining('正在处理第二轮'),
    ])
  })

  it('forwards card open clicks to open the target document (phase7 P2-5)', async () => {
    const wrapper = mountPanel({ changes: [change] })
    await flushPromises()

    const card = wrapper.get('.change-diff')
    await card.get('.diff-heading').trigger('click')
    await card.get('.diff-hunk').trigger('click')
    const opened = wrapper.emitted('openDocument') ?? []
    expect(opened).toHaveLength(2)
    expect(opened[0]).toEqual(['project-1', 'document-1'])
    expect(opened[1]).toEqual(['project-1', 'document-1', 'hunk-1'])
  })

  it('shows hunk summaries and batch actions per change set card', async () => {
    const multi: ChangeSetPreview = {
      change_set_id: 'change-multi', project_id: 'project-1', document_id: 'document-1',
      hunks: [
        { hunk_id: 'h1', range: { from: 0, to: 2 }, original: '旧一', replacement: '新一', status: 'pending' },
        { hunk_id: 'h2', range: { from: 5, to: 7 }, original: '旧二', replacement: '新二', status: 'stale' },
      ],
      document_version: 2, source: 'chat',
    }
    const wrapper = mountPanel({ changes: [multi] })
    await flushPromises()

    const card = wrapper.get('.change-diff')
    expect(card.text()).toContain('2 处')
    expect(card.text()).toContain('旧一')
    expect(card.text()).toContain('新二')
    expect(card.text()).toContain('已失效')
    expect(card.get('.primary-action').text()).toContain('全部接受')
  })

  it('keeps only the dismiss action on a fully stale card', async () => {
    const staleOnly: ChangeSetPreview = {
      change_set_id: 'change-stale', project_id: 'project-1', document_id: 'document-1',
      hunks: [
        { hunk_id: 'h1', range: { from: 0, to: 2 }, original: '旧一', replacement: '新一', status: 'stale' },
        { hunk_id: 'h2', range: { from: 5, to: 7 }, original: '旧二', replacement: '新二', status: 'stale' },
      ],
      document_version: 2, source: 'chat',
    }
    const wrapper = mountPanel({ changes: [staleOnly] })
    await flushPromises()

    const card = wrapper.get('.change-diff')
    // 全部失效的卡片没有可接受项：不再显示"全部接受"，只保留放弃与重试入口。
    expect(wrapper.find('.change-diff .primary-action').exists()).toBe(false)
    expect(wrapper.find('.change-diff .icon-action').exists()).toBe(true)
    expect(card.text()).toContain('已失效')
  })

  it('renders persisted work records collapsed and expands on click', async () => {
    apiMocks.listProjectChatSessions.mockResolvedValue([{
      chat_session_id: 'session-1', title: '历史会话',
      created_at: '2026-08-16T00:00:00Z', updated_at: '2026-08-16T01:00:00Z',
      message_count: 2,
    }])
    apiMocks.getProjectChatSession.mockResolvedValue({
      session: {
        chat_session_id: 'session-1', title: '历史会话',
        created_at: '2026-08-16T00:00:00Z', updated_at: '2026-08-16T01:00:00Z',
        message_count: 2,
      },
      messages: [
        { message_id: 1, role: 'user', content: '历史指令', created_at: '2026-08-16T00:00:00Z' },
        { message_id: 2, role: 'assistant', content: '历史回复', created_at: '2026-08-16T00:00:01Z' },
      ],
      pending_changes: [],
      work_events: [
        {
          event_id: 1, task_id: 'task-old', user_message_id: 1, event_seq: 1,
          kind: 'progress', status: 'succeeded', title: '正在读取当前文档与历史上下文',
          detail: '', tool_name: null, args_summary: null, result_summary: null,
          change_set_id: null, document_id: null,
          created_at: '2026-08-16T00:00:00Z', completed_at: '2026-08-16T00:00:02Z',
        },
        {
          event_id: 2, task_id: 'task-old', user_message_id: 1, event_seq: 2,
          kind: 'task', status: 'succeeded', title: '历史指令',
          detail: '无工具调用', tool_name: null, args_summary: null, result_summary: null,
          change_set_id: null, document_id: null,
          created_at: '2026-08-16T00:00:00Z', completed_at: '2026-08-16T00:00:03Z',
        },
      ],
    })
    const wrapper = mountPanel()
    await flushPromises()

    const record = wrapper.get('.work-record')
    expect(record.classes()).toContain('succeeded')
    expect(wrapper.find('.work-record-items').exists()).toBe(false)  // 历史默认折叠
    const order = wrapper.findAll('.message, .work-record')
    expect(order[0].classes()).toContain('user')
    expect(order[1].classes()).toContain('work-record')

    await wrapper.get('.work-record-header').trigger('click')
    expect(wrapper.find('.work-record-items').exists()).toBe(true)
    expect(wrapper.get('.work-item').text()).toContain('正在读取当前文档')
  })

  it('marks a persisted group without terminal as running, not interrupted', async () => {
    apiMocks.listProjectChatSessions.mockResolvedValue([{
      chat_session_id: 'session-1', title: '运行中会话',
      created_at: '2026-08-16T00:00:00Z', updated_at: '2026-08-16T01:00:00Z',
      message_count: 1,
    }])
    apiMocks.getProjectChatSession.mockResolvedValue({
      session: {
        chat_session_id: 'session-1', title: '运行中会话',
        created_at: '2026-08-16T00:00:00Z', updated_at: '2026-08-16T01:00:00Z',
        message_count: 1,
      },
      messages: [
        { message_id: 1, role: 'user', content: '运行中指令', created_at: '2026-08-16T00:00:00Z' },
      ],
      pending_changes: [],
      // 无 kind=task 终态：服务端对账只跳过仍在运行的任务，前端据此显示运行中。
      work_events: [
        {
          event_id: 9, task_id: 'task-live', user_message_id: 1, event_seq: 1,
          kind: 'progress', status: 'succeeded', title: '正在读取当前文档',
          detail: '', tool_name: null, args_summary: null, result_summary: null,
          change_set_id: null, document_id: null,
          created_at: '2026-08-16T00:00:00Z', completed_at: '2026-08-16T00:00:02Z',
        },
      ],
    })
    const wrapper = mountPanel()
    await flushPromises()

    const record = wrapper.get('.work-record')
    expect(record.classes()).toContain('running')
    expect(wrapper.get('.work-record-header').text()).toContain('运行中')
    expect(wrapper.get('.work-record-header').text()).not.toContain('已中断')
  })

  it('opens the document when clicking a changes work item', async () => {
    let callback: (event: TaskEvent) => void | Promise<void> = () => undefined
    apiMocks.watchTask.mockImplementation((_assistantId, _taskId, handler) => {
      callback = handler
      return { close: vi.fn() }
    })
    const wrapper = mountPanel()
    await flushPromises()

    await wrapper.get('textarea').setValue('修改正文')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    await callback(taskEvent('work_item_start', {
      work_id: 'w9', kind: 'changes', title: '为 article.md 生成修改建议',
      change_set_id: 'change-9', document_id: 'document-1',
    }))
    await nextTick()

    await wrapper.get('.work-item.changes').trigger('click')
    expect(wrapper.emitted('openDocument')).toEqual([['project-1', 'document-1']])
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

    expect(wrapper.emitted('changeAdded')?.[0]).toEqual([
      { ...change, chat_session_id: 'session-1' },
    ])
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

  it('recovers the persisted session after a reconnect gap ends the task', async () => {
    let callback: (event: TaskEvent) => void | Promise<void> = () => undefined
    apiMocks.watchTask.mockImplementation((_assistantId, _taskId, handler) => {
      callback = handler
      return { close: vi.fn() }
    })
    const wrapper = mountPanel()
    await flushPromises()

    await wrapper.get('textarea').setValue('写摘要')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    await callback(taskEvent('token', { text: '不完整片段' }))
    await callback(taskEvent('reconnect_gap', { after_seq: 0, available_from: 3 }))
    await nextTick()

    expect(wrapper.find('.message.assistant').exists()).toBe(false)
    expect(wrapper.get('.inline-error').text()).toContain('网络中断')
    expect(wrapper.get('textarea').attributes('disabled')).toBeDefined()

    apiMocks.getProjectChatSession.mockResolvedValue({
      session: {
        chat_session_id: 'session-1', title: '写摘要',
        created_at: '1', updated_at: '2', message_count: 2,
      },
      messages: [
        { message_id: 1, role: 'user', content: '写摘要', created_at: '1' },
        { message_id: 2, role: 'assistant', content: '完整回复', created_at: '2' },
      ],
      pending_changes: [change],
    })
    await callback(taskEvent('task_done', {}))
    await flushPromises()

    expect(apiMocks.getProjectChatSession).toHaveBeenCalledWith('default', 'project-1', 'session-1')
    expect(wrapper.get('.message.assistant').text()).toContain('完整回复')
    expect(wrapper.emitted('changesLoaded')?.at(-1)?.[0]).toEqual([
      { ...change, chat_session_id: 'session-1' },
    ])
    expect(wrapper.get('textarea').attributes('disabled')).toBeUndefined()
    // 恢复成功后网络中断提示必须消失，不得残留已过时的警告。
    expect(wrapper.find('.inline-error').exists()).toBe(false)
  })

  it('recovers pending changes when a gapped task fails', async () => {
    let callback: (event: TaskEvent) => void | Promise<void> = () => undefined
    apiMocks.watchTask.mockImplementation((_assistantId, _taskId, handler) => {
      callback = handler
      return { close: vi.fn() }
    })
    const wrapper = mountPanel()
    await flushPromises()

    await wrapper.get('textarea').setValue('修改正文')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    await callback(taskEvent('reconnect_gap', { after_seq: 1, available_from: 4 }))

    apiMocks.getProjectChatSession.mockResolvedValue({
      session: {
        chat_session_id: 'session-1', title: '修改正文',
        created_at: '1', updated_at: '2', message_count: 1,
      },
      messages: [
        { message_id: 1, role: 'user', content: '修改正文', created_at: '1' },
      ],
      pending_changes: [change],
    })
    await callback(taskEvent('task_failed', { reason: '模型不可用' }))
    await flushPromises()

    expect(wrapper.get('.inline-error').text()).toContain('模型不可用')
    expect(apiMocks.getProjectChatSession).toHaveBeenCalledWith('default', 'project-1', 'session-1')
    expect(wrapper.emitted('changesLoaded')?.at(-1)?.[0]).toEqual([
      { ...change, chat_session_id: 'session-1' },
    ])
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

  it('shows chat cards only for the active chat session', async () => {
    apiMocks.listProjectChatSessions.mockResolvedValue([{
      chat_session_id: 'session-1', title: '当前会话', created_at: '1', updated_at: '1', message_count: 0,
    }])
    apiMocks.getProjectChatSession.mockResolvedValue({
      session: { chat_session_id: 'session-1', title: '当前会话', created_at: '1', updated_at: '1', message_count: 0 },
      messages: [], pending_changes: [], work_events: [],
    })
    const wrapper = mountPanel({
      changes: [
        { ...change, change_set_id: 'current', chat_session_id: 'session-1' },
        { ...change, change_set_id: 'other', chat_session_id: 'session-2' },
      ],
    })
    await flushPromises()

    expect(wrapper.findAll('.change-diff')).toHaveLength(1)
    expect(wrapper.get('.change-diff').attributes('data-change-id')).not.toBe('other')
  })

  it('silently ignores a valid empty streamed change preview', async () => {
    let callback: (event: TaskEvent) => void | Promise<void> = () => undefined
    apiMocks.watchTask.mockImplementation((_assistantId, _taskId, handler) => {
      callback = handler
      return { close: vi.fn() }
    })
    const wrapper = mountPanel()
    await flushPromises()
    await wrapper.get('textarea').setValue('检查是否需要修改')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    await callback(taskEvent('change_preview', { ...change, hunks: [] }))
    await nextTick()

    expect(wrapper.emitted('changeAdded')).toBeUndefined()
    expect(wrapper.find('.inline-error').exists()).toBe(false)
  })

  it('bounds persisted work records and truncates long result details', async () => {
    const messages = Array.from({ length: 101 }, (_, index) => ({
      message_id: index + 1, role: 'user' as const, content: `指令 ${index + 1}`, created_at: '1',
    }))
    const work_events = Array.from({ length: 101 }, (_, index) => ([
      {
        event_id: index * 2 + 1, task_id: `task-${index}`, user_message_id: index + 1,
        event_seq: 1, kind: 'progress', status: 'succeeded', title: '处理',
        detail: `头部${'x'.repeat(600)}尾部`, tool_name: null, args_summary: null,
        result_summary: null, change_set_id: null, document_id: null,
        created_at: '2026-08-16T00:00:00Z', completed_at: '2026-08-16T00:00:01Z',
      },
      {
        event_id: index * 2 + 2, task_id: `task-${index}`, user_message_id: index + 1,
        event_seq: 2, kind: 'task', status: 'succeeded', title: '完成', detail: '',
        tool_name: null, args_summary: null, result_summary: null,
        change_set_id: null, document_id: null,
        created_at: '2026-08-16T00:00:00Z', completed_at: '2026-08-16T00:00:02Z',
      },
    ])).flat()
    apiMocks.listProjectChatSessions.mockResolvedValue([{
      chat_session_id: 'session-1', title: '历史', created_at: '1', updated_at: '2', message_count: 101,
    }])
    apiMocks.getProjectChatSession.mockResolvedValue({
      session: { chat_session_id: 'session-1', title: '历史', created_at: '1', updated_at: '2', message_count: 101 },
      messages, pending_changes: [], work_events,
    })
    const wrapper = mountPanel()
    await flushPromises()

    expect(wrapper.findAll('.work-record')).toHaveLength(100)
    await wrapper.findAll('.work-record-header')[0].trigger('click')
    const detail = wrapper.get('.work-item-detail').text()
    expect(detail.length).toBeLessThanOrEqual(500)
    expect(detail).toContain('尾部')
    expect(detail).not.toContain('头部')
  })
})

describe('AgentPanel keyboard semantics', () => {
  beforeEach(() => {
    HTMLElement.prototype.scrollTo = vi.fn()
    apiMocks.chatProject.mockReset()
    apiMocks.watchTask.mockReset().mockImplementation(() => ({ close: vi.fn() }))
    apiMocks.listProjectChatSessions.mockReset().mockResolvedValue([])
    apiMocks.getProjectChatSession.mockReset()
    apiMocks.deleteProjectChatSession.mockReset().mockResolvedValue({ deleted: true })
    apiMocks.chatProject.mockResolvedValue({ task_id: 'task-1', chat_session_id: 'session-1' })
    apiMocks.listLlmProviders.mockReset().mockResolvedValue(providersPayload)
    apiMocks.addLlmProvider.mockReset()
    apiMocks.selectLlmProvider.mockReset().mockResolvedValue(providersSwitched)
  })

  it('activates change card navigation with Enter and Space (phase8 P3-5)', async () => {
    const wrapper = mountPanel({ changes: [change] })
    await flushPromises()

    const card = wrapper.get('.change-diff')
    await card.get('.diff-heading').trigger('keydown', { key: 'Enter' })
    await card.get('.diff-heading').trigger('keydown', { key: ' ' })
    await card.get('.diff-hunk').trigger('keydown', { key: 'Enter' })
    await card.get('.diff-hunk').trigger('keydown', { key: ' ' })

    const opened = wrapper.emitted('openDocument') ?? []
    expect(opened).toHaveLength(4)
    expect(opened[0]).toEqual(['project-1', 'document-1'])
    expect(opened[1]).toEqual(['project-1', 'document-1'])
    expect(opened[2]).toEqual(['project-1', 'document-1', 'hunk-1'])
    expect(opened[3]).toEqual(['project-1', 'document-1', 'hunk-1'])
  })

  it('gives changes work items button semantics with keyboard activation (phase7 P3-9)', async () => {
    let callback: (event: TaskEvent) => void | Promise<void> = () => undefined
    apiMocks.watchTask.mockImplementation((_assistantId, _taskId, handler) => {
      callback = handler
      return { close: vi.fn() }
    })
    const wrapper = mountPanel()
    await flushPromises()

    await wrapper.get('textarea').setValue('帮我修改')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    await callback(taskEvent('work_item_start', {
      work_id: 'w1', kind: 'progress', title: '正在读取当前文档与历史上下文',
    }))
    await callback(taskEvent('work_item_start', {
      work_id: 'w2', kind: 'changes', title: '生成修改建议',
      change_set_id: 'change-1', document_id: 'document-1',
    }))
    await nextTick()

    const items = wrapper.findAll('.work-item')
    expect(items).toHaveLength(2)
    expect(items[0].attributes('role')).toBeUndefined()
    expect(items[1].attributes('role')).toBe('button')

    await items[1].trigger('keydown', { key: 'Enter' })
    await items[1].trigger('keydown', { key: ' ' })
    const opened = wrapper.emitted('openDocument') ?? []
    expect(opened).toEqual([
      ['project-1', 'document-1'],
      ['project-1', 'document-1'],
    ])
  })
})

describe('AgentPanel model picker integration (v1.31)', () => {
  beforeEach(() => {
    HTMLElement.prototype.scrollTo = vi.fn()
    apiMocks.chatProject.mockReset()
    apiMocks.watchTask.mockReset().mockImplementation(() => ({ close: vi.fn() }))
    apiMocks.listProjectChatSessions.mockReset().mockResolvedValue([])
    apiMocks.getProjectChatSession.mockReset()
    apiMocks.deleteProjectChatSession.mockReset().mockResolvedValue({ deleted: true })
    apiMocks.listLlmProviders.mockReset().mockResolvedValue(providersPayload)
    apiMocks.addLlmProvider.mockReset()
    apiMocks.selectLlmProvider.mockReset().mockResolvedValue(providersSwitched)
  })

  it('shows the current provider and model on the composer picker', async () => {
    const wrapper = mountPanel()
    await flushPromises()

    expect(wrapper.get('.model-button').text()).toContain('默认提供商 · test-chat')
  })

  it('keeps a fallback label when the provider list cannot load', async () => {
    apiMocks.listLlmProviders.mockRejectedValue(new Error('down'))
    const wrapper = mountPanel()
    await flushPromises()

    expect(wrapper.get('.model-button').text()).toContain('模型')
  })

  it('switches provider and model through the picker menu', async () => {
    const wrapper = mountPanel()
    await flushPromises()

    await wrapper.get('button[title="切换模型与提供商"]').trigger('click')
    const options = wrapper.findAll('.model-option')
    expect(options).toHaveLength(3)
    const active = options.find((option) => option.classes().includes('active'))
    expect(active?.text()).toContain('test-chat')

    await options.find((option) => option.text().includes('other-reasoner'))!.trigger('click')
    expect(apiMocks.selectLlmProvider).toHaveBeenCalledWith('p-other', 'other-reasoner')
    await flushPromises()
    expect(wrapper.get('.model-button').text()).toContain('备选厂商 · other-reasoner')
  })

  it('disables the picker while a switch request is in flight', async () => {
    // phase10 P3-22/P2-1：快速连点两次会产生并发 in-flight POST，切换期间须禁用触发器。
    let resolveSwitch: (value: typeof providersSwitched) => void = () => {}
    apiMocks.selectLlmProvider.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveSwitch = resolve
      }),
    )
    const wrapper = mountPanel()
    await flushPromises()

    await wrapper.get('button[title="切换模型与提供商"]').trigger('click')
    await wrapper.findAll('.model-option')[1].trigger('click')
    expect(apiMocks.selectLlmProvider).toHaveBeenCalledTimes(1)
    expect(wrapper.get('button[title="切换模型与提供商"]').attributes('disabled')).toBeDefined()

    resolveSwitch(providersSwitched)
    await flushPromises()
    expect(wrapper.get('button[title="切换模型与提供商"]').attributes('disabled')).toBeUndefined()
  })

  it('keeps the original selection visible when switching fails', async () => {
    apiMocks.selectLlmProvider.mockRejectedValue(new Error('提供商未声明该模型'))
    const wrapper = mountPanel()
    await flushPromises()

    await wrapper.get('button[title="切换模型与提供商"]').trigger('click')
    await wrapper.findAll('.model-option')[1].trigger('click')
    await flushPromises()

    expect(wrapper.get('.model-button').text()).toContain('默认提供商 · test-chat')
    expect(wrapper.get('.provider-inline-error').text()).toContain('提供商未声明该模型')
  })

  it('adds a provider through the dialog and auto-selects its first model', async () => {
    apiMocks.addLlmProvider.mockResolvedValue(providerAdded)
    apiMocks.selectLlmProvider.mockResolvedValue({
      ...providerAdded,
      current: { provider_id: 'p-new', model: 'new-chat' },
    })
    const wrapper = mountPanel()
    await flushPromises()

    await wrapper.get('button[title="切换模型与提供商"]').trigger('click')
    await wrapper.get('.model-add-action').trigger('click')
    expect(wrapper.find('.dialog-backdrop').exists()).toBe(true)

    await wrapper.get('#provider-name').setValue('新建厂商')
    await wrapper.get('#provider-base-url').setValue('https://api.new.com')
    await wrapper.get('#provider-api-key').setValue('sk-new-0000')
    await wrapper.get('#provider-models').setValue('new-chat\nnew-mini\n')
    await wrapper.get('.dialog-backdrop form').trigger('submit')

    expect(apiMocks.addLlmProvider).toHaveBeenCalledWith({
      name: '新建厂商',
      base_url: 'https://api.new.com',
      api_key: 'sk-new-0000',
      models: ['new-chat', 'new-mini'],
    })
    await flushPromises()
    expect(apiMocks.selectLlmProvider).toHaveBeenCalledWith('p-new', 'new-chat')
    expect(wrapper.get('.model-button').text()).toContain('新建厂商 · new-chat')
    // 保存成功后对话框关闭
    expect(wrapper.find('.dialog-backdrop').exists()).toBe(false)
  })

  it('keeps the add dialog open with the server error when adding fails', async () => {
    apiMocks.addLlmProvider.mockRejectedValue(new Error('base_url 必须以 http:// 或 https:// 开头'))
    const wrapper = mountPanel()
    await flushPromises()

    await wrapper.get('button[title="切换模型与提供商"]').trigger('click')
    await wrapper.get('.model-add-action').trigger('click')
    await wrapper.get('#provider-name').setValue('坏地址')
    await wrapper.get('#provider-base-url').setValue('https://ok.com')
    await wrapper.get('#provider-api-key').setValue('sk-1')
    await wrapper.get('#provider-models').setValue('m')
    await wrapper.get('.dialog-backdrop form').trigger('submit')
    await flushPromises()

    expect(wrapper.find('.dialog-backdrop').exists()).toBe(true)
    expect(wrapper.get('.dialog-backdrop .inline-error').text()).toContain('base_url')
  })
})
