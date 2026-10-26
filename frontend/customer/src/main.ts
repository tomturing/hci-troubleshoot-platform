import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
// 三个侧边面板（历史工单 / SSH 终端 / 日志收集）共用的外观与拖拽样式
import '@/styles/workspace-panel.css'
// 面板底部操作区（composer）的统一规范：结构、高度、字号、按钮规格
import '@/styles/panel-composer.css'
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
