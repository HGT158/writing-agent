<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue'
import { X } from '@lucide/vue'

const props = defineProps<{
  busy: boolean
  error: string
  /** create = 新建助手；edit = 编辑当前助手（v1.28，id 只读）。 */
  mode?: 'create' | 'edit'
  initial?: { id: string; name: string; description: string; persona: string }
}>()

const emit = defineEmits<{
  submit: [payload: { id: string; name: string; description: string; persona: string }]
  cancel: []
}>()

const isEdit = computed(() => props.mode === 'edit')
const id = ref(props.initial?.id ?? '')
const name = ref(props.initial?.name ?? '')
const description = ref(props.initial?.description ?? '')
const persona = ref(props.initial?.persona ?? '')
const idInput = ref<HTMLInputElement>()
const nameInput = ref<HTMLInputElement>()

// 与后端 AssistantRegistry 同一规则：^[a-z0-9][a-z0-9_-]{0,49}$（架构 §5.10 v1.21）。
const idPattern = /^[a-z0-9][a-z0-9_-]{0,49}$/
const idError = computed(() => {
  if (isEdit.value) return ''
  const value = id.value.trim()
  if (!value) return ''
  return idPattern.test(value)
    ? ''
    : '标识只能使用小写字母、数字、下划线或连字符（1-50 位），且以字母或数字开头'
})
const submittable = computed(() => (
  !!name.value.trim() && !idError.value && (isEdit.value || !!id.value.trim())
))

function submit() {
  if (!submittable.value) return
  emit('submit', {
    id: id.value.trim(),
    name: name.value.trim(),
    description: description.value.trim(),
    persona: persona.value,
  })
}

onMounted(async () => {
  await nextTick()
  ;(isEdit.value ? nameInput : idInput).value?.focus()
})
</script>

<template>
  <div class="dialog-backdrop" @mousedown.self="!busy && emit('cancel')">
    <section class="project-dialog" role="dialog" aria-modal="true" aria-labelledby="create-assistant-title">
      <div class="dialog-heading">
        <h2 id="create-assistant-title">{{ isEdit ? '编辑助手' : '新建助手' }}</h2>
        <button class="icon-action" title="关闭" :disabled="busy" @click="emit('cancel')"><X :size="16" /></button>
      </div>
      <form @submit.prevent="submit">
        <label for="assistant-id">标识（目录名，创建后不可改）</label>
        <input id="assistant-id" ref="idInput" v-model="id" maxlength="50" placeholder="tech-writer" :disabled="busy || isEdit" />
        <label for="assistant-name">显示名</label>
        <input id="assistant-name" ref="nameInput" v-model="name" maxlength="120" placeholder="科技作者" :disabled="busy" />
        <label for="assistant-description">描述（可选）</label>
        <input id="assistant-description" v-model="description" maxlength="500" placeholder="深度技术文章，注重引用来源" :disabled="busy" />
        <label for="assistant-persona">{{ isEdit ? '系统提示词（清空保存即恢复默认）' : '系统提示词（可选）' }}</label>
        <textarea id="assistant-persona" v-model="persona" rows="6" maxlength="50000" placeholder="你是一名严谨的编辑，注重……" :disabled="busy"></textarea>
        <p class="dialog-hint">{{ persona.length }} / 50000</p>
        <p class="dialog-hint">每个助手拥有独立的人设、记忆和文章项目，彼此不共享。</p>
        <p v-if="idError || error" class="inline-error">{{ idError || error }}</p>
        <div class="dialog-actions">
          <button type="button" class="secondary-action" :disabled="busy" @click="emit('cancel')">取消</button>
          <button type="submit" class="primary-action" :disabled="busy || !submittable">
            {{ busy ? (isEdit ? '保存中...' : '创建中...') : (isEdit ? '保存' : '创建') }}
          </button>
        </div>
      </form>
    </section>
  </div>
</template>
