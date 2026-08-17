import { reactive, readonly } from 'vue'

export interface ThemeDefinition {
  id: string
  name: string
  /** 色板预览：[背景, 主色, 点缀色] */
  swatch: [string, string, string]
  dark: boolean
}

export const THEMES: ThemeDefinition[] = [
  { id: 'paper', name: '纸墨', swatch: ['#fbfbfc', '#126d5b', '#e4f1ed'], dark: false },
  { id: 'ink', name: '墨夜', swatch: ['#1e2227', '#7fd4bd', '#2c333a'], dark: true },
  { id: 'sepia', name: '暖卷', swatch: ['#f8f1e3', '#9a5b2d', '#efe2c8'], dark: false },
  { id: 'forest', name: '竹青', swatch: ['#f2f6f1', '#3f7d4e', '#dcecdf'], dark: false },
  { id: 'ocean', name: '海湾', swatch: ['#16202c', '#6fb3e0', '#243244'], dark: true },
]

export const THEME_STORAGE_KEY = 'writing-agent.theme'
export const DEFAULT_LIGHT_THEME = 'paper'
export const DEFAULT_DARK_THEME = 'ink'

const state = reactive({ current: DEFAULT_LIGHT_THEME })

function normalizeThemeId(value: string | null): string | null {
  return THEMES.some((theme) => theme.id === value) ? value : null
}

function prefersDark(): boolean {
  try {
    return typeof window.matchMedia === 'function'
      && window.matchMedia('(prefers-color-scheme: dark)').matches
  } catch {
    return false
  }
}

/** 存储被禁用（隐私模式等）时静默降级，不阻断应用启动。 */
function storedTheme(): string | null {
  try {
    return normalizeThemeId(localStorage.getItem(THEME_STORAGE_KEY))
  } catch {
    return null
  }
}

function persistTheme(themeId: string): void {
  try {
    localStorage.setItem(THEME_STORAGE_KEY, themeId)
  } catch {
    // 存储不可用时仅本次会话生效。
  }
}

function setTheme(themeId: string): boolean {
  const normalized = normalizeThemeId(themeId)
  if (!normalized) return false
  state.current = normalized
  document.documentElement.dataset.theme = normalized
  return true
}

export function initialTheme(): string {
  const stored = storedTheme()
  if (stored) return stored
  return prefersDark() ? DEFAULT_DARK_THEME : DEFAULT_LIGHT_THEME
}

/** 用户显式选择：立即生效并持久化；此后系统深浅变化不再联动。 */
export function applyTheme(themeId: string): boolean {
  if (!setTheme(themeId)) return false
  persistTheme(state.current)
  return true
}

/** 启动时应用一次；只读取存储不回写，避免把系统推导值标记为用户选择。 */
export function initTheme(): string {
  const theme = initialTheme()
  setTheme(theme)
  return theme
}

/** 系统深浅变化联动：仅在用户尚未手动选择时跟随。main.ts 启动时调用一次。 */
export function watchSystemTheme(): void {
  if (typeof window.matchMedia !== 'function') return
  const query = window.matchMedia('(prefers-color-scheme: dark)')
  const listener = () => {
    if (storedTheme() !== null) return  // 用户已手动选择，以选择为准
    setTheme(query.matches ? DEFAULT_DARK_THEME : DEFAULT_LIGHT_THEME)
  }
  if (typeof query.addEventListener === 'function') {
    query.addEventListener('change', listener)
  } else if (typeof (query as MediaQueryList).addListener === 'function') {
    ;(query as MediaQueryList).addListener(listener)
  }
}

export function useTheme() {
  return {
    themes: THEMES,
    current: readonly(state),
    apply: applyTheme,
  }
}
