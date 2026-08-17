<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { Check, Palette } from '@lucide/vue'

import { useTheme } from '../theme'

const { themes, current, apply } = useTheme()
const open = ref(false)
const root = ref<HTMLElement>()

function toggle() {
  open.value = !open.value
}

function choose(id: string) {
  apply(id)
  open.value = false
}

function onDocClick(event: MouseEvent) {
  if (!open.value) return
  if (root.value?.contains(event.target as Node)) return
  open.value = false
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') open.value = false
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
      type="button"
      class="theme-button"
      title="切换主题"
      :aria-expanded="open"
      @click="toggle"
    ><Palette :size="15" /></button>
    <div v-if="open" class="theme-options" role="menu" aria-label="主题">
      <button
        v-for="theme in themes"
        :key="theme.id"
        type="button"
        class="theme-option"
        :class="{ active: current.current === theme.id }"
        role="menuitemradio"
        :aria-checked="current.current === theme.id"
        @click="choose(theme.id)"
      >
        <span class="theme-swatch">
          <span
            v-for="(color, index) in theme.swatch"
            :key="index"
            :style="{ background: color }"
          />
        </span>
        {{ theme.name }}
        <Check v-if="current.current === theme.id" :size="13" class="theme-check" />
      </button>
    </div>
  </div>
</template>
