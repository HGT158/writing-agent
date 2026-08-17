import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  DEFAULT_DARK_THEME,
  DEFAULT_LIGHT_THEME,
  THEME_STORAGE_KEY,
  THEMES,
  applyTheme,
  initialTheme,
  initTheme,
  watchSystemTheme,
} from './theme'

function stubMatchMedia(dark: boolean) {
  vi.stubGlobal('matchMedia', vi.fn().mockReturnValue({ matches: dark }))
}

describe('theme store', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
    localStorage.clear()
    delete document.documentElement.dataset.theme
  })

  it('falls back to the light default without storage or dark preference', () => {
    stubMatchMedia(false)
    expect(initialTheme()).toBe(DEFAULT_LIGHT_THEME)
  })

  it('honors the system dark preference on first visit', () => {
    stubMatchMedia(true)
    expect(initialTheme()).toBe(DEFAULT_DARK_THEME)
  })

  it('restores the persisted theme over system preference', () => {
    stubMatchMedia(true)
    localStorage.setItem(THEME_STORAGE_KEY, 'sepia')
    expect(initialTheme()).toBe('sepia')
  })

  it('falls back to system preference when the stored value is unknown', () => {
    stubMatchMedia(true)
    localStorage.setItem(THEME_STORAGE_KEY, 'not-a-theme')
    expect(initialTheme()).toBe(DEFAULT_DARK_THEME)
  })

  it('applies a theme to the document root and persists it', () => {
    stubMatchMedia(false)
    expect(applyTheme('ocean')).toBe(true)
    expect(document.documentElement.dataset.theme).toBe('ocean')
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe('ocean')
  })

  it('rejects unknown themes without touching state', () => {
    stubMatchMedia(false)
    initTheme()
    expect(applyTheme('bogus')).toBe(false)
    expect(document.documentElement.dataset.theme).toBe(DEFAULT_LIGHT_THEME)
  })

  it('does not crash when storage access is denied', () => {
    vi.stubGlobal('localStorage', {
      getItem: () => { throw new Error('denied') },
      setItem: () => { throw new Error('denied') },
    })
    stubMatchMedia(false)

    expect(initialTheme()).toBe(DEFAULT_LIGHT_THEME)
    expect(applyTheme('ink')).toBe(true)
    expect(document.documentElement.dataset.theme).toBe('ink')
  })

  it('does not persist the derived default on init', () => {
    stubMatchMedia(true)
    initTheme()

    expect(document.documentElement.dataset.theme).toBe(DEFAULT_DARK_THEME)
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBeNull()
  })

  it('follows system theme changes until the user picks a theme', () => {
    const media = { matches: false, addEventListener: vi.fn() }
    vi.stubGlobal('matchMedia', vi.fn().mockReturnValue(media))
    initTheme()
    watchSystemTheme()
    expect(document.documentElement.dataset.theme).toBe(DEFAULT_LIGHT_THEME)

    const handler = media.addEventListener.mock.calls[0]?.[1] as () => void
    media.matches = true
    handler()

    expect(document.documentElement.dataset.theme).toBe(DEFAULT_DARK_THEME)
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBeNull()
  })

  it('ignores system changes once the user picked a theme', () => {
    const media = { matches: false, addEventListener: vi.fn() }
    vi.stubGlobal('matchMedia', vi.fn().mockReturnValue(media))
    localStorage.setItem(THEME_STORAGE_KEY, 'sepia')
    initTheme()
    watchSystemTheme()

    const handler = media.addEventListener.mock.calls[0]?.[1] as () => void
    media.matches = true
    handler()

    expect(document.documentElement.dataset.theme).toBe('sepia')
  })

  it('exposes five distinct themes with names and swatches', () => {
    expect(THEMES).toHaveLength(5)
    expect(THEMES.map((theme) => theme.id)).toEqual(
      ['paper', 'ink', 'sepia', 'forest', 'ocean'],
    )
    for (const theme of THEMES) {
      expect(theme.name).toBeTruthy()
      expect(theme.swatch).toHaveLength(3)
      expect(typeof theme.dark).toBe('boolean')
    }
  })
})
