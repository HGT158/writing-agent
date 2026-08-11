<script setup lang="ts">
import { Check, RefreshCw, X } from '@lucide/vue'
import type { ChangePreview } from '../types'

defineProps<{ change: ChangePreview | null; busy?: boolean }>()
defineEmits<{ apply: []; reject: []; regenerate: [] }>()
</script>

<template>
  <section v-if="change" class="change-diff" aria-label="AI 修改预览">
    <div class="diff-heading"><span>修改建议</span><span class="diff-source">{{ change.source === 'chat' ? 'Agent' : '选区' }}</span></div>
    <div class="diff-block removed"><span class="diff-sign">−</span>{{ change.original }}</div>
    <div class="diff-block added"><span class="diff-sign">+</span>{{ change.replacement }}</div>
    <div class="diff-actions">
      <button class="primary-action" :disabled="busy" @click="$emit('apply')"><Check :size="15" /> 接受</button>
      <button class="secondary-action" :disabled="busy" @click="$emit('regenerate')"><RefreshCw :size="15" /> 重试</button>
      <button class="icon-action" title="拒绝修改" :disabled="busy" @click="$emit('reject')"><X :size="16" /></button>
    </div>
  </section>
</template>
