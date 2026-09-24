// 管理员认证工具
//
// 职责：
// 1. 管理登录态（auth-service 签发的 RS256 JWT，存于 localStorage）
// 2. 暴露 login / logout / isAuthenticated / authHeaders / getAccessToken
// 3. setupAuthFetch：全局注入 fetch 拦截器，自动为所有 /api 请求（登录端点除外）
//    附带登录签发的 JWT，并对 401（会话失效）统一清令牌 + 跳登录页。
//
// 安全重构（SRC 管理台强制认证 · L1 删回退 + L3 前端守卫）：
// - 移除共享内部令牌回退（原 FALLBACK_INTERNAL_TOKEN）：构建产物不再携带任何内部服务令牌，
//   杜绝“前端硬编码共享令牌 → 任何人可冒充管理端”的 P0 穿透。
// - 未登录不再伪造管理端身份，由路由守卫强制跳登录页；令牌真伪与有效期一律以服务端 JWKS 验签为准，
//   前端过期判断仅为体验优化。

const ACCESS_TOKEN_KEY = 'hci_admin_access_token'

export interface LoginResponse {
  access_token: string
  token_type: string
  expires_in: number
}

export function getAccessToken(): string | null {
  return localStorage.getItem(ACCESS_TOKEN_KEY)
}

export function setAccessToken(token: string): void {
  localStorage.setItem(ACCESS_TOKEN_KEY, token)
}

export function clearAccessToken(): void {
  localStorage.removeItem(ACCESS_TOKEN_KEY)
}

// 解析 JWT 载荷中的 exp（秒级时间戳）。仅用于前端过期预判（体验优化），
// 不作为安全边界——令牌真伪与有效期一律以服务端 JWKS 验签为准。
function getTokenExpiry(token: string): number | null {
  try {
    const part = token.split('.')[1]
    if (!part) return null
    const padded = part.replace(/-/g, '+').replace(/_/g, '/')
    const json = decodeURIComponent(
      atob(padded)
        .split('')
        .map((c) => '%' + ('00' + c.charCodeAt(0).toString(16)).slice(-2))
        .join(''),
    )
    const exp = (JSON.parse(json) as { exp?: unknown }).exp
    return typeof exp === 'number' ? exp : null
  } catch {
    return null
  }
}

// 已登录 = 存在令牌且未过期（容忍 30s 时钟偏移）。无法解析 exp 时不阻断，
// 交由服务端 401 + fetch 拦截器兜底，避免误判把管理员挡在外面。
export function isAuthenticated(): boolean {
  const token = getAccessToken()
  if (!token) return false
  const exp = getTokenExpiry(token)
  if (exp === null) return true
  return exp * 1000 > Date.now() - 30_000
}

// 统一认证头：仅携带登录签发的 JWT；未登录返回空头（不再回退共享令牌）。
// 未携带有效 JWT 的管理端请求由网关按 require_admin 拒绝（401/403）。
export function authHeaders(): Record<string, string> {
  const token = getAccessToken()
  return token ? { Authorization: `Bearer ${token}` } : {}
}

// 结构化认证事件日志（前端可观测性）：统一 schema，便于排障与审计前端认证生命周期。
function logAuthEvent(event: string, detail: Record<string, unknown> = {}): void {
  // eslint-disable-next-line no-console
  console.info(`[admin-auth] ${event}`, {
    event,
    traceId: (window as unknown as { __TRACE_ID__?: string }).__TRACE_ID__ || '',
    ts: new Date().toISOString(),
    ...detail,
  })
}

// 跳转登录页并携带回跳地址；防重入避免并发 401 触发多次整页跳转。
let redirectingToLogin = false
function redirectToLogin(): void {
  if (redirectingToLogin) return
  if (window.location.pathname.startsWith('/login')) return
  redirectingToLogin = true
  const redirect = encodeURIComponent(window.location.pathname + window.location.search)
  logAuthEvent('redirect_to_login', { from: window.location.pathname })
  window.location.assign(`/login?redirect=${redirect}`)
}

export async function login(identifier: string, password: string): Promise<LoginResponse> {
  // 登录端点本身不带 Authorization，由 body 携带凭证
  const resp = await fetch('/api/platform-auth/admin/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ identifier, password }),
  })
  if (!resp.ok) {
    let detail = '登录失败，请检查账号或密码'
    try {
      const data = await resp.json()
      if (data && typeof data.detail === 'string') detail = data.detail
    } catch {
      // 忽略解析错误，使用默认提示
    }
    throw new Error(detail)
  }
  const data = (await resp.json()) as LoginResponse
  setAccessToken(data.access_token)
  return data
}

export function logout(): void {
  clearAccessToken()
  logAuthEvent('logout')
  // 登出后跳登录页并携带回跳地址，避免停留在受保护页面反复 401
  redirectToLogin()
}

// 全局 fetch 拦截：除登录端点外，自动注入登录签发的 JWT（不再回退共享令牌）；
// 并对 401（会话失效）统一清令牌 + 跳登录页携带回跳地址。
// 幂等：通过标记避免 HMR 重复包装。
export function setupAuthFetch(): void {
  if (typeof window === 'undefined') return
  const w = window as unknown as { __hciAuthFetchInstalled?: boolean }
  if (w.__hciAuthFetchInstalled) return
  w.__hciAuthFetchInstalled = true

  const original = window.fetch.bind(window)
  window.fetch = async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url =
      typeof input === 'string'
        ? input
        : input instanceof URL
          ? input.href
          : (input as Request).url
    const isAuthEndpoint = url.includes('/api/platform-auth/')
    // 登录端点由 body 携带凭证，不参与拦截注入
    if (!isAuthEndpoint) {
      const headers = new Headers(init.headers)
      const token = getAccessToken()
      // 仅注入真实 JWT；未登录不再伪造管理端身份（已删除共享令牌回退）
      if (token) headers.set('Authorization', `Bearer ${token}`)
      init = { ...init, headers }
    }
    const response = await original(input, init)
    // 401 拦截：JWT 过期/被吊销 → 清令牌并跳登录页。
    // 登录端点自身的 401（账号或密码错误）不在此处理，避免重定向死循环。
    if (!isAuthEndpoint && response.status === 401) {
      logAuthEvent('unauthorized', { url, status: 401 })
      clearAccessToken()
      redirectToLogin()
    }
    return response
  }
}
