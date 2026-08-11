<script setup lang="ts">
import { Bot, FolderOpen } from '@lucide/vue'

defineProps<{ activeSection: 'projects' | 'agent' | null }>()
defineEmits<{ select: [section: 'projects' | 'agent'] }>()
</script>

<template>
  <nav class="activity-bar" aria-label="工作区导航">
    <button class="activity-button" :class="{ active: activeSection === 'projects' }" title="项目" @click="$emit('select', 'projects')"><FolderOpen :size="18" /></button>
    <span class="activity-spacer" />
    <button class="activity-button" :class="{ active: activeSection === 'agent' }" title="Agent" @click="$emit('select', 'agent')"><Bot :size="18" /></button>
  </nav>
</template>
