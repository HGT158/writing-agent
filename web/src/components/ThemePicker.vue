<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { Check, Palette } from '@lucide/vue'

import { useTheme } from '../theme'

const { themes, current, apply } = useTheme()
const open = ref(false)
const root = ref<HTMLElement>()
const trigger = ref<HTMLButtonElement>()
const options = ref<HTMLButtonElement[]>([])

function setOptionRef(el: unknown, index: number) {
  if (el instanceof HTMLButtonElement) options.value[index] = el
}

function toggle() {
  open.value = !open.value
  if (open.value) void focusActiveOption()
}

/** 打开后聚焦当前主题项（phase7 P3-9）：键盘用户不必先 Tab 穿透整份列表。 */
async function focusActiveOption() {
  await nextTick()
  if (!open.value) return
  const index = themes.findIndex((theme) => theme.id === current.current)
  ;(options.value[index >= 0 ? index : 0] ?? options.value[0])?.focus()
}

function choose(id: string) {
  apply(id)
  open.value = false
  trigger.value?.focus()
}

function onDocClick(event: MouseEvent) {
  if (!open.value) return
  if (root.value?.contains(event.target as Node)) return
  open.value = false
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape' && open.value) {
    open.value = false
    trigger.value?.focus()
  }
}

/** 菜单方向键导航（phase7 P3-9）：上下循环移动，Home/End 跳到首尾。 */
function onMenuKeydown(event: KeyboardEvent) {
  if (!open.value || !options.value.length) return
  const items = options.value
  const index = items.indexOf(document.activeElement as HTMLButtonElement)
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault()
    const delta = event.key === 'ArrowDown' ? 1 : -1
    const next = index < 0 ? 0 : (index + delta + items.length) % items.length
    items[next]?.focus()
  } else if (event.key === 'Home') {
    event.preventDefault()
    items[0]?.focus()
  } else if (event.key === 'End') {
    event.preventDefault()
    items[items.length - 1]?.focus()
  }
}

onMounted(() => {
  document.addEventListener('click', onDocClick)
  document.addEventListener('keydown', onKeydown)
})
onBeforeUnmount(() => {
  document.removeEventListener('click', onDocClick)
  document.removeEventListener('keydown', onKeydown)
})
</script>

<template>
  <div ref="root" class="theme-picker">
    <button
      ref="trigger"
      type="button"
      class="theme-button"
      title="切换主题"
      :aria-expanded="open"
      aria-haspopup="menu"
      @click="toggle"
    ><Palette :size="15" /></button>
    <div v-if="open" class="theme-options" role="menu" aria-label="主题" @keydown="onMenuKeydown">
      <button
        v-for="(theme, index) in themes"
        :key="theme.id"
        :ref="(el) => setOptionRef(el, index)"
        type="button"
        class="theme-option"
        :class="{ active: current.current === theme.id }"
        role="menuitemradio"
        :aria-checked="current.current === theme.id"
        :tabindex="current.current === theme.id ? 0 : -1"
        @click="choose(theme.id)"
      >
        <span class="theme-swatch">
          <span
            v-for="(color, colorIndex) in theme.swatch"
            :key="colorIndex"
            :style="{ background: color }"
          />
        </span>
        {{ theme.name }}
        <Check v-if="current.current === theme.id" :size="13" class="theme-check" />
      </button>
    </div>
  </div>
</template>
