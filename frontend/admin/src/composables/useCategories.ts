import { ref } from 'vue'

export interface CategoryOption {
  code: string
  name: string
  parent_code?: string
  description?: string
  level?: number
  path_labels?: string[]
}

// el-tree-select 树节点：label 展示名，children 承载分组层级（解A 下分组/叶子同文法，
// 层级完全由后端 parent_id/level 结构还原，不依赖 code 编码）。
export interface CategoryTreeNode {
  code: string
  name: string
  label: string
  level: number
  isLeaf: boolean
  children: CategoryTreeNode[]
}

// 后端 grouped=true 每条节点的真实字段（routes/categories.py: id=code, label=name, id_in_db=DB 主键）
export interface RawCategoryNode {
  code: string
  name: string
  level: number
  id_in_db: number
  parent_id: number | null
  path_labels?: string[]
}

export function extractNodes(data: { domains?: Record<string, RawCategoryNode[]> }): RawCategoryNode[] {
  const domains = data.domains ?? {}
  return Object.values(domains).flat()
}

export function flattenOptions(nodes: RawCategoryNode[]): CategoryOption[] {
  return nodes
    .map((n) => ({
      code: n.code,
      name: n.name,
      level: n.level,
      path_labels: n.path_labels,
    }))
    .sort((a, b) => a.code.localeCompare(b.code))
}

// 按 parent_id(DB 主键) → id_in_db 关联建树；level===1 为域根。解A 下 code 不表征角色，
// 分组/叶子的区分纯靠是否挂有 children（结构性判定），与页面 el-tree 展示口径一致。
export function buildCategoryTree(nodes: RawCategoryNode[]): CategoryTreeNode[] {
  const byId = new Map<number, CategoryTreeNode>()
  nodes.forEach((n) => {
    byId.set(n.id_in_db, {
      code: n.code,
      name: n.name,
      // label 保留旧「code  name」展示文本，兼顾层级缩进表达分组、且 filter 可搜 code/name
      label: `${n.code}  ${n.name}`,
      level: n.level,
      isLeaf: true,
      children: [],
    })
  })
  const roots: CategoryTreeNode[] = []
  nodes.forEach((n) => {
    const node = byId.get(n.id_in_db)
    if (!node) return
    const parent = n.parent_id != null ? byId.get(n.parent_id) : undefined
    if (parent) {
      parent.children.push(node)
    } else if (n.level === 1) {
      roots.push(node)
    }
  })
  const finalize = (arr: CategoryTreeNode[]) => {
    for (const x of arr) {
      x.isLeaf = x.children.length === 0
      if (x.children.length) finalize(x.children)
    }
    arr.sort((a, b) => a.code.localeCompare(b.code))
  }
  finalize(roots)
  return roots
}

export function useCategories() {
  // 全树扁平（含分组/域根）——兼容既有按 code 取数/展示的调用点
  const categoryOptions = ref<CategoryOption[]>([])
  // 全树嵌套结构——用于「分类筛选」el-tree-select，清晰呈现分组层级并可按子树级联筛选
  const categoryTree = ref<CategoryTreeNode[]>([])
  // 仅叶子（后端结构性 NOT EXISTS 判定）——用于「绑定/编辑」归类，
  // KBD/SOP 必须归到叶子，避免挂到分组父节点导致下游按叶子精确匹配查不到
  const leafCategoryOptions = ref<CategoryOption[]>([])
  const categoriesLoading = ref(false)

  async function fetchCategories() {
    categoriesLoading.value = true
    try {
      // 鉴权头由全局 fetch 拦截器（utils/auth.setupAuthFetch）统一注入登录 JWT；
      // 共享内部令牌已移除，构建产物不再携带。此处保留空对象以兼容既有调用点结构。
      const authHeader: Record<string, string> = {}
      const [treeResp, leafResp] = await Promise.all([
        fetch('/api/kb/categories?grouped=true', { headers: authHeader }),
        fetch('/api/kb/categories?grouped=true&leaf_only=true', { headers: authHeader }),
      ])
      if (treeResp.ok) {
        const treeData = await treeResp.json()
        const nodes = extractNodes(treeData)
        categoryOptions.value = flattenOptions(nodes)
        categoryTree.value = buildCategoryTree(nodes)
      }
      if (leafResp.ok) leafCategoryOptions.value = flattenOptions(extractNodes(await leafResp.json()))
    } catch (e) {
      console.error('加载分类基线失败', e)
    } finally {
      categoriesLoading.value = false
    }
  }

  return {
    categoryOptions,
    categoryTree,
    leafCategoryOptions,
    categoriesLoading,
    fetchCategories
  }
}
