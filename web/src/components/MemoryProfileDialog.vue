<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { X } from '@lucide/vue'

import { apiClient } from '../api/client'

const props = defineProps<{ assistantId: string }>()
const emit = defineEmits<{ close: [] }>()

// 与后端 ASSISTANT_PROFILE_MAX_CHARS 一致（架构 §5.9 v1.30）。
const PROFILE_MAX_CHARS = 50_000

const content = ref('')
const loading = ref(true)
const busy = ref(false)
const error = ref('')
const loadError = ref('')
const saved = ref(false)
const textarea = ref<HTMLTextAreaElement>()

const overLimit = computed(() => content.value.length > PROFILE_MAX_CHARS)

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const detail = await apiClient.getMemoryProfile(props.assistantId)
    content.value = detail.content
  } catch (cause) {
    loadError.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    loading.value = false
    textarea.value?.focus()
  }
}

onMounted(() => void load())

async function save() {
  if (busy.value || loading.value || overLimit.value) return
  busy.value = true
  error.value = ''
  saved.value = false
  try {
    const result = await apiClient.replaceMemoryProfile(props.assistantId, content.value)
    content.value = result.content
    saved.value = true
  } catch (cause) {
    // 409（助手任务运行中）/400（超上限）等服务端拒绝原样提示，不关闭对话框。
    error.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="dialog-backdrop" @mousedown.self="!busy && emit('close')">
    <section class="project-dialog memory-dialog" role="dialog" aria-modal="true" aria-labelledby="memory-profile-title">
      <div class="dialog-heading">
        <h2 id="memory-profile-title">记忆画像（profile.md）</h2>
        <button class="icon-action" title="关闭" :disabled="busy" @click="emit('close')"><X :size="16" /></button>
      </div>
      <p v-if="loading" class="dialog-hint">加载中…</p>
      <template v-else-if="loadError">
        <p class="inline-error">{{ loadError }}</p>
        <div class="dialog-actions">
          <button type="button" class="secondary-action" @click="emit('close')">关闭</button>
          <button type="button" class="primary-action" @click="load">重试</button>
        </div>
      </template>
      <template v-else>
        <p class="dialog-hint">Agent 与你共用这份画像：自动沉淀的偏好会追加进来，也可随时手工修改；保存后对后续 recall 立即生效。</p>
        <textarea
          ref="textarea"
          v-model="content"
          class="memory-textarea"
          rows="14"
          spellcheck="false"
          :disabled="busy"
        ></textarea>
        <p class="dialog-hint">{{ content.length }} / {{ PROFILE_MAX_CHARS }} 字符</p>
        <p v-if="error" class="inline-error">{{ error }}</p>
        <p v-else-if="saved" class="dialog-hint">已保存。</p>
        <div class="dialog-actions">
          <button type="button" class="secondary-action" :disabled="busy" @click="emit('close')">关闭</button>
          <button type="button" class="primary-action" :disabled="busy || overLimit" @click="save">
            {{ busy ? '保存中...' : '保存' }}
          </button>
        </div>
      </template>
    </section>
  </div>
</template>
