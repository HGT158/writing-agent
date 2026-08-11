<script setup lang="ts">
import { nextTick, onMounted, ref } from 'vue'
import { X } from '@lucide/vue'

defineProps<{ busy: boolean; error: string }>()
const emit = defineEmits<{ submit: [name: string]; cancel: [] }>()
const name = ref('新文章')
const input = ref<HTMLInputElement>()

function submit() {
  const cleanName = name.value.trim()
  if (cleanName) emit('submit', cleanName)
}

onMounted(async () => {
  await nextTick()
  input.value?.select()
})
</script>

<template>
  <div class="dialog-backdrop" @mousedown.self="!busy && emit('cancel')">
    <section class="project-dialog" role="dialog" aria-modal="true" aria-labelledby="create-project-title">
      <div class="dialog-heading">
        <h2 id="create-project-title">新建文章项目</h2>
        <button class="icon-action" title="关闭" :disabled="busy" @click="emit('cancel')"><X :size="16" /></button>
      </div>
      <form @submit.prevent="submit">
        <label for="project-name">项目名称</label>
        <input id="project-name" ref="input" v-model="name" maxlength="120" :disabled="busy" />
        <p v-if="error" class="inline-error">{{ error }}</p>
        <div class="dialog-actions">
          <button type="button" class="secondary-action" :disabled="busy" @click="emit('cancel')">取消</button>
          <button type="submit" class="primary-action" :disabled="busy || !name.trim()">{{ busy ? '创建中...' : '创建' }}</button>
        </div>
      </form>
    </section>
  </div>
</template>
