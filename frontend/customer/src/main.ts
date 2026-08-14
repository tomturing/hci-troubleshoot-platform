import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import { isOfflineDiagnosisPath } from '@/utils/offlineDiagnosis'

async function bootstrap() {
  const rootComponent = isOfflineDiagnosisPath(window.location.pathname)
    ? (await import('./OfflineDiagnosisPage.vue')).default
    : (await import('./App.vue')).default
  const app = createApp(rootComponent)
  app.use(createPinia())
  app.use(ElementPlus)
  app.mount('#app')
}

void bootstrap()
