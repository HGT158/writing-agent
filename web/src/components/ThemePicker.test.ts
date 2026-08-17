import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import ThemePicker from './ThemePicker.vue'
import { THEME_STORAGE_KEY, THEMES } from '../theme'

describe('ThemePicker', () => {
  beforeEach(() => {
    localStorage.clear()
    delete document.documentElement.dataset.theme
    vi.unstubAllGlobals()
    vi.stubGlobal('matchMedia', vi.fn().mockReturnValue({ matches: false }))
  })

  it('opens the palette from the title-bar button', async () => {
    const wrapper = mount(ThemePicker, { attachTo: document.body })
    expect(wrapper.find('.theme-options').exists()).toBe(false)

    await wrapper.get('button[title="切换主题"]').trigger('click')
    expect(wrapper.findAll('.theme-option')).toHaveLength(THEMES.length)
    wrapper.unmount()
  })

  it('lists every theme by name and marks the active one', async () => {
    const wrapper = mount(ThemePicker, { attachTo: document.body })
    await wrapper.get('button[title="切换主题"]').trigger('click')

    const names = wrapper.findAll('.theme-option').map((option) => option.text())
    for (const theme of THEMES) {
      expect(names.some((text) => text.includes(theme.name))).toBe(true)
    }
    const active = wrapper.findAll('.theme-option').find((option) => option.classes().includes('active'))
    expect(active?.text()).toContain('纸墨')
    wrapper.unmount()
  })

  it('applies the chosen theme to the document root and persists it', async () => {
    const wrapper = mount(ThemePicker, { attachTo: document.body })
    await wrapper.get('button[title="切换主题"]').trigger('click')
    const option = wrapper
      .findAll('.theme-option')
      .find((node) => node.text().includes('墨夜'))
    await option!.trigger('click')

    expect(document.documentElement.dataset.theme).toBe('ink')
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe('ink')
    wrapper.unmount()
  })

  it('closes the palette after choosing and on outside click', async () => {
    const wrapper = mount(ThemePicker, { attachTo: document.body })
    await wrapper.get('button[title="切换主题"]').trigger('click')
    await wrapper.findAll('.theme-option')[0].trigger('click')
    expect(wrapper.find('.theme-options').exists()).toBe(false)

    await wrapper.get('button[title="切换主题"]').trigger('click')
    document.body.click()
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(wrapper.find('.theme-options').exists()).toBe(false)
    wrapper.unmount()
  })
})
