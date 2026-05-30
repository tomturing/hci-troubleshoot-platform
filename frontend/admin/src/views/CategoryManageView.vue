<script setup lang="ts">
import { ref, reactive, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Histogram, Upload, Download, WarningFilled, Plus, Edit, Delete, VideoPlay, VideoPause, RefreshRight, Share } from '@element-plus/icons-vue'
import { marked } from 'marked'
import DOMPurify, { type Config as DOMPurifyConfig } from 'dompurify'
import type { UploadFile, UploadRawFile, UploadInstance } from 'element-plus'

// ──────────────────────────────────────────────────────────────────────────────
// 类型定义（适配 kb_category 表结构）
// ──────────────────────────────────────────────────────────────────────────────
interface KbCategory {
  id: number                     // 数据库主键
  id_in_db?: number              // 别名：数据库主键（树形组件兼容）
  code: string                   // 业务键，如 "虚拟机-003"
  name: string                   // 分类名称
  domain: string                 // 一级技术域
  level: number                  // 层级 1-4
  parent_id: number | null       // 父节点数据库主键
  path_labels: string[]          // 完整路径
  hit_count: number              // S0 命中次数
  is_active: boolean             // 启用状态
  children?: KbCategory[]        // 子节点（树形结构）
  // 统计字段（后端子查询返回）
  published_kbd_count: number    // 已发布 KBD 数量
  published_sop_count: number    // 已发布 SOP 数量
}

interface DomainGroup {
  domain: string
  count: number
  categories: KbCategory[]
}

interface ImportDetailItem {
  index: number
  code: string
  status: 'would_create' | 'would_update' | 'error'
  name?: string
  level?: number
  reason?: string
}

interface ImportDiff {
  success: boolean
  dry_run: boolean
  yaml_categories: number  // YAML 原始叶节点数
  total: number            // 含 L1+中间层+叶节点的总节点数
  created: number          // 将新增节点数
  updated: number          // 将更新节点数
  errors: string[]
  details: ImportDetailItem[]
}

// ──────────────────────────────────────────────────────────────────────────────
// 已发布条目列表类型
// ──────────────────────────────────────────────────────────────────────────────
interface SopListItem {
  id: number
  title: string
  hit_count: number
  category_id: string | null
}

interface KbdListItem {
  id: number
  support_id: string
  title: string
  hit_count: number
  category_id: string | null
}

// ──────────────────────────────────────────────────────────────────────────────
// 鉴权头
// ──────────────────────────────────────────────────────────────────────────────
const internalToken = import.meta.env.VITE_INTERNAL_API_TOKEN || 'hci-dev-internal-token'
const authHeader = { Authorization: `Bearer ${internalToken}` }

// ──────────────────────────────────────────────────────────────────────────────
// 响应式状态
// ──────────────────────────────────────────────────────────────────────────────
const loading = ref(false)
const domainGroups = ref<DomainGroup[]>([])
const searchKeyword = ref('')
const filterDomain = ref('')
const filterActive = ref<boolean | null>(null)

// 统计信息
const totalCategories = ref(0)
const totalActive = ref(0)
const totalWithSop = ref(0)
const totalPublishedKbd = ref(0)

// ──────────────────────────────────────────────────────────────────────────────
// 响应式状态：右侧详情
// ──────────────────────────────────────────────────────────────────────────────
const selectedCategory = ref<KbCategory | null>(null)
const editSaving = ref(false)
const editForm = reactive({
  is_active: true,
})

// ──────────────────────────────────────────────────────────────────────────────
// 响应式状态：已发布 SOP/KBD 列表
// ──────────────────────────────────────────────────────────────────────────────
const publishedSopList = ref<SopListItem[]>([])
const publishedKbdList = ref<KbdListItem[]>([])
const listLoading = ref(false)
const listLoadError = ref<string | null>(null) // 列表加载失败消息

// ──────────────────────────────────────────────────────────────────────────────
// 响应式状态：详情弹窗
// ──────────────────────────────────────────────────────────────────────────────
const detailDialogVisible = ref(false)
const detailKbdEntry = ref<{
  id: number
  support_id: string
  title: string
  content_md: string
  hit_count: number
} | null>(null)
const detailSopEntry = ref<{
  id: number
  title: string
  content_md: string
  hit_count: number
} | null>(null)
const detailLoading = ref(false)
const detailHtml = ref('')

// ──────────────────────────────────────────────────────────────────────────────
// 响应式状态：YAML 导入
// ──────────────────────────────────────────────────────────────────────────────
const importDialogVisible = ref(false)
const importLoading = ref(false)
const importDiff = ref<ImportDiff | null>(null)
const pendingFile = ref<File | null>(null)
const importConfirming = ref(false)
const uploadRef = ref<UploadInstance>()

// ──────────────────────────────────────────────────────────────────────────────
// 计算属性：过滤后的域分组
// ──────────────────────────────────────────────────────────────────────────────
const filteredGroups = computed<DomainGroup[]>(() => {
  return domainGroups.value
    .filter((g) => !filterDomain.value || g.domain === filterDomain.value)
    .map((g) => ({
      ...g,
      categories: g.categories.filter((c) => {
        const matchKeyword =
          !searchKeyword.value ||
          c.name.includes(searchKeyword.value) ||
          c.code.includes(searchKeyword.value)
        const matchActive =
          filterActive.value === null || c.is_active === filterActive.value
        return matchKeyword && matchActive
      }),
    }))
    .filter((g) => g.categories.length > 0)
})

// ──────────────────────────────────────────────────────────────────────────────
// 计算属性：域汇总统计（统计每个域下所有活跃子分类的 SOP/KBD 数量之和）
// ──────────────────────────────────────────────────────────────────────────────
const domainStats = computed<Record<string, { sop: number; kbd: number }>>(() => {
  const stats: Record<string, { sop: number; kbd: number }> = {}
  const allCategories = domainGroups.value.flatMap((g) => g.categories)
  for (const cat of allCategories) {
    if (!cat.is_active) continue
    if (!stats[cat.domain]) {
      stats[cat.domain] = { sop: 0, kbd: 0 }
    }
    stats[cat.domain].sop += cat.published_sop_count || 0
    stats[cat.domain].kbd += cat.published_kbd_count || 0
  }
  return stats
})

// ──────────────────────────────────────────────────────────────────────────────
// 树级拓扑与可视化编辑核心逻辑
// ──────────────────────────────────────────────────────────────────────────────

// 新建分类的 Dialog 状态
const createDialogVisible = ref(false)
const createLoading = ref(false)
const createForm = reactive({
  name: '',
  code: '',
  parent_id_in_db: null as number | null,
  parent_code: '',
  parent_name: '',
  domain: '',
  keywordsString: '',
})

// 编辑分类的 Dialog 状态
const editDialogVisible = ref(false)
const editingCategory = ref<any>(null)
const editCategoryForm = reactive({
  name: '',
  code: '',
  keywordsString: '',
})

// 树状数据结构的计算属性：将平铺列表转化为无环森林
const globalCategoryTree = computed<KbCategory[]>(() => {
  const allCats = domainGroups.value.flatMap(g => g.categories)
  if (!allCats.length) return []

  const map: Record<string | number, KbCategory> = {}
  const roots: KbCategory[] = []

  // 1. 初始化节点映射，同时确保 parent_id 与 id_in_db 的关联关系
  allCats.forEach(cat => {
    map[cat.id_in_db ?? cat.id] = {
      ...cat,
      id_in_db: cat.id_in_db ?? cat.id,
      children: []
    } as KbCategory
  })

  // 2. 关联 parent_id 并找出根节点
  allCats.forEach(cat => {
    const key = cat.id_in_db ?? cat.id
    const node = map[key]
    const pid = cat.parent_id
    if (pid && map[pid]) {
      (map[pid].children as KbCategory[]).push(node)
    } else if (cat.level === 1) {
      roots.push(node)
    }
  })

  // 3. 对节点排序（按 level 升序，code/name 字典序）
  const sortTreeNodes = (nodes: KbCategory[]) => {
    nodes.sort((a, b) => {
      if (a.level !== b.level) return a.level - b.level
      return (a.code || '').localeCompare(b.code || '')
    })
    nodes.forEach(n => {
      if (n.children && n.children.length) {
        sortTreeNodes(n.children)
      }
    })
  }
  sortTreeNodes(roots)

  return roots
})

// 选中分类时的关联子树
const selectedSubtree = computed(() => {
  if (!selectedCategory.value) return []
  const allRoots = globalCategoryTree.value
  const targetCode = selectedCategory.value.code
  const targetId = selectedCategory.value.id_in_db ?? selectedCategory.value.id

  const hasNode = (node: any, code: string, dbId: number | string): boolean => {
    if (node.code === code || node.id_in_db === dbId) return true
    if (node.children && node.children.length) {
      return node.children.some((child: any) => hasNode(child, code, dbId))
    }
    return false
  }

  // 查找包含当前选中节点的那个 L1 大分类树作为其子树展示，这样不仅有子分类还有全景关联
  const matchedRoot = allRoots.find(root => hasNode(root, targetCode, targetId))
  return matchedRoot ? [matchedRoot] : []
})

// 树节点拖拽和拖放规则验证
function handleAllowDrag(node: any) {
  // L1 技术域大类作为分类基线根目录，不允许拖拽
  return node.data.level > 1
}

// 递归获取子树的最大深度 (深度验证 L4 限制)
function getSubtreeMaxLevel(node: any): number {
  if (!node.children || !node.children.length) {
    return node.level
  }
  return Math.max(...node.children.map((c: any) => getSubtreeMaxLevel(c)))
}

function handleAllowDrop(draggingNode: any, dropNode: any, type: string) {
  // 1. 不能拖放到 L1 技术域的 before 或 after（L1 大类是全局固定的根）
  if (dropNode.data.level === 1 && type !== 'inner') {
    return false
  }

  // 2. 被拖拽子树如果成为新父节点的子节点，深度不能超出 L4
  if (type === 'inner') {
    const draggingMaxLevel = getSubtreeMaxLevel(draggingNode.data)
    const draggingSubtreeHeight = draggingMaxLevel - draggingNode.data.level
    const targetParentLevel = dropNode.data.level
    if (targetParentLevel + 1 + draggingSubtreeHeight > 4) {
      return false
    }
  } else {
    // 作为 sibling 拖拽时，其新 level 会与 dropNode 保持一致
    const draggingMaxLevel = getSubtreeMaxLevel(draggingNode.data)
    const draggingSubtreeHeight = draggingMaxLevel - draggingNode.data.level
    const targetParentLevel = dropNode.parent ? dropNode.parent.data.level : 1
    if (targetParentLevel + 1 + draggingSubtreeHeight > 4) {
      return false
    }
  }

  return true
}

// 拖拽释放后，异步与后端级联通信
async function handleNodeDrop(draggingNode: any, dropNode: any, dropType: string) {
  loading.value = true
  let newParentId: number | null = null

  if (dropType === 'inner') {
    newParentId = dropNode.data.id_in_db ?? dropNode.data.id
  } else {
    // 'before' or 'after' -> 和目标节点平级，采用目标节点的 parent_id
    newParentId = dropNode.data.parent_id
  }

  try {
    const resp = await fetch(`/api/kb/categories/${encodeURIComponent(draggingNode.data.code)}/parent`, {
      method: 'PUT',
      headers: {
        'Content-Type': 'application/json',
        ...authHeader,
      },
      body: JSON.stringify({ parent_id: newParentId })
    })

    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}))
      throw new Error(err.detail || `HTTP ${resp.status}`)
    }

    ElMessage.success('拖拽调整成功，级联层级已同步更新')
    await fetchCategories()
    
    // 如果当前选中的分类恰好是被拖拽的分类，同步更新其在详情中的显示
    if (selectedCategory.value && selectedCategory.value.code === draggingNode.data.code) {
      const updated = domainGroups.value
        .flatMap(g => g.categories)
        .find(c => c.code === draggingNode.data.code)
      if (updated) {
        selectedCategory.value = updated
      }
    }
  } catch (e: any) {
    ElMessage.error(`拖拽调整失败: ${e.message}`)
    await fetchCategories()
  } finally {
    loading.value = false
  }
}

// 树节点直接点击查看详情
function handleNodeClick(data: any) {
  selectCategory(data)
}

// 可视化编辑：打开新增子分类 Modal
function openAddDialog(parentData: any) {
  createForm.name = ''
  createForm.code = ''
  createForm.parent_id_in_db = parentData.id_in_db ?? parentData.id
  createForm.parent_code = parentData.code
  createForm.parent_name = parentData.name
  createForm.domain = parentData.domain
  createForm.keywordsString = ''
  createDialogVisible.value = true
}

// 保存新增分类
async function handleCreateCategory() {
  if (!createForm.name.trim()) {
    ElMessage.warning('请输入分类名称')
    return
  }
  createLoading.value = true
  try {
    const keywords = createForm.keywordsString
      .split(/[,，\n]/)
      .map(k => k.trim())
      .filter(Boolean)

    const resp = await fetch('/api/kb/categories', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...authHeader,
      },
      body: JSON.stringify({
        name: createForm.name.trim(),
        domain: createForm.domain,
        parent_id: createForm.parent_id_in_db,
        code: createForm.code.trim() || undefined,
        keywords,
      })
    })

    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}))
      throw new Error(err.detail || `HTTP ${resp.status}`)
    }

    ElMessage.success('新增分类成功')
    createDialogVisible.value = false
    await fetchCategories()
  } catch (e: any) {
    ElMessage.error(`新增分类失败: ${e.message}`)
  } finally {
    createLoading.value = false
  }
}

// 可视化编辑：打开编辑分类 Modal
function openEditDialog(data: any) {
  editingCategory.value = data
  editCategoryForm.name = data.name
  editCategoryForm.code = data.code
  editCategoryForm.keywordsString = (data.keywords || []).join(', ')
  editDialogVisible.value = true
}

// 保存编辑分类
async function handleSaveEditCategory() {
  if (!editCategoryForm.name.trim()) {
    ElMessage.warning('分类名称不能为空')
    return
  }
  loading.value = true
  try {
    const keywords = editCategoryForm.keywordsString
      .split(/[,，\n]/)
      .map(k => k.trim())
      .filter(Boolean)

    const resp = await fetch(`/api/kb/categories/${encodeURIComponent(editCategoryForm.code)}`, {
      method: 'PUT',
      headers: {
        'Content-Type': 'application/json',
        ...authHeader,
      },
      body: JSON.stringify({
        name: editCategoryForm.name.trim(),
        keywords,
      })
    })

    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}))
      throw new Error(err.detail || `HTTP ${resp.status}`)
    }

    ElMessage.success('编辑分类成功')
    editDialogVisible.value = false
    await fetchCategories()

    if (selectedCategory.value && selectedCategory.value.code === editCategoryForm.code) {
      const updated = domainGroups.value
        .flatMap(g => g.categories)
        .find(c => c.code === editCategoryForm.code)
      if (updated) {
        selectedCategory.value = updated
      }
    }
  } catch (e: any) {
    ElMessage.error(`编辑分类失败: ${e.message}`)
  } finally {
    loading.value = false
  }
}

// 快速切换启用/禁用状态
async function toggleActiveStatus(data: any) {
  loading.value = true
  try {
    const newStatus = !data.is_active
    const resp = await fetch(`/api/kb/categories/${encodeURIComponent(data.code)}`, {
      method: 'PUT',
      headers: {
        'Content-Type': 'application/json',
        ...authHeader,
      },
      body: JSON.stringify({
        is_active: newStatus
      })
    })

    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}))
      throw new Error(err.detail || `HTTP ${resp.status}`)
    }

    ElMessage.success(`${newStatus ? '启用' : '禁用'}分类成功`)
    await fetchCategories()

    if (selectedCategory.value && selectedCategory.value.code === data.code) {
      const updated = domainGroups.value
        .flatMap(g => g.categories)
        .find(c => c.code === data.code)
      if (updated) {
        selectedCategory.value = updated
        editForm.is_active = newStatus
      }
    }
  } catch (e: any) {
    ElMessage.error(`操作失败: ${e.message}`)
  } finally {
    loading.value = false
  }
}

// 物理删除分类
async function handleDelete(data: KbCategory) {
  try {
    const confirmMsg = `此操作将永久删除分类「${data.name}」(${data.code})，若是中间层节点也会阻断。是否继续？`
    await ElMessageBox.confirm(confirmMsg, '危险提示', {
      confirmButtonText: '确定删除',
      cancelButtonText: '取消',
      type: 'warning',
      confirmButtonClass: 'el-button--danger'
    })
  } catch {
    return // 用户取消
  }

  loading.value = true
  try {
    const resp = await fetch(`/api/kb/categories/${encodeURIComponent(data.code)}`, {
      method: 'DELETE',
      headers: authHeader,
    })

    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}))
      throw new Error(err.detail || `HTTP ${resp.status}`)
    }

    ElMessage.success('分类删除成功')
    if (selectedCategory.value && selectedCategory.value.code === data.code) {
      selectedCategory.value = null
    }
    await fetchCategories()
  } catch (e: any) {
    ElMessage.error(`删除失败: ${e.message}`)
  } finally {
    loading.value = false
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// 数据加载
// ──────────────────────────────────────────────────────────────────────────────

// ──────────────────────────────────────────────────────────────────────────────
// 数据加载
// ──────────────────────────────────────────────────────────────────────────────
async function fetchCategories() {
  loading.value = true
  try {
    const resp = await fetch('/api/kb/categories?grouped=true&include_inactive=true', {
      headers: authHeader,
    })
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    const data = await resp.json()
    // 后端返回 { domains: { domain: [cat, ...] }, total_domains: N }
    // 转换为前端 DomainGroup[] 格式
    const domainsDict = (data.domains ?? {}) as Record<string, KbCategory[]>
    domainGroups.value = Object.entries(domainsDict).map(([domain, categories]) => ({
      domain,
      count: categories.length,
      categories,
    }))
    totalCategories.value = domainGroups.value.reduce((sum, g) => sum + g.count, 0)

    // 计算统计
    const allCategories = domainGroups.value.flatMap((g) => g.categories)
    totalActive.value = allCategories.filter((c) => c.is_active).length
    totalWithSop.value = allCategories.filter((c) => c.published_sop_count > 0).length
    // 计算已发布 KBD 总数（只统计活跃分类）
    totalPublishedKbd.value = allCategories
      .filter((c) => c.is_active)
      .reduce((sum, c) => sum + (c.published_kbd_count || 0), 0)
  } catch {
    ElMessage.error('加载分类失败，请刷新重试')
  } finally {
    loading.value = false
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// 选中分类
// ──────────────────────────────────────────────────────────────────────────────
function selectCategory(cat: KbCategory) {
  selectedCategory.value = cat
  editForm.is_active = cat.is_active
  // 加载已发布 SOP/KBD 列表
  fetchPublishedList(cat.code)
}

// ──────────────────────────────────────────────────────────────────────────────
// 加载已发布 SOP/KBD 列表
// ──────────────────────────────────────────────────────────────────────────────
async function fetchPublishedList(categoryCode: string) {
  listLoading.value = true
  listLoadError.value = null
  publishedSopList.value = []
  publishedKbdList.value = []
  try {
    // 查询已发布 KBD（通过 api-gateway /api/v1/ 前缀，传入 page_size 避免静默截断）
    const kbdResp = await fetch(`/api/v1/kbd/pending?status=published&category_id=${encodeURIComponent(categoryCode)}&page_size=100`, {
      headers: authHeader,
    })
    if (kbdResp.ok) {
      const kbdData = await kbdResp.json()
      // 竞态保护：写入前校验当前分类是否仍是发起请求时的分类
      if (selectedCategory.value?.code !== categoryCode) return
      publishedKbdList.value = (kbdData.entries || []).map((e: KbdListItem) => ({
        id: e.id,
        support_id: e.support_id,
        title: e.title,
        hit_count: e.hit_count || 0,
        category_id: e.category_id,
      }))
    }

    // 查询已发布 SOP（通过 api-gateway /api/v1/ 前缀，传入 page_size）
    const sopResp = await fetch(`/api/v1/sop?status=published&category_id=${encodeURIComponent(categoryCode)}&page_size=100`, {
      headers: authHeader,
    })
    if (sopResp.ok) {
      const sopData = await sopResp.json()
      // 竞态保护：写入前校验当前分类是否仍是发起请求时的分类
      if (selectedCategory.value?.code !== categoryCode) return
      publishedSopList.value = (sopData.documents || []).map((d: SopListItem) => ({
        id: d.id,
        title: d.title,
        hit_count: d.hit_count || 0,
        category_id: d.category_id,
      }))
    }
  } catch (e: unknown) {
    // 竞态保护：只有当前分类仍是发起时的分类才记录错误
    if (selectedCategory.value?.code === categoryCode) {
      listLoadError.value = `加载失败：${(e as Error).message}`
    }
    console.warn(`加载 ${categoryCode} 已发布列表失败:`, (e as Error).message)
  } finally {
    // 竞态保护：只有当前分类仍是发起时的分类才关闭 loading
    if (selectedCategory.value?.code === categoryCode) {
      listLoading.value = false
    }
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// 详情弹窗：KBD 详情
// ──────────────────────────────────────────────────────────────────────────────
async function openKbdDetail(kbdId: number) {
  detailLoading.value = true
  detailDialogVisible.value = true
  detailKbdEntry.value = null
  detailSopEntry.value = null
  detailHtml.value = ''
  try {
    const resp = await fetch(`/api/v1/kbd/${kbdId}`, { headers: authHeader })
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    const data = await resp.json()
    detailKbdEntry.value = data
    detailHtml.value = renderMarkdown(data.content_md || '')
  } catch {
    ElMessage.error('加载 KBD 详情失败')
    detailDialogVisible.value = false
  } finally {
    detailLoading.value = false
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// 详情弹窗：SOP 详情
// ──────────────────────────────────────────────────────────────────────────────
async function openSopDetail(sopId: number) {
  detailLoading.value = true
  detailDialogVisible.value = true
  detailKbdEntry.value = null
  detailSopEntry.value = null
  detailHtml.value = ''
  try {
    const resp = await fetch(`/api/v1/sop/${sopId}`, { headers: authHeader })
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    const data = await resp.json()
    detailSopEntry.value = data
    detailHtml.value = renderMarkdown(data.content_md || '')
  } catch {
    ElMessage.error('加载 SOP 详情失败')
    detailDialogVisible.value = false
  } finally {
    detailLoading.value = false
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// Markdown 渲染（使用 marked 库，符合业界最佳实践）
// ──────────────────────────────────────────────────────────────────────────────
// 配置 marked：GFM 模式（支持表格、代码块、任务列表等）
marked.setOptions({ gfm: true, breaks: true })

// 配置 DOMPurify：仅允许安全标签和属性，防止 XSS 注入
const DOMPURIFY_CONFIG: DOMPurifyConfig = {
  // 允许常见排版标签
  ALLOWED_TAGS: [
    'h1','h2','h3','h4','h5','h6',
    'p','br','hr','blockquote','pre','code',
    'ul','ol','li','table','thead','tbody','tr','th','td',
    'strong','em','del','a','img','span','div',
  ],
  // 允许安全属性；链接统一加 rel 防止 opener 攻击
  ALLOWED_ATTR: ['class','id','href','src','alt','title','rel','target'],
  // 强制所有链接添加安全属性
  FORCE_BODY: false,
  ADD_ATTR: [],
}

function renderMarkdown(md: string): string {
  if (!md) return ''
  const result = marked.parse(md)
  const raw = typeof result === 'string' ? result : ''
  // 对 marked 输出做 XSS 清洗，并为所有链接补充 rel="noopener noreferrer"
  // sanitize 返回 string | TrustedHTML，强转为 string
  const clean = String(DOMPurify.sanitize(raw, DOMPURIFY_CONFIG))
  // 使用 DOMParser 为外部链接补 rel/target（DOMPurify 清洗后做 DOM 操作最安全）
  const parser = new DOMParser()
  const doc = parser.parseFromString(clean, 'text/html')
  doc.querySelectorAll('a[href]').forEach((el) => {
    const href = el.getAttribute('href') || ''
    if (href.startsWith('http://') || href.startsWith('https://')) {
      el.setAttribute('rel', 'noopener noreferrer')
      el.setAttribute('target', '_blank')
    }
  })
  return doc.body.innerHTML
}

// ──────────────────────────────────────────────────────────────────────────────
// 保存编辑
// ──────────────────────────────────────────────────────────────────────────────
async function saveEdit() {
  if (!selectedCategory.value) return
  editSaving.value = true
  try {
    // 只允许修改 is_active 状态，分类名称通过 YAML 导入统一管理
    const body: Record<string, unknown> = {
      is_active: editForm.is_active,
    }
    const resp = await fetch(`/api/kb/categories/${encodeURIComponent(selectedCategory.value.code)}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', ...authHeader },
      body: JSON.stringify(body),
    })
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    ElMessage.success('保存成功')
    // 同步列表中该条目
    for (const g of domainGroups.value) {
      const idx = g.categories.findIndex((c) => c.code === selectedCategory.value!.code)
      if (idx >= 0) {
        g.categories[idx] = {
          ...g.categories[idx],
          is_active: editForm.is_active,
        }
        selectedCategory.value = g.categories[idx]
        break
      }
    }
    // 重新统计（包括 totalActive 和 totalPublishedKbd）
    const allCategories = domainGroups.value.flatMap((g) => g.categories)
    totalActive.value = allCategories.filter((c) => c.is_active).length
    totalPublishedKbd.value = allCategories
      .filter((c) => c.is_active)
      .reduce((sum, c) => sum + (c.published_kbd_count || 0), 0)
  } catch {
    ElMessage.error('保存失败，请重试')
  } finally {
    editSaving.value = false
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// YAML 导入 — 第一阶段（dry_run）
// ──────────────────────────────────────────────────────────────────────────────
async function handleFileUpload(uploadFile: UploadFile) {
  // 仅处理「已就绪」状态，跳过重复触发
  if (uploadFile.status !== 'ready') return
  const raw = uploadFile.raw as UploadRawFile | undefined
  if (!raw) return

  // 清空旧文件列表，保证只有当前一个文件
  uploadRef.value?.clearFiles()

  pendingFile.value = raw
  importLoading.value = true
  importDiff.value = null
  try {
    const form = new FormData()
    form.append('file', raw)
    const resp = await fetch('/api/kb/categories/import?dry_run=true', {
      method: 'POST',
      headers: authHeader,
      body: form,
    })
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}))
      // 后端 400 错误包含 detail.message 或 detail 字符串
      const msg = err.detail?.message ?? err.detail ?? `HTTP ${resp.status}`
      throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg))
    }
    // 后端直接返回 ImportDiff 对象（无 data 包装层）
    const data: ImportDiff = await resp.json()
    importDiff.value = data
  } catch (e: unknown) {
    ElMessage.error(`解析失败：${(e as Error).message}`)
    pendingFile.value = null
  } finally {
    importLoading.value = false
  }
}

// 超出文件数限制时：清旧文件并重新触发上传
function handleExceed(files: File[]) {
  uploadRef.value?.clearFiles()
  const file = files[0] as UploadRawFile
  uploadRef.value?.handleStart(file)
}

// ──────────────────────────────────────────────────────────────────────────────
// YAML 导入 — 第二阶段（确认写入）
// ──────────────────────────────────────────────────────────────────────────────
async function confirmImport() {
  if (!pendingFile.value) return
  importConfirming.value = true
  try {
    const form = new FormData()
    form.append('file', pendingFile.value)
    const resp = await fetch('/api/kb/categories/import?dry_run=false', {
      method: 'POST',
      headers: authHeader,
      body: form,
    })
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    ElMessage.success('导入成功，分类已更新')
    importDialogVisible.value = false
    importDiff.value = null
    pendingFile.value = null
    await fetchCategories()
  } catch {
    ElMessage.error('导入失败，请重试')
  } finally {
    importConfirming.value = false
  }
}

function cancelImport() {
  importDialogVisible.value = false
  importDiff.value = null
  pendingFile.value = null
  uploadRef.value?.clearFiles()
}

// ──────────────────────────────────────────────────────────────────────────────
// 导出 YAML
// ──────────────────────────────────────────────────────────────────────────────
async function exportYaml() {
  try {
    const resp = await fetch('/api/kb/categories/export', { headers: authHeader })
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    const blob = await resp.blob()
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `category_baseline_${new Date().toISOString().slice(0, 10)}.yaml`
    a.click()
    URL.revokeObjectURL(url)
  } catch {
    ElMessage.error('导出失败，请重试')
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// 挂载
// ──────────────────────────────────────────────────────────────────────────────
onMounted(fetchCategories)
</script>

<template>
  <div class="category-manage">
    <!-- ── 顶部工具栏 ── -->
    <div class="toolbar">
      <span class="page-title">
        <el-icon><Histogram /></el-icon>
        分类基线管理
      </span>
      <div class="toolbar-actions">
        <el-button :icon="Upload" @click="importDialogVisible = true">导入 YAML</el-button>
        <el-button :icon="Download" @click="exportYaml">导出 YAML</el-button>
      </div>
    </div>

    <!-- ── 主体：左树 + 右详情 ── -->
    <div class="main-layout" v-loading="loading">
      <!-- 左侧：分类树 -->
      <div class="left-panel">
        <!-- 搜索 / 过滤 -->
        <div class="filter-bar">
          <el-input
            v-model="searchKeyword"
            placeholder="搜索分类..."
            clearable
            style="margin-bottom: 8px"
          />
          <el-select v-model="filterDomain" placeholder="按域过滤" clearable style="width: 100%; margin-bottom: 8px">
            <el-option label="全部" value="" />
            <el-option label="虚拟机" value="虚拟机" />
            <el-option label="网络" value="网络" />
            <el-option label="存储" value="存储" />
            <el-option label="硬件" value="硬件" />
            <el-option label="平台" value="平台" />
          </el-select>
          <el-select v-model="filterActive" placeholder="按状态过滤" clearable style="width: 100%">
            <el-option label="全部" :value="null" />
            <el-option label="启用" :value="true" />
            <el-option label="禁用" :value="false" />
          </el-select>
        </div>

        <!-- 域分组列表 -->
        <div class="domain-list">
          <div
            v-for="group in filteredGroups"
            :key="group.domain"
            class="domain-section"
          >
            <div class="domain-header">
              <span class="domain-name">{{ group.domain }}</span>
              <span class="domain-count">{{ group.count }}</span>
              <span class="domain-stats">
                [SOP:{{ domainStats[group.domain]?.sop || 0 }}]
                [KBD:{{ domainStats[group.domain]?.kbd || 0 }}]
              </span>
            </div>
            <div
              v-for="cat in group.categories"
              :key="cat.code"
              class="category-item"
              :class="{
                selected: selectedCategory?.code === cat.code,
                inactive: !cat.is_active,
              }"
              @click="selectCategory(cat)"
            >
              <span class="cat-code">{{ cat.code }}</span>
              <span class="cat-name">{{ cat.name }}</span>
              <span class="count-tag">[SOP:{{ cat.published_sop_count || 0 }}]</span>
              <span class="count-tag">[KBD:{{ cat.published_kbd_count || 0 }}]</span>
              <span v-if="!cat.is_active" class="inactive-badge">禁用</span>
            </div>
          </div>
        </div>

        <!-- 统计 -->
        <div class="stats-bar">
          <span>总计: {{ totalCategories }}</span>
          <span>启用: {{ totalActive }}</span>
          <span>有SOP: {{ totalWithSop }}</span>
          <span>已发布KBD: {{ totalPublishedKbd }}</span>
        </div>
      </div>

      <!-- 右侧：详情编辑 -->
      <div class="right-panel">
        <!-- 未选择任何分类时，展示整棵全局分类基线树 -->
        <div v-if="!selectedCategory" class="global-tree-container">
          <div class="global-tree-header">
            <h3 class="panel-title-text">
              <el-icon><Share /></el-icon>
              全局分类基线树 (包含 {{ domainGroups.length }} 大域，共 {{ totalCategories }} 个节点)
            </h3>
            <span class="view-tip">提示：您可以直接在下方树结构中进行节点拖拽重组，或使用节点右侧的悬浮操作按钮增删分类。</span>
          </div>
          
          <div class="tree-scroll-wrapper">
            <el-tree
              :data="globalCategoryTree"
              node-key="code"
              :props="{ label: 'name', children: 'children' }"
              default-expand-all
              highlight-current
              draggable
              :allow-drag="handleAllowDrag"
              :allow-drop="handleAllowDrop"
              @node-drop="handleNodeDrop"
              @node-click="handleNodeClick"
            >
              <template #default="{ node, data }: { node: any; data: any }">
                <div class="custom-tree-node" :class="{ 'is-selected': selectedCategory?.code === data.code }">
                  <span class="level-pill" :class="`level-l${data.level}`">L{{ data.level }}</span>
                  <code class="node-code">{{ data.code }}</code>
                  <span class="node-name" :class="{ 'is-inactive-name': !data.is_active }">{{ data.name }}</span>
                  
                  <span class="stats-pills">
                    <span class="count-tag" v-if="data.published_sop_count > 0">[SOP:{{ data.published_sop_count }}]</span>
                    <span class="count-tag" v-if="data.published_kbd_count > 0">[KBD:{{ data.published_kbd_count }}]</span>
                    <span v-if="data.hit_count > 0" class="hit-count-pill">{{ data.hit_count }} 次命中</span>
                    <span v-if="!data.is_active" class="inactive-badge">已禁用</span>
                  </span>

                  <span class="node-actions" @click.stop>
                    <el-button
                      v-if="data.level < 4"
                      size="small"
                      link
                      type="primary"
                      :icon="Plus"
                      @click="openAddDialog(data)"
                      title="新增子分类"
                    />
                    <el-button
                      size="small"
                      link
                      type="primary"
                      :icon="Edit"
                      @click="openEditDialog(data)"
                      title="编辑分类"
                    />
                    <el-button
                      size="small"
                      link
                      :type="data.is_active ? 'warning' : 'success'"
                      :icon="data.is_active ? VideoPause : VideoPlay"
                      @click="toggleActiveStatus(data)"
                      :title="data.is_active ? '禁用分类' : '启用分类'"
                    />
                    <el-button
                      size="small"
                      link
                      type="danger"
                      :icon="Delete"
                      @click="handleDelete(data)"
                      title="删除分类"
                    />
                  </span>
                </div>
              </template>
            </el-tree>
          </div>
        </div>

        <div v-else class="detail-form">
          <!-- 标题行：分类详情 + 状态开关 + 保存按钮 -->
          <div class="detail-header">
            <h3 class="detail-title">分类详情</h3>
            <div class="detail-status">
              <el-radio-group v-model="editForm.is_active" size="small">
                <el-radio :value="true">启用</el-radio>
                <el-radio :value="false">禁用</el-radio>
              </el-radio-group>
            </div>
            <div class="detail-actions">
              <el-button type="primary" size="small" :loading="editSaving" @click="saveEdit">
                保存修改
              </el-button>
              <el-button size="small" @click="selectedCategory = null" style="margin-left: 8px">
                返回全局树
              </el-button>
            </div>
          </div>

          <!-- 基本信息：4列×3行表格 -->
          <table class="info-table">
            <tr>
              <td class="label">业务编码</td>
              <td class="value">{{ selectedCategory.code }}</td>
              <td class="label">分类名称</td>
              <td class="value">{{ selectedCategory.name }}</td>
            </tr>
            <tr>
              <td class="label">所属域</td>
              <td class="value">{{ selectedCategory.domain }}</td>
              <td class="label">完整路径</td>
              <td class="value">{{ selectedCategory.path_labels?.join(' / ') || '' }}</td>
            </tr>
            <tr>
              <td class="label">
                分类命中次数
                <el-tooltip
                  content="AI 将工单路由到本分类的累计次数（与下方文档命中次数独立统计）"
                  placement="top"
                  :trigger="['hover', 'focus']"
                >
                  <button
                    class="help-icon"
                    type="button"
                    aria-label="分类命中次数说明"
                  >?</button>
                </el-tooltip>
              </td>
              <td class="value hit-count-cell">{{ selectedCategory.hit_count }} 次</td>
              <td class="label">层级</td>
              <td class="value">L{{ selectedCategory.level }}</td>
            </tr>
          </table>

          <!-- 已发布 SOP 列表 -->
          <div class="published-section" v-if="selectedCategory.published_sop_count > 0">
            <h4 class="section-title">已发布 SOP ({{ selectedCategory.published_sop_count }}篇)</h4>
            <div class="published-list" v-loading="listLoading">
              <div
                v-for="sop in publishedSopList"
                :key="sop.id"
                class="published-item"
              >
                <span class="hit-tag">[命中:{{ sop.hit_count || 0 }}]</span>
                <span class="item-title">{{ sop.title }}</span>
                <el-button size="small" text type="primary" @click="openSopDetail(sop.id)">详情</el-button>
              </div>
            </div>
          </div>

          <!-- 已发布 KBD 列表 -->
          <div class="published-section" v-if="selectedCategory.published_kbd_count > 0">
            <h4 class="section-title">已发布 KBD ({{ selectedCategory.published_kbd_count }}篇)</h4>
            <div class="published-list" v-loading="listLoading">
              <div
                v-for="kbd in publishedKbdList"
                :key="kbd.id"
                class="published-item"
              >
                <span class="hit-tag">[命中:{{ kbd.hit_count || 0 }}]</span>
                <span class="item-title">{{ kbd.title }}</span>
                <el-button size="small" text type="primary" @click="openKbdDetail(kbd.id)">详情</el-button>
              </div>
            </div>
          </div>

          <!-- 列表加载失败占位 -->
          <div class="error-section" v-if="listLoadError && !listLoading">
            <el-icon class="error-icon"><WarningFilled /></el-icon>
            <span class="error-text">{{ listLoadError }}</span>
            <el-button size="small" text type="primary" @click="fetchPublishedList(selectedCategory.code)">重试</el-button>
          </div>

          <!-- 无数据提示 -->
          <div class="empty-section" v-if="selectedCategory.published_sop_count === 0 && selectedCategory.published_kbd_count === 0 && !listLoading && !listLoadError">
            <span class="empty-text">暂无已发布的 SOP/KBD</span>
          </div>

          <!-- ── 分类详情下方的关联分类分支子树 ── -->
          <div class="subtree-section">
            <h4 class="section-title">「{{ selectedCategory.name }}」相关的分类分支树</h4>
            <div class="tree-scroll-wrapper is-subtree">
              <el-tree
                :data="selectedSubtree"
                node-key="code"
                :props="{ label: 'name', children: 'children' }"
                default-expand-all
                highlight-current
                draggable
                :allow-drag="handleAllowDrag"
                :allow-drop="handleAllowDrop"
                @node-drop="handleNodeDrop"
                @node-click="handleNodeClick"
                :current-node-key="selectedCategory.code"
              >
                <template #default="{ node, data }: { node: any; data: any }">
                  <div class="custom-tree-node" :class="{ 'is-selected': selectedCategory?.code === data.code }">
                    <span class="level-pill" :class="`level-l${data.level}`">L{{ data.level }}</span>
                    <code class="node-code">{{ data.code }}</code>
                    <span class="node-name" :class="{ 'is-inactive-name': !data.is_active }">{{ data.name }}</span>
                    
                    <span class="stats-pills">
                      <span class="count-tag" v-if="data.published_sop_count > 0">[SOP:{{ data.published_sop_count }}]</span>
                      <span class="count-tag" v-if="data.published_kbd_count > 0">[KBD:{{ data.published_kbd_count }}]</span>
                      <span v-if="data.hit_count > 0" class="hit-count-pill">{{ data.hit_count }} 次命中</span>
                      <span v-if="!data.is_active" class="inactive-badge">已禁用</span>
                    </span>

                    <span class="node-actions" @click.stop>
                      <el-button
                        v-if="data.level < 4"
                        size="small"
                        link
                        type="primary"
                        :icon="Plus"
                        @click="openAddDialog(data)"
                        title="新增子分类"
                      />
                      <el-button
                        size="small"
                        link
                        type="primary"
                        :icon="Edit"
                        @click="openEditDialog(data)"
                        title="编辑分类"
                      />
                      <el-button
                        size="small"
                        link
                        :type="data.is_active ? 'warning' : 'success'"
                        :icon="data.is_active ? VideoPause : VideoPlay"
                        @click="toggleActiveStatus(data)"
                        :title="data.is_active ? '禁用分类' : '启用分类'"
                      />
                      <el-button
                        size="small"
                        link
                        type="danger"
                        :icon="Delete"
                        @click="handleDelete(data)"
                        title="删除分类"
                      />
                    </span>
                  </div>
                </template>
              </el-tree>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- ── YAML 导入 Dialog ── -->
    <el-dialog v-model="importDialogVisible" title="导入分类 YAML" width="600px">
      <div v-if="!importDiff">
        <el-upload
          ref="uploadRef"
          drag
          :auto-upload="false"
          :limit="1"
          :on-change="handleFileUpload"
          :on-exceed="handleExceed"
          accept=".yaml,.yml"
        >
          <el-icon size="48"><Upload /></el-icon>
          <div>拖拽或点击上传 category_baseline.yaml</div>
        </el-upload>
        <div v-if="importLoading" style="text-align:center;padding:12px 0;color:#409EFF">
          解析中，请稍候…
        </div>
      </div>

      <div v-else class="import-preview">
        <h4>预览结果</h4>
        <div class="preview-summary">
          <span>YAML 叶节点 {{ importDiff.yaml_categories }} 条，含中间层共 {{ importDiff.total }} 个节点</span>
          <span class="added">新增: {{ importDiff.created }}</span>
          <span class="modified">更新: {{ importDiff.updated }}</span>
        </div>

        <div v-if="importDiff.details.filter(d => d.status === 'would_create').length" class="diff-section">
          <h5>新增分类</h5>
          <ul>
            <li v-for="item in importDiff.details.filter(d => d.status === 'would_create')" :key="item.code">
              {{ item.code }} - {{ item.name }}
            </li>
          </ul>
        </div>

        <div v-if="importDiff.details.filter(d => d.status === 'would_update').length" class="diff-section">
          <h5>将更新分类</h5>
          <ul>
            <li v-for="item in importDiff.details.filter(d => d.status === 'would_update')" :key="item.code">
              {{ item.code }} - {{ item.name }}
            </li>
          </ul>
        </div>

        <div v-if="importDiff.errors.length" class="diff-section">
          <h5 style="color:#F56C6C">错误</h5>
          <ul>
            <li v-for="(err, i) in importDiff.errors" :key="i" style="color:#F56C6C">{{ err }}</li>
          </ul>
        </div>
      </div>

      <template #footer>
        <el-button @click="cancelImport">取消</el-button>
        <el-button
          v-if="importDiff"
          type="primary"
          :loading="importConfirming"
          @click="confirmImport"
        >
          确认导入
        </el-button>
      </template>
    </el-dialog>

    <!-- ── 新增分类 Dialog ── -->
    <el-dialog v-model="createDialogVisible" title="新增子分类" width="540px" destroy-on-close>
      <el-form :model="createForm" label-width="100px" label-position="left">
        <el-form-item label="所属父分类">
          <el-input :model-value="`${createForm.parent_name} (${createForm.parent_code})`" disabled />
        </el-form-item>
        <el-form-item label="所属技术域">
          <el-input v-model="createForm.domain" disabled />
        </el-form-item>
        <el-form-item label="分类名称" required>
          <el-input v-model="createForm.name" placeholder="请输入子分类名称，如: 重装系统" />
        </el-form-item>
        <el-form-item label="业务编码">
          <el-input v-model="createForm.code" placeholder="选填，若留空则自动生成 (建议自动生成)" />
        </el-form-item>
        <el-form-item label="触发关键字">
          <el-input
            v-model="createForm.keywordsString"
            type="textarea"
            :rows="2"
            placeholder="选填，多个关键字用逗号或换行分隔，如: 蓝屏, 慢, 系统重装"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="createLoading" @click="handleCreateCategory">保存</el-button>
      </template>
    </el-dialog>

    <!-- ── 编辑分类 Dialog ── -->
    <el-dialog v-model="editDialogVisible" title="编辑分类基线" width="540px" destroy-on-close>
      <el-form :model="editCategoryForm" label-width="100px" label-position="left">
        <el-form-item label="业务编码">
          <el-input v-model="editCategoryForm.code" disabled />
        </el-form-item>
        <el-form-item label="分类名称" required>
          <el-input v-model="editCategoryForm.name" placeholder="请输入分类名称" />
        </el-form-item>
        <el-form-item label="触发关键字">
          <el-input
            v-model="editCategoryForm.keywordsString"
            type="textarea"
            :rows="3"
            placeholder="多个关键字以中文/英文逗号或换行分隔"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editDialogVisible = false">取消</el-button>
        <el-button type="primary" @click="handleSaveEditCategory">保存</el-button>
      </template>
    </el-dialog>

    <!-- ── KBD/SOP 详情弹窗 ── -->
    <el-dialog
      v-model="detailDialogVisible"
      :title="detailKbdEntry?.title || detailSopEntry?.title || '详情'"
      width="min(960px, 90vw)"
      top="4vh"
    >
      <div v-loading="detailLoading" class="detail-content">
        <template v-if="detailKbdEntry">
          <!-- KBD 元信息 -->
          <div class="kbd-meta">
            <span>案例ID: {{ detailKbdEntry.support_id }}</span>
            <span>命中次数: {{ detailKbdEntry.hit_count || 0 }}</span>
          </div>
          <!-- KBD 内容渲染 -->
          <div class="kbd-content" v-html="detailHtml"></div>
        </template>
        <template v-else-if="detailSopEntry">
          <!-- SOP 元信息 -->
          <div class="kbd-meta">
            <span>命中次数: {{ detailSopEntry.hit_count || 0 }}</span>
          </div>
          <!-- SOP 内容渲染 -->
          <div class="kbd-content" v-html="detailHtml"></div>
        </template>
      </div>
      <template #footer>
        <el-button @click="detailDialogVisible = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.category-manage {
  height: 100%;
  display: flex;
  flex-direction: column;
}

.toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 16px;
  border-bottom: 1px solid #e4e7ed;
}

.page-title {
  font-size: 16px;
  font-weight: 600;
  display: flex;
  align-items: center;
  gap: 8px;
}

.main-layout {
  flex: 1;
  display: flex;
  overflow: hidden;
}

.left-panel {
  width: 420px;
  border-right: 1px solid #e4e7ed;
  display: flex;
  flex-direction: column;
}

.filter-bar {
  padding: 12px;
  border-bottom: 1px solid #e4e7ed;
}

.domain-list {
  flex: 1;
  overflow-y: auto;
  padding: 8px;
}

.domain-section {
  margin-bottom: 12px;
}

.domain-header {
  display: flex;
  justify-content: space-between;
  padding: 8px;
  background: #f5f7fa;
  border-radius: 4px;
  font-weight: 600;
}

.domain-count {
  color: #909399;
  font-size: 12px;
}

.category-item {
  display: flex;
  align-items: center;
  padding: 8px 12px;
  cursor: pointer;
  border-radius: 4px;
  margin: 2px 0;
}

.category-item:hover {
  background: #f0f2f5;
}

.category-item.selected {
  background: #ecf5ff;
}

.category-item.inactive {
  opacity: 0.5;
}

.cat-code {
  font-family: monospace;
  font-size: 12px;
  color: #606266;
  min-width: 80px;
}

.cat-name {
  flex: 1;
  margin-left: 8px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sop-badge {
  font-size: 10px;
  padding: 2px 6px;
  background: #67c23a;
  color: white;
  border-radius: 4px;
  margin-left: 4px;
}

.count-tag {
  font-size: 10px;
  padding: 2px 4px;
  background: #f0f2f5;
  color: #606266;
  border-radius: 2px;
  margin-left: 4px;
}

.domain-stats {
  font-size: 12px;
  color: #909399;
  margin-left: 8px;
}

.inactive-badge {
  font-size: 10px;
  padding: 2px 6px;
  background: #909399;
  color: white;
  border-radius: 4px;
  margin-left: 4px;
}

.stats-bar {
  padding: 12px;
  border-top: 1px solid #e4e7ed;
  display: flex;
  justify-content: space-around;
  font-size: 12px;
  color: #909399;
}

.right-panel {
  flex: 1;
  padding: 24px;
  overflow-y: auto;
}

.empty-state {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
  color: #909399;
}

.detail-header {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 12px 0;
  border-bottom: 1px solid #e4e7ed;
  margin-bottom: 16px;
}

.detail-title {
  font-size: 16px;
  font-weight: 600;
  margin: 0;
  padding: 0;
  border-bottom: none;
  line-height: 1;
}

.detail-status {
  display: flex;
  align-items: center;
  gap: 12px;
}

.detail-actions {
  margin-left: auto;
}

.info-table {
  width: 100%;
  border-collapse: collapse;
  margin-bottom: 16px;
}

.info-table td {
  padding: 8px 12px;
  border: 1px solid #e4e7ed;
}

.info-table .label {
  background: #f5f7fa;
  font-weight: 500;
  color: #606266;
  width: 100px;
}

.info-table .value {
  color: #303133;
}

.empty-section {
  padding: 24px 0;
  text-align: center;
}

.empty-text {
  color: #909399;
  font-size: 13px;
}

.error-section {
  padding: 16px 0;
  text-align: center;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
}

.error-icon {
  color: #f56c6c;
  font-size: 16px;
}

.error-text {
  color: #f56c6c;
  font-size: 13px;
}

.published-section {
  margin-bottom: 16px;
}

.section-title {
  font-size: 14px;
  font-weight: 500;
  color: #606266;
  margin: 16px 0 8px 0;
  padding-bottom: 8px;
  border-bottom: 1px solid #ebeef5;
}

.published-list {
  max-height: 200px;
  overflow-y: auto;
}

.published-item {
  display: flex;
  align-items: center;
  padding: 8px;
  border-radius: 4px;
  margin-bottom: 4px;
  background: #f5f7fa;
}

.hit-tag {
  font-size: 12px;
  padding: 2px 6px;
  background: #409eff;
  color: white;
  border-radius: 4px;
  margin-right: 8px;
}

.item-title {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
  color: #303133;
}

.detail-content {
  max-height: 78vh;
  overflow-y: auto;
  padding-right: 4px;
}

.kbd-meta {
  display: flex;
  gap: 16px;
  padding: 8px 0;
  color: #909399;
  font-size: 12px;
  border-bottom: 1px solid #ebeef5;
  margin-bottom: 16px;
}

/* ── Markdown 内容区：GitHub 风格排版 ── */
.kbd-content {
  font-size: 14px;
  line-height: 1.75;
  color: #24292f;
  word-break: break-word;
}

.kbd-content :deep(h1),
.kbd-content :deep(h2),
.kbd-content :deep(h3),
.kbd-content :deep(h4),
.kbd-content :deep(h5),
.kbd-content :deep(h6) {
  margin: 20px 0 8px 0;
  font-weight: 600;
  line-height: 1.25;
  color: #1f2328;
}

.kbd-content :deep(h1) { font-size: 20px; border-bottom: 1px solid #d0d7de; padding-bottom: 6px; }
.kbd-content :deep(h2) { font-size: 18px; border-bottom: 1px solid #d0d7de; padding-bottom: 4px; }
.kbd-content :deep(h3) { font-size: 16px; }
.kbd-content :deep(h4) { font-size: 14px; }

.kbd-content :deep(p) {
  margin: 8px 0;
}

.kbd-content :deep(ul),
.kbd-content :deep(ol) {
  margin: 8px 0;
  padding-left: 24px;
}

.kbd-content :deep(li) {
  margin: 4px 0;
}

.kbd-content :deep(li > ul),
.kbd-content :deep(li > ol) {
  margin: 2px 0;
}

.kbd-content :deep(blockquote) {
  margin: 10px 0;
  padding: 8px 16px;
  background: #f6f8fa;
  border-left: 4px solid #d0d7de;
  color: #57606a;
}

.kbd-content :deep(code) {
  background: #f6f8fa;
  padding: 2px 6px;
  border-radius: 6px;
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
  font-size: 85%;
  border: 1px solid #d0d7de;
}

.kbd-content :deep(pre) {
  background: #f6f8fa;
  border: 1px solid #d0d7de;
  border-radius: 6px;
  padding: 14px 16px;
  overflow-x: auto;
  margin: 12px 0;
  line-height: 1.45;
}

.kbd-content :deep(pre code) {
  background: transparent;
  padding: 0;
  border: none;
  font-size: 13px;
  border-radius: 0;
}

.kbd-content :deep(table) {
  width: 100%;
  border-collapse: collapse;
  margin: 12px 0;
  font-size: 13px;
}

.kbd-content :deep(th),
.kbd-content :deep(td) {
  border: 1px solid #d0d7de;
  padding: 6px 12px;
  text-align: left;
}

.kbd-content :deep(th) {
  background: #f6f8fa;
  font-weight: 600;
}

.kbd-content :deep(tr:nth-child(even) td) {
  background: #f6f8fa;
}

.kbd-content :deep(hr) {
  border: none;
  border-top: 1px solid #d0d7de;
  margin: 16px 0;
}

.kbd-content :deep(a) {
  color: #0969da;
  text-decoration: none;
}

.kbd-content :deep(strong) {
  font-weight: 600;
}

.hit-count-cell {
  font-weight: 600;
  color: #409eff;
}

.help-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: #909399;
  color: white;
  font-size: 10px;
  cursor: pointer;
  margin-left: 4px;
  vertical-align: middle;
  /* 重置按钮默认样式 */
  border: none;
  padding: 0;
  line-height: 1;
  /* 键盘焦点可见轮廓 */
  outline-offset: 2px;
}

.import-preview {
  padding: 16px 0;
}

.preview-summary {
  display: flex;
  gap: 16px;
  margin-bottom: 16px;
}

.preview-summary .added {
  color: #67c23a;
}

.preview-summary .modified {
  color: #e6a23c;
}

.preview-summary .unchanged {
  color: #909399;
}

.diff-section {
  margin-bottom: 16px;
}

.diff-section h5 {
  margin-bottom: 8px;
  color: #606266;
}

.diff-section ul {
  margin: 0;
  padding-left: 20px;
  font-size: 13px;
}

/* ── 新增：分类基线管理可视化树状及分支树编辑样式 ── */
.global-tree-container {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: #ffffff;
  border-radius: 8px;
  border: 1px solid #e4e7ed;
  overflow: hidden;
}

.global-tree-header {
  padding: 16px 20px;
  background: linear-gradient(180deg, #f8fafc 0%, #f1f5f9 100%);
  border-bottom: 1px solid #e4e7ed;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.panel-title-text {
  font-size: 15px;
  font-weight: 600;
  color: #1e293b;
  margin: 0;
  display: flex;
  align-items: center;
  gap: 8px;
}

.view-tip {
  font-size: 12px;
  color: #64748b;
}

.tree-scroll-wrapper {
  flex: 1;
  overflow-y: auto;
  padding: 20px;
  background: #fafbfe;
}

.tree-scroll-wrapper.is-subtree {
  border: 1px solid #e2e8f0;
  border-radius: 6px;
  max-height: 480px;
  padding: 12px 16px;
  background: #ffffff;
}

.subtree-section {
  margin-top: 24px;
  border-top: 1px dashed #e2e8f0;
  padding-top: 16px;
}

/* 自定义树节点高保真渲染 */
.custom-tree-node {
  flex: 1;
  display: flex;
  align-items: center;
  height: 36px;
  font-size: 13px;
  gap: 10px;
  padding-right: 8px;
  min-width: 0;
}

/* 等高卡片悬浮及选中背景高亮 */
.custom-tree-node.is-selected {
  color: #409eff;
  font-weight: 600;
}

.node-code {
  font-family: 'SFMono-Regular', Consolas, monospace;
  font-size: 11px;
  color: #64748b;
  background: #f1f5f9;
  padding: 1px 5px;
  border-radius: 3px;
  border: 1px solid #e2e8f0;
}

.node-name {
  color: #334155;
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 240px;
}

.is-inactive-name {
  text-decoration: line-through;
  opacity: 0.5;
}

.stats-pills {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 6px;
}

.hit-count-pill {
  font-size: 11px;
  color: #409eff;
  background: #ecf5ff;
  border: 1px solid #d9ecff;
  padding: 1px 6px;
  border-radius: 4px;
  font-weight: 600;
}

/* 层级精美 Badges */
.level-pill {
  font-family: 'SFMono-Regular', Consolas, monospace;
  font-size: 10px;
  font-weight: 700;
  padding: 1px 5px;
  border-radius: 4px;
  line-height: 1.2;
}

.level-l1 { color: hsl(220, 90%, 56%); background: hsl(220, 90%, 95%); border: 1px solid hsl(220, 90%, 90%); }
.level-l2 { color: hsl(170, 80%, 35%); background: hsl(170, 80%, 95%); border: 1px solid hsl(170, 80%, 90%); }
.level-l3 { color: hsl(38, 92%, 40%); background: hsl(38, 92%, 95%); border: 1px solid hsl(38, 92%, 90%); }
.level-l4 { color: hsl(142, 70%, 40%); background: hsl(142, 70%, 95%); border: 1px solid hsl(142, 70%, 90%); }

/* Hover 时淡入操作按钮 */
.node-actions {
  display: none;
  align-items: center;
  gap: 4px;
  padding-left: 10px;
}

.custom-tree-node:hover .node-actions {
  display: inline-flex;
}

.node-actions .el-button {
  padding: 4px;
  height: 24px;
  font-size: 14px;
}

.node-actions .el-button:hover {
  transform: scale(1.1);
  transition: transform 0.15s ease;
}

/* 拖拽指示线与高亮定制 */
:deep(.el-tree-node__content:hover) {
  background-color: #f1f5f9;
  border-radius: 4px;
}

:deep(.el-tree-node.is-current > .el-tree-node__content) {
  background-color: #ecf5ff;
  border-radius: 4px;
  border-left: 3px solid #409eff;
}
</style>