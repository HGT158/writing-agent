import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  build: {
    chunkSizeWarningLimit: 650,
    rollupOptions: {
      output: {
        manualChunks: {
          vue: ['vue'],
          codemirror: ['codemirror', '@codemirror/lang-markdown'],
          markdown: ['marked'],
          icons: ['@lucide/vue'],
        },
      },
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/testSetup.ts'],
  },
  server: {
    host: '127.0.0.1',
    proxy: {
      '/api': 'http://127.0.0.1:8000',
    },
  },
})
