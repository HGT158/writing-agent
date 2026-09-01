<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue'
import { X } from '@lucide/vue'

import type { LLMProviderCreatePayload } from '../types'

const props = defineProps<{
  busy: boolean
  error: string
}>()

const emit = defineEmits<{
  submit: [payload: LLMProviderCreatePayload]
  cancel: []
}>()

const name = ref('')
const baseUrl = ref('')
const apiKey = ref('')
const modelsText = ref('')
const temperatureText = ref('')
const nameInput = ref<HTMLInputElement>()

const models = computed(() => modelsText.value
  .split('\n')
  .map((item) => item.trim())
  .filter(Boolean))

const baseUrlError = computed(() => {
  const value = baseUrl.value.trim()
  if (!value) return ''
  return /^https?:\/\//.test(value) ? '' : 'base_url 必须以 http:// 或 https:// 开头'
})

const temperatureError = computed(() => {
  const text = temperatureText.value.trim()
  if (!text) return ''
  const value = Number(text)
  if (!Number.isFinite(value) || value < 0 || value > 2) return '温度须在 0 到 2 之间'
  return ''
})

const submittable = computed(() => (
  !!name.value.trim()
  && !!baseUrl.value.trim()
  && !!apiKey.value.trim()
  && models.value.length > 0
  && !baseUrlError.value
  && !temperatureError.value
))

function submit() {
  if (!submittable.value) return
  const temperature = temperatureText.value.trim()
  emit('submit', {
    name: name.value.trim(),
    base_url: baseUrl.value.trim(),
    api_key: apiKey.value.trim(),
    models: models.value,
    ...(temperature ? { temperature: Number(temperature) } : {}),
  })
}

onMounted(async () => {
  await nextTick()
  nameInput.value?.focus()
})
</script>

<template>
  <div class="dialog-backdrop" @mousedown.self="!busy && emit('cancel')">
    <section class="project-dialog" role="dialog" aria-modal="true" aria-labelledby="add-provider-title">
      <div class="dialog-heading">
        <h2 id="add-provider-title">添加提供商</h2>
        <button class="icon-action" title="关闭" :disabled="busy" @click="emit('cancel')"><X :size="16" /></button>
      </div>
      <form @submit.prevent="submit">
        <label for="provider-name">显示名</label>
        <input id="provider-name" ref="nameInput" v-model="name" maxlength="100" placeholder="DeepSeek / 通义 / 自建网关" :disabled="busy" />
        <label for="provider-base-url">base_url（OpenAI 兼容接口）</label>
        <input id="provider-base-url" v-model="baseUrl" maxlength="2000" placeholder="https://api.deepseek.com" :disabled="busy" />
        <p v-if="baseUrlError" class="inline-error">{{ baseUrlError }}</p>
        <label for="provider-api-key">API Key（仅保存在本机 llm_providers.json，不上传）</label>
        <input id="provider-api-key" v-model="apiKey" type="password" maxlength="4096" placeholder="sk-…" :disabled="busy" autocomplete="off" />
        <label for="provider-models">可用模型（每行一个）</label>
        <textarea id="provider-models" v-model="modelsText" rows="4" placeholder="deepseek-chat&#10;deepseek-reasoner" :disabled="busy"></textarea>
        <label for="provider-temperature">温度（可选，0–2，留空使用默认 0.3）</label>
        <input id="provider-temperature" v-model="temperatureText" inputmode="decimal" placeholder="0.3" :disabled="busy" />
        <p v-if="temperatureError" class="inline-error">{{ temperatureError }}</p>
        <p class="dialog-hint">新增后立即作为当前模型使用；服务暂不可用时可在菜单中切回原提供商。</p>
        <p v-if="error" class="inline-error">{{ error }}</p>
        <div class="dialog-actions">
          <button type="button" class="secondary-action" :disabled="busy" @click="emit('cancel')">取消</button>
          <button type="submit" class="primary-action" :disabled="busy || !submittable">
            {{ busy ? '添加中...' : '添加' }}
          </button>
        </div>
      </form>
    </section>
  </div>
</template>
