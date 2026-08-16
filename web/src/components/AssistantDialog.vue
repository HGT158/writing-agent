<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue'
import { X } from '@lucide/vue'

defineProps<{ busy: boolean; error: string }>()
const emit = defineEmits<{
  submit: [payload: { id: string; name: string; description: string }]
  cancel: []
}>()

const id = ref('')
const name = ref('')
const description = ref('')
const input = ref<HTMLInputElement>()

// 与后端 AssistantRegistry 同一规则：^[a-z0-9][a-z0-9_-]{0,49}$（架构 §5.10 v1.21）。
const idPattern = /^[a-z0-9][a-z0-9_-]{0,49}$/
const idError = computed(() => {
  const value = id.value.trim()
  if (!value) return ''
  return idPattern.test(value)
    ? ''
    : '标识只能使用小写字母、数字、下划线或连字符（1-50 位），且以字母或数字开头'
})
const submittable = computed(() => !!id.value.trim() && !!name.value.trim() && !idError.value)

function submit() {
  if (!submittable.value) return
  emit('submit', {
    id: id.value.trim(),
    name: name.value.trim(),
    description: description.value.trim(),
  })
}

onMounted(async () => {
  await nextTick()
  input.value?.focus()
})
</script>

<template>
  <div class="dialog-backdrop" @mousedown.self="!busy && emit('cancel')">
    <section class="project-dialog" role="dialog" aria-modal="true" aria-labelledby="create-assistant-title">
      <div class="dialog-heading">
        <h2 id="create-assistant-title">新建助手</h2>
        <button class="icon-action" title="关闭" :disabled="busy" @click="emit('cancel')"><X :size="16" /></button>
      </div>
      <form @submit.prevent="submit">
        <label for="assistant-id">标识（目录名，创建后不可改）</label>
        <input id="assistant-id" ref="input" v-model="id" maxlength="50" placeholder="tech-writer" :disabled="busy" />
        <label for="assistant-name">显示名</label>
        <input id="assistant-name" v-model="name" maxlength="120" placeholder="科技作者" :disabled="busy" />
        <label for="assistant-description">描述（可选）</label>
        <input id="assistant-description" v-model="description" maxlength="500" placeholder="深度技术文章，注重引用来源" :disabled="busy" />
        <p class="dialog-hint">每个助手拥有独立的人设、记忆和文章项目，彼此不共享。</p>
        <p v-if="idError || error" class="inline-error">{{ idError || error }}</p>
        <div class="dialog-actions">
          <button type="button" class="secondary-action" :disabled="busy" @click="emit('cancel')">取消</button>
          <button type="submit" class="primary-action" :disabled="busy || !submittable">{{ busy ? '创建中...' : '创建' }}</button>
        </div>
      </form>
    </section>
  </div>
</template>
