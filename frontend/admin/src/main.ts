import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import * as ElementPlusIconsVue from '@element-plus/icons-vue'
import 'element-plus/dist/index.css'
import App from './App.vue'
import router from './router'
import { setupAuthFetch, getAccessToken } from '@/utils/auth'

const app = createApp(App)
app.use(createPinia())
app.use(router)
app.use(ElementPlus)

// 注册所有图标（同时绑定原始 Key 及小写容错）
for (const [key, component] of Object.entries(ElementPlusIconsVue)) {
  app.component(key, component)
  if (key.toLowerCase() !== key) {
    app.component(key.toLowerCase(), component)
  }
}

setupAuthFetch()

// 同源身份层：向共享 API 客户端（axios，不经 window.fetch 拦截器）及离线诊断视图暴露
// 管理端 JWT 读取器，使所有管理端请求统一携带登录令牌（替代已移除的共享令牌回退）。
window.__HCI_AUTH__ = { getAccessToken: () => getAccessToken() ?? undefined }

app.mount('#app')
