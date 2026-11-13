import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import { showCustomerFault } from '../src/utils/logCollectFault'
import VerifyApp from './VerifyApp.vue'

const app = createApp(VerifyApp)
app.use(ElementPlus)
app.mount('#app')

// 供 Playwright 触发真实 Element Plus 弹框：这里调用的是产品里同一个
// showCustomerFault，而不是在验收页里复制一份文案逻辑。
;(window as unknown as { __lcShowFault: (code: string) => void }).__lcShowFault = (code) =>
  showCustomerFault('', code)
