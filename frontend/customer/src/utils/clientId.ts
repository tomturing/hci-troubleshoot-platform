/**
 * 客户端ID管理 - 自动生成并持久化
 */

const STORAGE_KEY = 'hci-client-id'

export function getClientId(): string {
  let id = localStorage.getItem(STORAGE_KEY)
  if (!id) {
    id = `client-${Date.now().toString(36)}-${Math.random().toString(36).substring(2, 9)}`
    localStorage.setItem(STORAGE_KEY, id)
  }
  return id
}
