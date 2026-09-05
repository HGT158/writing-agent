import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it } from 'vitest'

import ModelPicker from './ModelPicker.vue'
import type { LLMProvidersPayload } from '../types'

const payload: LLMProvidersPayload = {
  current: { provider_id: 'default', model: 'test-chat' },
  providers: [
    {
      id: 'default', name: '默认提供商', base_url: 'https://api.example.com',
      models: ['test-chat', 'test-lite'], temperature: 0.3, api_key_hint: 'sk-***7890',
    },
    {
      id: 'p-other', name: '备选厂商', base_url: 'https://api.other.com',
      models: ['other-chat'], temperature: 0.7, api_key_hint: 'sk-***9876',
    },
  ],
}

describe('ModelPicker', () => {
  beforeEach(() => {
    document.body.innerHTML = ''
  })

  it('shows a fallback label when no provider payload is loaded yet', () => {
    const wrapper = mount(ModelPicker, { props: { providers: null, busy: false } })
    expect(wrapper.get('.model-button').text()).toContain('模型')
  })

  it('shows the current provider and model on the trigger', () => {
    const wrapper = mount(ModelPicker, { props: { providers: payload, busy: false } })
    expect(wrapper.get('.model-button').text()).toContain('默认提供商 · test-chat')
  })

  it('opens a grouped menu, marks the current item and never shows api keys', async () => {
    const wrapper = mount(ModelPicker, {
      props: { providers: payload, busy: false },
      attachTo: document.body,
    })
    await wrapper.get('button[title="切换模型与提供商"]').trigger('click')

    const headers = wrapper.findAll('.model-provider-name').map((node) => node.text())
    expect(headers).toEqual(['默认提供商', '备选厂商'])
    const options = wrapper.findAll('.model-option')
    expect(options).toHaveLength(3)
    expect(options[0].text()).toContain('test-chat')
    expect(options[1].text()).toContain('test-lite')
    expect(options[2].text()).toContain('other-chat')
    const active = options.filter((option) => option.classes().includes('active'))
    expect(active).toHaveLength(1)
    expect(active[0].text()).toContain('test-chat')
    expect(wrapper.find('.model-menu').text()).not.toContain('sk-')
    wrapper.unmount()
  })

  it('emits select with provider id and model when choosing an item', async () => {
    const wrapper = mount(ModelPicker, {
      props: { providers: payload, busy: false },
      attachTo: document.body,
    })
    await wrapper.get('button[title="切换模型与提供商"]').trigger('click')
    const option = wrapper
      .findAll('.model-option')
      .find((node) => node.text().includes('other-chat'))
    await option!.trigger('click')

    expect(wrapper.emitted('select')).toEqual([['p-other', 'other-chat']])
    expect(wrapper.find('.model-menu').exists()).toBe(false)
    wrapper.unmount()
  })

  it('emits add from the second-level entry', async () => {
    const wrapper = mount(ModelPicker, {
      props: { providers: payload, busy: false },
      attachTo: document.body,
    })
    await wrapper.get('button[title="切换模型与提供商"]').trigger('click')
    await wrapper.get('.model-add-action').trigger('click')

    expect(wrapper.emitted('add')).toHaveLength(1)
    expect(wrapper.find('.model-menu').exists()).toBe(false)
    wrapper.unmount()
  })

  it('closes on outside click and supports arrow key navigation', async () => {
    const wrapper = mount(ModelPicker, {
      props: { providers: payload, busy: false },
      attachTo: document.body,
    })
    await wrapper.get('button[title="切换模型与提供商"]').trigger('click')
    const options = wrapper.findAll('.model-option')
    await options[0].trigger('keydown', { key: 'ArrowDown' })
    expect(document.activeElement).toBe(options[1].element)
    await options[1].trigger('keydown', { key: 'ArrowUp' })
    expect(document.activeElement).toBe(options[0].element)

    document.body.click()
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(wrapper.find('.model-menu').exists()).toBe(false)
    wrapper.unmount()
  })

  it('includes the add-provider action in arrow key navigation', async () => {
    // phase10 P3-22：「添加提供商…」此前游离于 roving 循环之外，键盘用户只能 Tab 到达。
    const wrapper = mount(ModelPicker, {
      props: { providers: payload, busy: false },
      attachTo: document.body,
    })
    await wrapper.get('button[title="切换模型与提供商"]').trigger('click')
    const options = wrapper.findAll('.model-option')
    const addAction = wrapper.get('.model-add-action')

    ;(options[options.length - 1].element as HTMLButtonElement).focus()
    await options[options.length - 1].trigger('keydown', { key: 'ArrowDown' })
    expect(document.activeElement).toBe(addAction.element)
    await addAction.trigger('keydown', { key: 'ArrowDown' })
    expect(document.activeElement).toBe(options[0].element)
    await addAction.trigger('keydown', { key: 'End' })
    expect(document.activeElement).toBe(addAction.element)
    wrapper.unmount()
  })

  it('disables the trigger while busy', () => {
    const wrapper = mount(ModelPicker, { props: { providers: payload, busy: true } })
    expect(wrapper.get('button[title="切换模型与提供商"]').attributes('disabled')).toBeDefined()
  })
})
