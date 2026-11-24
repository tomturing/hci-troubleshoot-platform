import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import * as ElementPlusIconsVue from '@element-plus/icons-vue'
import 'element-plus/dist/index.css'
import App from './App.vue'
import router from './router'
import { setupAuthFetch, getAccessToken, handleAuthExpired } from '@/utils/auth'

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

// axios 通道的鉴权失效统一处置（与 setupAuthFetch 的 fetch 401 对齐）：
// - 401：令牌过期/被吊销，必须重新登录；
// - 403：仅当网关明确回「需要管理员凭证」时才算登录失效——普通权限不足
//   不应把用户踢出登录页，保留原错误让页面提示更具体的原因。
window.addEventListener('hci:auth-error', (event) => {
  const info = (event as CustomEvent<{ status?: number; detail?: string }>).detail || {}
  const status = Number(info.status || 0)
  const text = String(info.detail || '')
  if (status === 401 || (status === 403 && /管理员|凭证|admin/i.test(text))) {
    handleAuthExpired()
  }
})

// 同源身份层：向共享 API 客户端（axios，不经 window.fetch 拦截器）及离线诊断视图暴露
// 管理端 JWT 读取器，使所有管理端请求统一携带登录令牌（替代已移除的共享令牌回退）。
window.__HCI_AUTH__ = { getAccessToken: () => getAccessToken() ?? undefined }

app.mount('#app')
