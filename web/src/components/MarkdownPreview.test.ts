import { mount } from '@vue/test-utils'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

import MarkdownPreview from './MarkdownPreview.vue'

describe('MarkdownPreview', () => {
  it('does not render executable HTML from untrusted markdown', () => {
    const wrapper = mount(MarkdownPreview, {
      props: { content: '<img src=x onerror="alert(1)">[safe](javascript:alert(2))' },
    })

    expect(wrapper.find('[onerror]').exists()).toBe(false)
    expect(wrapper.html()).not.toContain('javascript:')
  })

  it('does not allow previews to load remote tracking images', () => {
    const indexHtml = readFileSync(resolve(process.cwd(), 'index.html'), 'utf8')

    expect(indexHtml).toContain("img-src 'self' data:")
    expect(indexHtml).not.toMatch(/img-src[^;]*https:/)
  })
})
