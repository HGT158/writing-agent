import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import SelectionToolbar from './SelectionToolbar.vue'

describe('SelectionToolbar', () => {
  it('submits a non-empty rewrite instruction', async () => {
    const wrapper = mount(SelectionToolbar, { props: { loading: false } })

    await wrapper.get('input').setValue('压缩成一句话')
    await wrapper.get('.primary-icon').trigger('click')

    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual(['压缩成一句话'])
    expect(wrapper.emitted('submit')).toHaveLength(1)
  })

  it('focuses the prompt input on mount', () => {
    const wrapper = mount(SelectionToolbar, {
      props: { loading: false },
      attachTo: document.body,
    })

    expect(document.activeElement).toBe(wrapper.get('input').element)
    wrapper.unmount()
  })

  it('lets the browser focus the input instead of preventing its mousedown', async () => {
    const wrapper = mount(SelectionToolbar, { props: { loading: false } })

    const onInput = new MouseEvent('mousedown', { bubbles: true, cancelable: true })
    wrapper.get('input').element.dispatchEvent(onInput)
    expect(onInput.defaultPrevented).toBe(false)

    const onSurface = new MouseEvent('mousedown', { bubbles: true, cancelable: true })
    wrapper.get('.selection-toolbar').element.dispatchEvent(onSurface)
    expect(onSurface.defaultPrevented).toBe(true)
  })

  it('cancels on Escape', async () => {
    const wrapper = mount(SelectionToolbar, { props: { loading: false } })

    await wrapper.get('input').trigger('keydown', { key: 'Escape' })

    expect(wrapper.emitted('cancel')).toHaveLength(1)
  })
})
