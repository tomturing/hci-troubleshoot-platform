import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import VerifyApp from './VerifyApp.vue'

// 管理端真浏览器验收：挂载产品里同一个 LogCollectAttribution 组件。
const app = createApp(VerifyApp)
app.use(createPinia())
app.use(ElementPlus)
app.mount('#app')