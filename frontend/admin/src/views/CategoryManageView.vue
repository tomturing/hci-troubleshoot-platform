<script setup lang="ts">
import { ref, reactive, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { Histogram, Upload, Download } from '@element-plus/icons-vue'
import type { UploadFile, UploadRawFile, UploadInstance } from 'element-plus'

// ──────────────────────────────────────────────────────────────────────────────
// 类型定义（适配 kb_category 表结构）
// ──────────────────────────────────────────────────────────────────────────────
interface KbCategory {
  id: number
  code: string                  // 业务键，如 "虚拟机-003"
  name: string                  // 分类名称
  domain: string                // 一级技术域
  level: number                 // 层级 1-4
  parent_id: number | null
  path_labels: string[]         // 完整路径
  hit_count: number             // S0 命中次数
  is_active: boolean            // 启用状态
  has_sop: boolean              // 是否有 SOP 覆盖（计算字段）
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

// ──────────────────────────────────────────────────────────────────────────────
// 响应式状态：右侧详情
// ──────────────────────────────────────────────────────────────────────────────
const selectedCategory = ref<KbCategory | null>(null)
const editSaving = ref(false)
const editForm = reactive({
  name: '',
  is_active: true,
})

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
// 数据加载
// ──────────────────────────────────────────────────────────────────────────────
async function fetchCategories() {
  loading.value = true
  try {
    const resp = await fetch('/api/kb/categories?grouped=true', {
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
    totalWithSop.value = allCategories.filter((c) => c.has_sop).length
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
  editForm.name = cat.name
  editForm.is_active = cat.is_active
}

// ──────────────────────────────────────────────────────────────────────────────
// 保存编辑
// ──────────────────────────────────────────────────────────────────────────────
async function saveEdit() {
  if (!selectedCategory.value) return
  editSaving.value = true
  try {
    const body: Record<string, unknown> = {
      name: editForm.name,
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
          name: editForm.name,
          is_active: editForm.is_active,
        }
        selectedCategory.value = g.categories[idx]
        break
      }
    }
    // 重新统计
    const allCategories = domainGroups.value.flatMap((g) => g.categories)
    totalActive.value = allCategories.filter((c) => c.is_active).length
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
              <span v-if="cat.has_sop" class="sop-badge">SOP</span>
              <span v-if="!cat.is_active" class="inactive-badge">禁用</span>
            </div>
          </div>
        </div>

        <!-- 统计 -->
        <div class="stats-bar">
          <span>总计: {{ totalCategories }}</span>
          <span>启用: {{ totalActive }}</span>
          <span>有SOP: {{ totalWithSop }}</span>
        </div>
      </div>

      <!-- 右侧：详情编辑 -->
      <div class="right-panel">
        <div v-if="!selectedCategory" class="empty-state">
          请从左侧选择一个分类查看详情
        </div>
        <div v-else class="detail-form">
          <h3>分类详情</h3>

          <div class="form-item">
            <label>业务编码</label>
            <el-input :model-value="selectedCategory.code" disabled />
          </div>

          <div class="form-item">
            <label>分类名称</label>
            <el-input v-model="editForm.name" />
          </div>

          <div class="form-item">
            <label>所属域</label>
            <el-input :model-value="selectedCategory.domain" disabled />
          </div>

          <div class="form-item">
            <label>完整路径</label>
            <el-input :model-value="selectedCategory.path_labels?.join(' / ') || ''" disabled />
          </div>

          <div class="form-item">
            <label>状态</label>
            <el-radio-group v-model="editForm.is_active">
              <el-radio :value="true">启用</el-radio>
              <el-radio :value="false">禁用</el-radio>
            </el-radio-group>
          </div>

          <div class="form-item">
            <label>SOP 覆盖</label>
            <el-tag :type="selectedCategory.has_sop ? 'success' : 'info'">
              {{ selectedCategory.has_sop ? '已绑定 SOP' : '无 SOP' }}
            </el-tag>
          </div>

          <div class="form-item">
            <label>命中次数</label>
            <span class="hit-count">{{ selectedCategory.hit_count }} 次</span>
          </div>

          <el-button type="primary" :loading="editSaving" @click="saveEdit">
            保存修改
          </el-button>
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
  width: 320px;
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

.detail-form h3 {
  margin-bottom: 20px;
  padding-bottom: 12px;
  border-bottom: 1px solid #e4e7ed;
}

.form-item {
  margin-bottom: 16px;
}

.form-item label {
  display: block;
  margin-bottom: 8px;
  font-size: 14px;
  color: #606266;
}

.hit-count {
  font-size: 16px;
  font-weight: 600;
  color: #409eff;
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
</style>