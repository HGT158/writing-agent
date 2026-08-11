import { beforeEach, describe, expect, it, vi } from 'vitest'

import { apiClient } from './client'

class FakeEventSource {
  static instances: FakeEventSource[] = []
  onmessage: ((event: MessageEvent) => void) | null = null
  onerror: (() => void) | null = null
  close = vi.fn()
  constructor(public readonly url: string) {
    FakeEventSource.instances.push(this)
  }
}

describe('apiClient.watchTask', () => {
  beforeEach(() => {
    FakeEventSource.instances = []
    vi.stubGlobal('EventSource', FakeEventSource)
  })

  it('scopes SSE by assistant and closes on parse or network errors', () => {
    const onEvent = vi.fn()
    const onError = vi.fn()
    apiClient.watchTask('writer-a', 'task-1', onEvent, onError)
    const source = FakeEventSource.instances[0]

    expect(source.url).toBe('/api/tasks/task-1/stream?assistant_id=writer-a')
    source.onmessage?.(new MessageEvent('message', { data: '{bad json' }))
    source.onerror?.()

    expect(source.close).toHaveBeenCalledTimes(2)
    expect(onError).toHaveBeenCalled()
  })
})
