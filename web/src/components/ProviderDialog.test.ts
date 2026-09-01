import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import ProviderDialog from './ProviderDialog.vue'

function mountDialog(overrides: Record<string, unknown> = {}) {
  return mount(ProviderDialog, {
    props: { busy: false, error: '', ...overrides },
    attachTo: document.body,
  })
}

describe('ProviderDialog', () => {
  it('disables submit until the required fields are valid', async () => {
    const wrapper = mountDialog()
    const submit = wrapper.get('button[type="submit"]')
    expect(submit.attributes('disabled')).toBeDefined()

    await wrapper.get('#provider-name').setValue('新建厂商')
    await wrapper.get('#provider-base-url').setValue('notaurl')
    await wrapper.get('#provider-api-key').setValue('sk-1')
    await wrapper.get('#provider-models').setValue('m')
    expect(submit.attributes('disabled')).toBeDefined()
    expect(wrapper.get('.inline-error').text()).toContain('base_url 必须以 http:// 或 https:// 开头')

    await wrapper.get('#provider-base-url').setValue('https://api.new.com')
    expect(submit.attributes('disabled')).toBeUndefined()
    wrapper.unmount()
  })

  it('requires at least one non-empty model line', async () => {
    const wrapper = mountDialog()
    await wrapper.get('#provider-name').setValue('新建厂商')
    await wrapper.get('#provider-base-url').setValue('https://api.new.com')
    await wrapper.get('#provider-api-key').setValue('sk-1')
    await wrapper.get('#provider-models').setValue('  \n  ')
    expect(wrapper.get('button[type="submit"]').attributes('disabled')).toBeDefined()
    wrapper.unmount()
  })

  it('validates the optional temperature range', async () => {
    const wrapper = mountDialog()
    await wrapper.get('#provider-name').setValue('新建厂商')
    await wrapper.get('#provider-base-url').setValue('https://api.new.com')
    await wrapper.get('#provider-api-key').setValue('sk-1')
    await wrapper.get('#provider-models').setValue('m')

    await wrapper.get('#provider-temperature').setValue('2.5')
    expect(wrapper.get('button[type="submit"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('.inline-error').text()).toContain('温度须在 0 到 2 之间')

    await wrapper.get('#provider-temperature').setValue('0.7')
    expect(wrapper.get('button[type="submit"]').attributes('disabled')).toBeUndefined()
    wrapper.unmount()
  })

  it('emits a trimmed payload and omits an empty temperature', async () => {
    const wrapper = mountDialog()
    await wrapper.get('#provider-name').setValue('  新建厂商  ')
    await wrapper.get('#provider-base-url').setValue(' https://api.new.com ')
    await wrapper.get('#provider-api-key').setValue(' sk-new-0000 ')
    await wrapper.get('#provider-models').setValue('new-chat\n  \nnew-mini\n')
    await wrapper.get('form').trigger('submit')

    const emitted = wrapper.emitted('submit')
    expect(emitted).toHaveLength(1)
    expect(emitted![0][0]).toEqual({
      name: '新建厂商',
      base_url: 'https://api.new.com',
      api_key: 'sk-new-0000',
      models: ['new-chat', 'new-mini'],
    })
    wrapper.unmount()
  })

  it('emits a numeric temperature only when provided', async () => {
    const wrapper = mountDialog()
    await wrapper.get('#provider-name').setValue('新建厂商')
    await wrapper.get('#provider-base-url').setValue('https://api.new.com')
    await wrapper.get('#provider-api-key').setValue('sk-1')
    await wrapper.get('#provider-models').setValue('m')
    await wrapper.get('#provider-temperature').setValue('0.7')
    await wrapper.get('form').trigger('submit')

    expect(wrapper.emitted('submit')![0][0]).toMatchObject({ temperature: 0.7 })
    wrapper.unmount()
  })

  it('shows the server error and closes via cancel', async () => {
    const wrapper = mountDialog({ error: '服务端拒绝' })
    expect(wrapper.get('.dialog-backdrop .inline-error').text()).toContain('服务端拒绝')

    await wrapper.get('.secondary-action').trigger('click')
    expect(wrapper.emitted('cancel')).toHaveLength(1)
    wrapper.unmount()
  })

  it('disables inputs while busy', () => {
    const wrapper = mountDialog({ busy: true })
    expect(wrapper.get('#provider-name').attributes('disabled')).toBeDefined()
    expect(wrapper.get('button[type="submit"]').attributes('disabled')).toBeDefined()
    wrapper.unmount()
  })
})
