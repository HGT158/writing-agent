<script setup lang="ts">
import { Check, FileText, RefreshCw, X } from '@lucide/vue'
import type { ChangePreview } from '../types'

defineProps<{
  change: ChangePreview
  busy?: boolean
  label?: string
  regenerable?: boolean
}>()
defineEmits<{ apply: []; reject: []; regenerate: [] }>()
</script>

<template>
  <section class="change-diff" aria-label="AI 修改预览">
    <header class="diff-heading">
      <span class="diff-target"><FileText :size="13" />{{ label || '当前文档' }}</span>
      <span class="diff-source">{{ change.source === 'chat' ? 'Agent' : '选区改写' }}</span>
    </header>
    <div v-if="change.original" class="diff-block removed"><span class="diff-sign">−</span>{{ change.original }}</div>
    <div class="diff-block added"><span class="diff-sign">+</span>{{ change.replacement }}</div>
    <footer class="diff-actions">
      <button class="primary-action" :disabled="busy" @click="$emit('apply')"><Check :size="14" /> 接受</button>
      <button v-if="regenerable" class="secondary-action" :disabled="busy" @click="$emit('regenerate')"><RefreshCw :size="14" /> 重试</button>
      <button class="icon-action" title="拒绝修改" :disabled="busy" @click="$emit('reject')"><X :size="15" /></button>
    </footer>
  </section>
</template>
