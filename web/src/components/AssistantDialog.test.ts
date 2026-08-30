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
      id: 'tech_writer', name: '技术作者', description: '', persona: '',
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

  it('emits the typed persona in create mode', async () => {
    const wrapper = mountDialog()
    await wrapper.get('#assistant-id').setValue('editor')
    await wrapper.get('#assistant-name').setValue('编辑助手')
    await wrapper.get('#assistant-persona').setValue('你是一名毒舌编辑。')
    await wrapper.get('form').trigger('submit')

    expect(wrapper.emitted('submit')?.[0]).toEqual([{
      id: 'editor', name: '编辑助手', description: '', persona: '你是一名毒舌编辑。',
    }])
  })

  it('edit mode prefills values, keeps id read-only and emits the update payload', async () => {
    const wrapper = mount(AssistantDialog, {
      props: {
        busy: false,
        error: '',
        mode: 'edit',
        initial: { id: 'editor', name: '编辑助手', description: '润色', persona: '旧人设' },
      },
    })

    expect(wrapper.get('#assistant-id').attributes('disabled')).toBeDefined()
    expect(wrapper.get('#assistant-name').element as HTMLInputElement).toMatchObject({ value: '编辑助手' })
    expect(wrapper.get('#assistant-persona').element as HTMLTextAreaElement).toMatchObject({ value: '旧人设' })

    await wrapper.get('#assistant-name').setValue('新名字')
    await wrapper.get('#assistant-persona').setValue('新人设')
    await wrapper.get('form').trigger('submit')

    expect(wrapper.emitted('submit')?.[0]).toEqual([{
      id: 'editor', name: '新名字', description: '润色', persona: '新人设',
    }])
  })

  it('edit mode allows a blank persona so the backend resets to default', async () => {
    const wrapper = mount(AssistantDialog, {
      props: {
        busy: false,
        error: '',
        mode: 'edit',
        initial: { id: 'editor', name: '编辑助手', description: '', persona: '旧人设' },
      },
    })

    await wrapper.get('#assistant-persona').setValue('')
    await wrapper.get('form').trigger('submit')

    expect(wrapper.emitted('submit')?.[0]).toEqual([{
      id: 'editor', name: '编辑助手', description: '', persona: '',
    }])
  })
})
