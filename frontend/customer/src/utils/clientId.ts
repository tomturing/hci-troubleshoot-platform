/**
 * 客户端ID管理 - 自动生成并持久化
 */

const STORAGE_KEY = 'hci-client-id'

export function getClientId(): string {
  let id = localStorage.getItem(STORAGE_KEY)
  if (!id) {
    id = `client-${crypto.randomUUID()}`
    localStorage.setItem(STORAGE_KEY, id)
  }
  return id
}
