<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { Check, ChevronDown, Plus } from '@lucide/vue'

import type { LLMProvidersPayload } from '../types'

const props = defineProps<{
  providers: LLMProvidersPayload | null
  busy: boolean
}>()

const emit = defineEmits<{
  select: [providerId: string, model: string]
  add: []
}>()

const open = ref(false)
const root = ref<HTMLElement>()
const trigger = ref<HTMLButtonElement>()
const options = ref<HTMLButtonElement[]>([])

type MenuItem =
  | { kind: 'header'; label: string }
  | { kind: 'model'; providerId: string; model: string; active: boolean }

const menuItems = computed<MenuItem[]>(() => {
  const payload = props.providers
  if (!payload) return []
  return payload.providers.flatMap((provider) => [
    { kind: 'header', label: provider.name } as MenuItem,
    ...provider.models.map((model) => ({
      kind: 'model',
      providerId: provider.id,
      model,
      active: payload.current.provider_id === provider.id && payload.current.model === model,
    } as MenuItem)),
  ])
})

const currentLabel = computed(() => {
  const payload = props.providers
  if (!payload) return '模型'
  const provider = payload.providers.find((item) => item.id === payload.current.provider_id)
  if (!provider) return '模型'
  return `${provider.name} · ${payload.current.model}`
})

function setOptionRef(el: unknown, index: number) {
  if (el instanceof HTMLButtonElement) options.value[index] = el
}

function toggle() {
  open.value = !open.value
  if (open.value) {
    options.value = []
    void focusCurrentOption()
  }
}

/** 打开后聚焦当前模型项（对齐主题菜单 phase7 P3-9 交互）。 */
async function focusCurrentOption() {
  await nextTick()
  if (!open.value) return
  const index = options.value.findIndex((option) => option?.dataset.active === 'true')
  const fallback = options.value.find(Boolean)
  ;(options.value[index >= 0 ? index : 0] ?? fallback)?.focus()
}

function choose(item: Extract<MenuItem, { kind: 'model' }>) {
  emit('select', item.providerId, item.model)
  open.value = false
  trigger.value?.focus()
}

function openAddDialog() {
  emit('add')
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

/** 菜单方向键导航：上下循环移动，Home/End 跳到首尾。 */
function onMenuKeydown(event: KeyboardEvent) {
  if (!open.value || !options.value.length) return
  const items = options.value.filter(Boolean)
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
  <div ref="root" class="model-picker">
    <button
      ref="trigger"
      type="button"
      class="model-button"
      title="切换模型与提供商"
      :aria-expanded="open"
      aria-haspopup="menu"
      :disabled="busy"
      @click="toggle"
    >
      <span class="model-label">{{ currentLabel }}</span>
      <ChevronDown :size="13" />
    </button>
    <div v-if="open" class="model-menu" role="menu" aria-label="模型与提供商" @keydown="onMenuKeydown">
      <template v-for="(item, index) in menuItems" :key="index">
        <div v-if="item.kind === 'header'" class="model-provider-name" role="presentation">{{ item.label }}</div>
        <button
          v-else
          :ref="(el) => setOptionRef(el, index)"
          type="button"
          class="model-option"
          :class="{ active: item.active }"
          :data-active="item.active"
          role="menuitemradio"
          :aria-checked="item.active"
          :tabindex="item.active ? 0 : -1"
          @click="choose(item)"
        >
          {{ item.model }}
          <Check v-if="item.active" :size="13" class="model-check" />
        </button>
      </template>
      <div v-if="!providers || !providers.providers.length" class="model-provider-name" role="presentation">
        暂无提供商配置
      </div>
      <button
        type="button"
        class="model-add-action"
        role="menuitem"
        :ref="(el) => setOptionRef(el, menuItems.length)"
        @click="openAddDialog"
      >
        <Plus :size="13" />
        添加提供商…
      </button>
    </div>
  </div>
</template>
