<script setup lang="ts">
import { computed } from 'vue'
import DOMPurify from 'dompurify'
import { marked } from 'marked'

const props = defineProps<{ content: string }>()
const html = computed(() => {
  const rendered = marked.parse(props.content, { breaks: true, gfm: true })
  return DOMPurify.sanitize(typeof rendered === 'string' ? rendered : '')
})
</script>

<template><article class="markdown-preview" v-html="html" /></template>
