import { createApp } from 'vue'

import App from './App.vue'
import { initTheme, watchSystemTheme } from './theme'
import './styles.css'

initTheme()
watchSystemTheme()
createApp(App).mount('#app')

