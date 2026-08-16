import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import AssistantDialog from './AssistantDialog.vue'

function mountDialog() {
  return mount(AssistantDialog, { props: { busy: false, error: '' } })
}

describe('AssistantDialog', () => {
  it('accepts ids with underscores matching the backend rule', async () => {
    const wrapper = mountDialog()
    await wrapper.get('#assistant-id').setValue('tech_writer')
    await wrapper.get('#assistant-name').setValue('技术作者')

    expect(wrapper.find('.inline-error').exists()).toBe(false)
    await wrapper.get('form').trigger('submit')
    expect(wrapper.emitted('submit')?.[0]).toEqual([{
      id: 'tech_writer', name: '技术作者', description: '',
    }])
  })

  it('rejects ids that violate the backend rule', async () => {
    const wrapper = mountDialog()
    await wrapper.get('#assistant-id').setValue('Bad-Id')

    expect(wrapper.get('.inline-error').text()).toContain('小写')
    await wrapper.get('#assistant-name').setValue('名字')
    await wrapper.get('form').trigger('submit')
    expect(wrapper.emitted('submit')).toBeUndefined()
  })

  it('mentions underscores in the validation message', async () => {
    const wrapper = mountDialog()
    await wrapper.get('#assistant-id').setValue('-bad')

    expect(wrapper.get('.inline-error').text()).toContain('下划线')
  })
})
