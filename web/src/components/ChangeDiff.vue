<script setup lang="ts">
import { computed } from 'vue'
import { Check, FileText, RefreshCw, X } from '@lucide/vue'
import type { ChangeSetPreview } from '../types'

const props = defineProps<{
  change: ChangeSetPreview
  busy?: boolean
  label?: string
  regenerable?: boolean
}>()
defineEmits<{ apply: []; reject: []; regenerate: []; open: [hunkId?: string] }>()
// 全失效卡片没有可接受项：隐藏"全部接受"，只保留放弃与重试入口。
const hasPending = computed(() => props.change.hunks.some((hunk) => hunk.status === 'pending'))
</script>

<template>
  <section class="change-diff" aria-label="AI 修改预览">
    <header class="diff-heading" role="button" tabindex="0" title="打开目标文档" @click="$emit('open')" @keydown.enter="$emit('open')">
      <span class="diff-target"><FileText :size="13" />{{ label || '当前文档' }}</span>
      <span class="diff-source">{{ change.source === 'chat' ? 'Agent' : '选区改写' }} · {{ change.hunks.length }} 处</span>
    </header>
    <div
      v-for="hunk in change.hunks"
      :key="hunk.hunk_id"
      class="diff-hunk"
      :class="hunk.status"
      role="button"
      tabindex="0"
      title="在编辑器中定位该处修改"
      @click="$emit('open', hunk.hunk_id)"
      @keydown.enter="$emit('open', hunk.hunk_id)"
    >
      <div v-if="hunk.original" class="diff-block removed"><span class="diff-sign">−</span>{{ hunk.original }}</div>
      <div class="diff-block added"><span class="diff-sign">+</span>{{ hunk.replacement }}</div>
      <span v-if="hunk.status === 'applied'" class="hunk-state applied">已应用</span>
      <span v-else-if="hunk.status === 'rejected'" class="hunk-state rejected">已放弃</span>
      <span v-else-if="hunk.status === 'stale'" class="hunk-state stale">已失效</span>
    </div>
    <footer class="diff-actions">
      <button v-if="hasPending" class="primary-action" :disabled="busy" @click="$emit('apply')"><Check :size="14" /> 全部接受</button>
      <button v-if="regenerable" class="secondary-action" :disabled="busy" @click="$emit('regenerate')"><RefreshCw :size="14" /> 重试</button>
      <button class="icon-action" title="放弃全部修改" :disabled="busy" @click="$emit('reject')"><X :size="15" /></button>
    </footer>
  </section>
</template>
