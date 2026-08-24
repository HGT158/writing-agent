<script setup lang="ts">
import { onMounted, useTemplateRef } from 'vue'
import { Sparkles, X } from '@lucide/vue'

defineProps<{ loading: boolean; disabled?: boolean; disabledReason?: string }>()
const prompt = defineModel<string>({ default: '' })
const emit = defineEmits<{ submit: []; cancel: [] }>()
const input = useTemplateRef<HTMLInputElement>('input')

onMounted(() => input.value?.focus())

/**
 * 只对非输入控件区域阻止默认行为：这样点击浮层空白处不会清掉编辑器选区，
 * 而点击输入框时浏览器仍然会正常聚焦（架构 §5.10）。
 */
function keepEditorSelection(event: MouseEvent) {
  const target = event.target as HTMLElement | null
  if (target?.closest('input, textarea, button')) return
  event.preventDefault()
}
</script>

<template>
  <div class="selection-toolbar" @mousedown="keepEditorSelection" @keydown.esc.stop.prevent="emit('cancel')">
    <Sparkles :size="15" class="toolbar-glyph" />
    <input
      ref="input"
      v-model="prompt"
      placeholder="告诉 AI 如何修改这段..."
      :disabled="loading || disabled"
      @keydown.enter.prevent="emit('submit')"
    />
    <button class="primary-icon" :title="disabled ? disabledReason : '生成修改建议'" :disabled="loading || disabled || !prompt.trim()" @click="emit('submit')">
      {{ loading ? '生成中' : '改写' }}
    </button>
    <button class="ghost-icon" title="关闭工具栏 (Esc)" @click="emit('cancel')"><X :size="15" /></button>
    <span v-if="disabled && disabledReason" class="toolbar-disabled-reason">{{ disabledReason }}</span>
  </div>
</template>
