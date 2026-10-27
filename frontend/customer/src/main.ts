import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
// 三个侧边面板（历史工单 / SSH 终端 / 日志收集）共用的外观与拖拽样式
import '@/styles/workspace-panel.css'
// 面板底部操作区（composer）的统一规范：结构、高度、字号、按钮规格
import '@/styles/panel-composer.css'
import { isOfflineDiagnosisPath } from '@/utils/offlineDiagnosis'
import { installPersistentErrorMessages } from '@/utils/notify'

// 必须在任何业务代码弹错误提示之前装上：报错文案往往很长（路径 + 退出码 + 命令原文），
// 3s 自动关闭会让人读不完，实测反馈「来不及看就消失了」。
installPersistentErrorMessages()

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
