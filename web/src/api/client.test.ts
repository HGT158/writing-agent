import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { apiClient } from './client'

class FakeEventSource {
  static instances: FakeEventSource[] = []
  onmessage: ((event: MessageEvent) => void) | null = null
  onerror: (() => void) | null = null
  onopen: (() => void) | null = null
  close = vi.fn()
  listeners = new Map<string, Set<EventListener>>()
  constructor(public readonly url: string) {
    FakeEventSource.instances.push(this)
  }
  addEventListener(type: string, listener: EventListener) {
    const listeners = this.listeners.get(type) ?? new Set<EventListener>()
    listeners.add(listener)
    this.listeners.set(type, listeners)
  }
  emit(type: string) {
    for (const listener of this.listeners.get(type) ?? []) listener(new Event(type))
  }
}

function message(payload: unknown): MessageEvent {
  return new MessageEvent('message', { data: JSON.stringify(payload) })
}

describe('apiClient.watchTask', () => {
  beforeEach(() => {
    FakeEventSource.instances = []
    vi.stubGlobal('EventSource', FakeEventSource)
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('scopes SSE by assistant and closes on parse errors without reconnecting', () => {
    const onEvent = vi.fn()
    const onError = vi.fn()
    apiClient.watchTask('writer-a', 'task-1', onEvent, onError)
    const source = FakeEventSource.instances[0]

    expect(source.url).toBe('/api/tasks/task-1/stream?assistant_id=writer-a')
    source.onmessage?.(new MessageEvent('message', { data: '{bad json' }))
    expect(source.close).toHaveBeenCalled()
    expect(onError).toHaveBeenCalledTimes(1)

    source.onerror?.()
    expect(FakeEventSource.instances).toHaveLength(1)
  })

  it('reconnects with the last seq cursor and dedups replayed events', async () => {
    const onEvent = vi.fn()
    const handle = apiClient.watchTask('writer-a', 'task-1', onEvent)
    const first = FakeEventSource.instances[0]
    first.onmessage?.(message({ type: 'token', data: { text: 'a' }, seq: 0 }))
    first.onmessage?.(message({ type: 'token', data: { text: 'b' }, seq: 1 }))
    first.onerror?.()

    await vi.advanceTimersByTimeAsync(500)
    const second = FakeEventSource.instances[1]
    expect(second.url).toBe('/api/tasks/task-1/stream?assistant_id=writer-a&after_seq=1')
    second.onmessage?.(message({ type: 'token', data: { text: 'b' }, seq: 1 }))
    second.onmessage?.(message({ type: 'token', data: { text: 'c' }, seq: 2 }))

    expect(onEvent).toHaveBeenCalledTimes(3)
    handle.close()
  })

  it('reconnects without a cursor when nothing was received yet', async () => {
    const handle = apiClient.watchTask('writer-a', 'task-1', vi.fn())
    FakeEventSource.instances[0].onerror?.()

    await vi.advanceTimersByTimeAsync(500)
    expect(FakeEventSource.instances[1].url).toBe('/api/tasks/task-1/stream?assistant_id=writer-a')
    handle.close()
  })

  it('forwards the gap signal and suppresses non-terminal events afterwards', async () => {
    const onEvent = vi.fn()
    const handle = apiClient.watchTask('writer-a', 'task-1', onEvent)
    const first = FakeEventSource.instances[0]
    first.onmessage?.(message({ type: 'token', data: { text: 'a' }, seq: 0 }))
    first.onerror?.()

    await vi.advanceTimersByTimeAsync(500)
    const second = FakeEventSource.instances[1]
    second.onmessage?.(message({ type: 'reconnect_gap', data: { after_seq: 0, available_from: 3 } }))
    second.onmessage?.(message({ type: 'token', data: { text: '后续' }, seq: 4 }))
    second.onmessage?.(message({ type: 'change_preview', data: { change_set_id: 'c1' }, seq: 5 }))
    second.onmessage?.(message({ type: 'task_done', data: {}, seq: 6 }))

    expect(onEvent).toHaveBeenCalledTimes(3)
    expect(onEvent.mock.calls[1][0].type).toBe('reconnect_gap')
    expect(onEvent.mock.calls[2][0].type).toBe('task_done')
    expect(second.close).toHaveBeenCalled()
    handle.close()
  })

  it('stops reconnecting once a terminal event arrives', async () => {
    const onEvent = vi.fn()
    const handle = apiClient.watchTask('writer-a', 'task-1', onEvent)
    const first = FakeEventSource.instances[0]
    first.onmessage?.(message({ type: 'token', data: { text: 'a' }, seq: 0 }))
    first.onerror?.()

    await vi.advanceTimersByTimeAsync(500)
    const second = FakeEventSource.instances[1]
    second.onmessage?.(message({ type: 'task_done', data: {}, seq: 1 }))
    await vi.advanceTimersByTimeAsync(60_000)

    expect(FakeEventSource.instances).toHaveLength(2)
    expect(second.close).toHaveBeenCalled()
    handle.close()
  })

  it('gives up after bounded retries and reports the failure', async () => {
    const onError = vi.fn()
    const handle = apiClient.watchTask('writer-a', 'task-1', vi.fn(), onError)
    for (const delay of [500, 1000, 2000, 4000, 8000, 8000]) {
      FakeEventSource.instances.at(-1)!.onerror?.()
      await vi.advanceTimersByTimeAsync(delay)
    }
    FakeEventSource.instances.at(-1)!.onerror?.()

    expect(FakeEventSource.instances).toHaveLength(7)
    expect(onError).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(60_000)
    expect(FakeEventSource.instances).toHaveLength(7)
    handle.close()
  })

  it('resets the retry backoff after a successful connection', async () => {
    const handle = apiClient.watchTask('writer-a', 'task-1', vi.fn())
    FakeEventSource.instances[0].onerror?.()
    await vi.advanceTimersByTimeAsync(500)
    const second = FakeEventSource.instances[1]
    second.onopen?.()
    second.onerror?.()

    await vi.advanceTimersByTimeAsync(500)
    expect(FakeEventSource.instances).toHaveLength(3)
    handle.close()
  })

  it('cancels pending reconnects when the handle is closed', async () => {
    const onError = vi.fn()
    const handle = apiClient.watchTask('writer-a', 'task-1', vi.fn(), onError)
    FakeEventSource.instances[0].onerror?.()
    handle.close()

    await vi.advanceTimersByTimeAsync(60_000)
    expect(FakeEventSource.instances).toHaveLength(1)
    expect(onError).not.toHaveBeenCalled()
  })

  it('reconnects when an open SSE connection is idle for 60 seconds', async () => {
    const handle = apiClient.watchTask('writer-a', 'task-1', vi.fn())
    const first = FakeEventSource.instances[0]
    first.onopen?.()

    await vi.advanceTimersByTimeAsync(60_000)
    expect(first.close).toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(500)
    expect(FakeEventSource.instances).toHaveLength(2)
    handle.close()
  })

  it('counts heartbeat events as SSE activity', async () => {
    const handle = apiClient.watchTask('writer-a', 'task-1', vi.fn())
    const source = FakeEventSource.instances[0]
    source.onopen?.()

    await vi.advanceTimersByTimeAsync(59_000)
    source.emit('heartbeat')
    await vi.advanceTimersByTimeAsync(59_000)
    expect(source.close).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(1_000)
    expect(source.close).toHaveBeenCalled()
    handle.close()
  })
})

describe('apiClient request timeout', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('aborts a fetch that does not settle within 60 seconds', async () => {
    const fetchMock = vi.fn((_path: string, init?: RequestInit) => new Promise<Response>(
      (_resolve, reject) => init?.signal?.addEventListener(
        'abort', () => reject(new DOMException('aborted', 'AbortError')),
      ),
    ))
    vi.stubGlobal('fetch', fetchMock)

    const pending = apiClient.listAssistants()
    const rejected = expect(pending).rejects.toThrow('请求超时')
    await vi.advanceTimersByTimeAsync(60_000)
    await rejected
    expect(fetchMock.mock.calls[0][1]?.signal?.aborted).toBe(true)
  })
})

describe('apiClient project chat reconciliation', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('reconciles explicitly with POST before reading session detail', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ reconciled_task_ids: [] }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        session: {}, messages: [], pending_changes: [], work_events: [],
      }), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)

    await apiClient.getProjectChatSession('writer-a', 'project-1', 'session-1')

    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(fetchMock.mock.calls[0][0]).toContain('/reconcile?assistant_id=writer-a')
    expect(fetchMock.mock.calls[0][1]?.method).toBe('POST')
    expect(fetchMock.mock.calls[1][0]).not.toContain('/reconcile')
  })
})
