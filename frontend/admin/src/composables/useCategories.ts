import { ref } from 'vue'

export interface CategoryOption {
  code: string
  name: string
  parent_code?: string
  description?: string
}

export function useCategories() {
  const categoryOptions = ref<CategoryOption[]>([])
  const categoriesLoading = ref(false)

  async function fetchCategories() {
    categoriesLoading.value = true
    try {
      // 鉴权头由全局 fetch 拦截器（utils/auth.setupAuthFetch）统一注入登录 JWT；
      // 共享内部令牌已移除，构建产物不再携带。此处保留空对象以兼容既有调用点结构。
      const authHeader: Record<string, string> = {}
      const resp = await fetch('/api/kb/categories?grouped=true', { headers: authHeader })
      if (!resp.ok) return
      const data: { domains?: Record<string, CategoryOption[]> } = await resp.json()
      const domains = data.domains ?? {}
      categoryOptions.value = Object.values(domains).flat().sort((a, b) => a.code.localeCompare(b.code))
    } catch (e) {
      console.error('加载分类基线失败', e)
    } finally {
      categoriesLoading.value = false
    }
  }

  return {
    categoryOptions,
    categoriesLoading,
    fetchCategories
  }
}
