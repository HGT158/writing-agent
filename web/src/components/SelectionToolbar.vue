<script setup lang="ts">
defineProps<{ loading: boolean }>()
const prompt = defineModel<string>({ default: '' })
defineEmits<{ submit: []; cancel: [] }>()
</script>

<template>
  <div class="selection-toolbar" @mousedown.prevent>
    <input v-model="prompt" autofocus placeholder="告诉 AI 如何修改..." :disabled="loading" @keydown.enter="$emit('submit')" />
    <button class="primary-icon" title="生成修改建议" :disabled="loading || !prompt.trim()" @click="$emit('submit')">{{ loading ? '...' : '改写' }}</button>
    <button class="ghost-icon" title="关闭工具栏" @click="$emit('cancel')">×</button>
  </div>
</template>

