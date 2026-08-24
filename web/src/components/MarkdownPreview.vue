<script setup lang="ts">
import { computed } from 'vue'
import DOMPurify from 'dompurify'
import { marked } from 'marked'

const props = defineProps<{ content: string }>()
const html = computed(() => {
  const rendered = marked.parse(props.content, { breaks: true, gfm: true })
  const sanitized = DOMPurify.sanitize(typeof rendered === 'string' ? rendered : '')
  const template = document.createElement('template')
  template.innerHTML = sanitized
  template.content.querySelectorAll('a').forEach((link) => {
    link.setAttribute('target', '_blank')
    link.setAttribute('rel', 'noopener noreferrer')
  })
  return template.innerHTML
})
</script>

<template><article class="markdown-preview" v-html="html" /></template>
