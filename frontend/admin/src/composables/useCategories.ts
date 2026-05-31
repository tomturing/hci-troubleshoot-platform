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
      const internalToken = import.meta.env.VITE_INTERNAL_API_TOKEN || 'hci-dev-internal-token'
      const authHeader = { Authorization: `Bearer ${internalToken}` }
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
