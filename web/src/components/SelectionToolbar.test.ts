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
})
