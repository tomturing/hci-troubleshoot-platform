// 管理员认证工具（阶段1.x）
//
// 职责：
// 1. 管理登录态（auth-service 签发的 JWT，存于 localStorage）
// 2. 暴露 login / logout / isAuthenticated
// 3. setupAuthFetch：全局注入 fetch 拦截器，自动为所有 /api 请求（登录端点除外）
//    附带 Bearer 凭证——已登录用 JWT，未登录回退共享内部令牌（灰度兼容）。
//    这样全站调用点无需逐一改造即可切换到 JWT 强认证。

const ACCESS_TOKEN_KEY = 'hci_admin_access_token'

// 兜底内部令牌：未登录时使用，兼容现有网关共享令牌逻辑。
// AUTHN_ENFORCE_ADMIN=false 时网关仍按 admin 处理；开启后回退失效，必须登录。
const FALLBACK_INTERNAL_TOKEN =
  (import.meta.env.VITE_INTERNAL_API_TOKEN as string | undefined) || 'hci-dev-internal-token'

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

export function isAuthenticated(): boolean {
  return !!getAccessToken()
}

// 统一认证头：优先 JWT，未登录回退共享令牌（灰度兼容现有网关逻辑）
export function authHeaders(): Record<string, string> {
  const token = getAccessToken()
  const bearer = token ?? FALLBACK_INTERNAL_TOKEN
  return { Authorization: `Bearer ${bearer}` }
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
}

// 全局 fetch 拦截：除登录端点外，自动注入 Bearer 凭证（JWT 优先，否则共享令牌）。
// 幂等：通过标记避免 HMR 重复包装。
export function setupAuthFetch(): void {
  if (typeof window === 'undefined') return
  const w = window as unknown as { __hciAuthFetchInstalled?: boolean }
  if (w.__hciAuthFetchInstalled) return
  w.__hciAuthFetchInstalled = true

  const original = window.fetch.bind(window)
  window.fetch = (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url =
      typeof input === 'string'
        ? input
        : input instanceof URL
          ? input.href
          : (input as Request).url
    // 登录端点由 body 携带凭证，不参与拦截注入
    if (!url.includes('/api/platform-auth/')) {
      const headers = new Headers(init.headers)
      const token = getAccessToken()
      const bearer = token ?? FALLBACK_INTERNAL_TOKEN
      headers.set('Authorization', `Bearer ${bearer}`)
      init = { ...init, headers }
    }
    return original(input, init)
  }
}
