import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const apiMocks = vi.hoisted(() => ({
  getMemoryProfile: vi.fn(),
  replaceMemoryProfile: vi.fn(),
}))

vi.mock('../api/client', () => ({ apiClient: apiMocks }))

import MemoryProfileDialog from './MemoryProfileDialog.vue'

function mountDialog() {
  return mount(MemoryProfileDialog, { props: { assistantId: 'default' } })
}

describe('MemoryProfileDialog', () => {
  beforeEach(() => {
    apiMocks.getMemoryProfile.mockReset()
    apiMocks.replaceMemoryProfile.mockReset()
  })

  it('loads the current profile and shows the char budget', async () => {
    apiMocks.getMemoryProfile.mockResolvedValue({ content: '- [偏好] 正文先讲工程案例' })
    const wrapper = mountDialog()
    await flushPromises()

    const textarea = wrapper.get('textarea')
    expect((textarea.element as HTMLTextAreaElement).value).toContain('正文先讲工程案例')
    expect(wrapper.text()).toContain('15 / 50000')
  })

  it('saves edits through the API and keeps the dialog open', async () => {
    apiMocks.getMemoryProfile.mockResolvedValue({ content: '' })
    apiMocks.replaceMemoryProfile.mockResolvedValue({ content: '- [风格] 短句为主' })
    const wrapper = mountDialog()
    await flushPromises()

    await wrapper.get('textarea').setValue('- [风格] 短句为主')
    await wrapper.get('button.primary-action').trigger('click')
    await flushPromises()

    expect(apiMocks.replaceMemoryProfile).toHaveBeenCalledWith('default', '- [风格] 短句为主')
    expect(wrapper.text()).toContain('已保存')
    expect(wrapper.find('textarea').exists()).toBe(true)
  })

  it('shows server rejection as-is and keeps local edits (409 busy)', async () => {
    apiMocks.getMemoryProfile.mockResolvedValue({ content: '旧画像' })
    apiMocks.replaceMemoryProfile.mockRejectedValue(new Error('助手 default 正忙'))
    const wrapper = mountDialog()
    await flushPromises()

    await wrapper.get('textarea').setValue('新画像')
    await wrapper.get('button.primary-action').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('助手 default 正忙')
    expect((wrapper.get('textarea').element as HTMLTextAreaElement).value).toBe('新画像')
  })

  it('shows load failures and offers no save until content is loaded', async () => {
    apiMocks.getMemoryProfile.mockRejectedValue(new Error('资源不存在'))
    const wrapper = mountDialog()
    await flushPromises()

    expect(wrapper.text()).toContain('资源不存在')
    expect(wrapper.find('textarea').exists()).toBe(false)
    expect(wrapper.text()).toContain('重试')
  })
})
