<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Search, Edit, Delete, FullScreen, Document } from '@element-plus/icons-vue'

// ===== 类型定义（遵循 Agent Skills Open Standard）=====
interface SkillDefinition {
  id: number
  skill_name: string         // kebab-case 唯一标识（对应标准 name 字段）
  description: string        // 发现阶段使用，描述"做什么"和"何时触发"（最长 1024 字符）
  instructions_md?: string   // SKILL.md 正文 Markdown（激活阶段加载）
  compatibility?: string     // 环境兼容性说明（可选）
  license?: string           // 许可证（可选）
  allowed_tools?: string     // 预批准工具列表，空格分隔（可选）
  metadata_json?: Record<string, any>  // 扩展元数据（author/category/tags 等）
  display_name?: string      // 中文展示名（平台扩展字段）
  is_active: boolean         // 启用开关（平台扩展字段）
  assets_json?: any[]        // 资源文件内联（平台扩展字段）
  references_json?: any[]    // 参考文档内联（平台扩展字段）
  created_at?: string
  updated_at?: string
}

// ===== 响应式状态 =====
const skills = ref<SkillDefinition[]>([])
const loading = ref(false)
const searchQuery = ref('')
const categoryFilter = ref('')

// 内部 API Token
const internalToken = import.meta.env.VITE_INTERNAL_API_TOKEN || 'hci-dev-internal-token'
const authHeader = { Authorization: `Bearer ${internalToken}` }

// ===== 计算属性 =====

// 筛选后的技能列表
const filteredSkills = computed(() => {
  return skills.value.filter(s => {
    const q = searchQuery.value.toLowerCase()
    const matchQuery = !q ||
      s.skill_name.toLowerCase().includes(q) ||
      (s.display_name || '').toLowerCase().includes(q) ||
      s.description.toLowerCase().includes(q) ||
      (s.metadata_json?.tags || []).join(' ').toLowerCase().includes(q)
    const matchCategory = !categoryFilter.value ||
      (s.metadata_json?.category || '') === categoryFilter.value
    return matchQuery && matchCategory
  })
})

// 所有分类选项（从数据中提取）
const categoryOptions = computed(() => {
  const cats = new Set(skills.value.map(s => s.metadata_json?.category).filter(Boolean))
  return Array.from(cats)
})

// ===== 数据加载 =====
async function fetchSkills() {
  loading.value = true
  try {
    const res = await fetch('/api/v1/skills', { headers: authHeader })
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    skills.value = await res.json()
  } catch (e) {
    console.error('加载技能列表失败:', e)
    ElMessage.error('加载技能列表失败')
  } finally {
    loading.value = false
  }
}

// 加载单个 Skill 完整内容（含 instructions_md）
async function fetchSkillDetail(id: number): Promise<SkillDefinition | null> {
  try {
    const res = await fetch(`/api/v1/skills/${id}`, { headers: authHeader })
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    return await res.json()
  } catch (e) {
    console.error('加载技能详情失败:', e)
    return null
  }
}

// ===== 启用状态切换 =====
async function handleStatusChange(row: SkillDefinition) {
  try {
    const res = await fetch(`/api/v1/skills/${row.id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', ...authHeader },
      body: JSON.stringify({ is_active: row.is_active })
    })
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    ElMessage.success(`技能已${row.is_active ? '启用' : '禁用'}`)
  } catch (e) {
    console.error('更新技能状态失败:', e)
    row.is_active = !row.is_active
    ElMessage.error('更新技能状态失败')
  }
}

// ===== 弹窗表单 =====
const dialogVisible = ref(false)
const isEdit = ref(false)
const isFullscreen = ref(true) // 默认全屏，与 SOP 详情弹窗一致
const activeTab = ref('basic')
const dialogTitle = computed(() => isEdit.value ? '编辑技能定义' : '新建技能定义')

// 表单模型（对应新的数据库字段）
const formModel = ref({
  id: 0,
  skill_name: '',
  display_name: '',
  description: '',
  instructions_md: '',
  compatibility: '',
  license: '',
  allowed_tools: '',
  is_active: true,
  // metadata_json 的常用字段拆开编辑
  meta_author: '',
  meta_category: '',
  meta_tags: '',
  // 高级：自定义 key-value
  custom_meta_key: '',
  custom_meta_value: '',
  // 资源/参考文档（简化为纯文本编辑）
  assets_json: [] as any[],
  references_json: [] as any[],
})

// Markdown 预览（使用 marked + DOMPurify，与项目其他地方保持一致）
const mdPreview = computed(() => {
  try {
    // @ts-ignore
    if (window.marked && window.DOMPurify) {
      // @ts-ignore
      return window.DOMPurify.sanitize(window.marked.parse(formModel.value.instructions_md || ''))
    }
    return formModel.value.instructions_md
      .replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/\n/g, '<br>')
  } catch {
    return formModel.value.instructions_md
  }
})

// description 字符计数
const descCharCount = computed(() => formModel.value.description.length)

// ===== 打开新建弹窗 =====
function openCreateDialog() {
  isEdit.value = false
  isFullscreen.value = true
  activeTab.value = 'basic'
  formModel.value = {
    id: 0,
    skill_name: '',
    display_name: '',
    description: '',
    instructions_md: defaultInstructionsTpl,
    compatibility: '',
    license: '',
    allowed_tools: '',
    is_active: true,
    meta_author: 'hci-team',
    meta_category: 'storage',
    meta_tags: '',
    custom_meta_key: '',
    custom_meta_value: '',
    assets_json: [],
    references_json: [],
  }
  dialogVisible.value = true
}

// ===== 打开编辑弹窗 =====
async function openEditDialog(row: SkillDefinition) {
  isEdit.value = true
  isFullscreen.value = true
  activeTab.value = 'basic'
  dialogVisible.value = true
  loading.value = true

  // 加载完整详情（含 instructions_md）
  const detail = await fetchSkillDetail(row.id)
  loading.value = false

  if (!detail) {
    ElMessage.error('加载技能详情失败')
    dialogVisible.value = false
    return
  }

  const meta = detail.metadata_json || {}
  const tags = Array.isArray(meta.tags) ? meta.tags.join(', ') : (meta.tags || '')

  formModel.value = {
    id: detail.id,
    skill_name: detail.skill_name,
    display_name: detail.display_name || '',
    description: detail.description,
    instructions_md: detail.instructions_md || '',
    compatibility: detail.compatibility || '',
    license: detail.license || '',
    allowed_tools: detail.allowed_tools || '',
    is_active: detail.is_active,
    meta_author: meta.author || '',
    meta_category: meta.category || '',
    meta_tags: tags,
    custom_meta_key: '',
    custom_meta_value: '',
    assets_json: detail.assets_json || [],
    references_json: detail.references_json || [],
  }
}

// ===== 提交表单 =====
async function submitForm() {
  // 校验必填字段
  if (!formModel.value.skill_name.trim()) {
    activeTab.value = 'basic'
    ElMessage.warning('请填写技能标识名称（skill_name）')
    return
  }
  if (!formModel.value.description.trim()) {
    activeTab.value = 'basic'
    ElMessage.warning('请填写技能描述（description），须描述触发条件')
    return
  }
  if (formModel.value.description.length > 1024) {
    activeTab.value = 'basic'
    ElMessage.error('description 最长 1024 字符')
    return
  }

  // 组装 metadata_json
  const tagsArr = formModel.value.meta_tags
    .split(/[,，\s]+/)
    .map(t => t.trim())
    .filter(Boolean)

  const metadataJson: Record<string, any> = {}
  if (formModel.value.meta_author) metadataJson.author = formModel.value.meta_author
  if (formModel.value.meta_category) metadataJson.category = formModel.value.meta_category
  if (tagsArr.length) metadataJson.tags = tagsArr

  const payload: Record<string, any> = {
    description: formModel.value.description.trim(),
    instructions_md: formModel.value.instructions_md,
    compatibility: formModel.value.compatibility.trim() || null,
    license: formModel.value.license.trim() || null,
    allowed_tools: formModel.value.allowed_tools.trim() || null,
    metadata_json: metadataJson,
    display_name: formModel.value.display_name.trim() || null,
    is_active: formModel.value.is_active,
    assets_json: formModel.value.assets_json,
    references_json: formModel.value.references_json,
  }

  // 新建时需要 skill_name
  if (!isEdit.value) {
    payload.skill_name = formModel.value.skill_name.trim()
  }

  try {
    const url = isEdit.value ? `/api/v1/skills/${formModel.value.id}` : '/api/v1/skills'
    const method = isEdit.value ? 'PUT' : 'POST'

    const res = await fetch(url, {
      method,
      headers: { 'Content-Type': 'application/json', ...authHeader },
      body: JSON.stringify(payload)
    })

    if (!res.ok) {
      const err = await res.json()
      throw new Error(err.detail || `HTTP ${res.status}`)
    }

    ElMessage.success(`${dialogTitle.value}成功`)
    dialogVisible.value = false
    fetchSkills()
  } catch (e: any) {
    console.error('提交技能定义失败:', e)
    ElMessage.error(e.message || '操作失败')
  }
}

// ===== 删除 =====
async function handleDelete(row: SkillDefinition) {
  ElMessageBox.confirm(
    `确认删除技能 "${row.display_name || row.skill_name}" 吗？此操作无法撤销。`,
    '高危警示',
    {
      confirmButtonText: '极其确认',
      cancelButtonText: '取消',
      type: 'warning',
      confirmButtonClass: 'el-button--danger'
    }
  ).then(async () => {
    try {
      const res = await fetch(`/api/v1/skills/${row.id}`, {
        method: 'DELETE',
        headers: authHeader
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      ElMessage.success('技能已成功删除')
      fetchSkills()
    } catch (e) {
      console.error('删除技能失败:', e)
      ElMessage.error('删除失败')
    }
  }).catch(() => {})
}

// ===== 资源文件管理 =====
function addReference() {
  formModel.value.references_json.push({ filename: '', title: '', content: '' })
}
function removeReference(idx: number) {
  formModel.value.references_json.splice(idx, 1)
}
function addAsset() {
  formModel.value.assets_json.push({ filename: '', type: 'template', content: '' })
}
function removeAsset(idx: number) {
  formModel.value.assets_json.splice(idx, 1)
}

// ===== 新建技能的默认 instructions_md 模板 =====
const defaultInstructionsTpl = `## 技能描述

说明该 Skill 的用途和适用场景。

---

### 前置条件

列出执行本 Skill 前需要准备的信息或条件。

---

### Step 1：第一步操作

详细说明操作步骤。

### Step 2：第二步操作

...

---

### Gotchas（关键陷阱清单）

- 列出容易犯错的地方，例如：字段名称区别、单位混淆、特殊厂商例外等
- 每条 Gotcha 都应该是具体的、可操作的提示，而非笼统建议

---

### 输出格式

\`\`\`
判定结论：✅ / ⚠️ ...
具体数据：...
建议操作：...
\`\`\`
`

onMounted(() => {
  fetchSkills()
})
</script>

<template>
  <div class="skill-manage-container">
    <!-- 页面头部 -->
    <div class="page-header">
      <div class="header-row">
        <div>
          <h2 class="page-title">技能注册表</h2>
          <p class="page-desc">
            基于 <a href="https://agentskills.io" target="_blank" class="standard-link">Agent Skills Open Standard</a>
            的领域专业知识包管理 — 每个 Skill 是"过程性知识 + 诊断流程"，而非函数接口
          </p>
        </div>
        <el-button type="primary" :icon="Plus" @click="openCreateDialog">新建技能</el-button>
      </div>
    </div>

    <!-- 过滤栏 -->
    <el-card class="filter-card" shadow="never">
      <el-row :gutter="16" align="middle">
        <el-col :span="10">
          <el-input
            v-model="searchQuery"
            placeholder="搜索技能标识、展示名称、描述、标签..."
            clearable
          >
            <template #prefix>
              <el-icon><Search /></el-icon>
            </template>
          </el-input>
        </el-col>
        <el-col :span="5">
          <el-select v-model="categoryFilter" placeholder="按分类筛选" clearable style="width: 100%">
            <el-option
              v-for="cat in categoryOptions"
              :key="cat"
              :label="cat"
              :value="cat"
            />
          </el-select>
        </el-col>
        <el-col :span="9" style="text-align: right; color: #909399; font-size: 14px;">
          共 <strong>{{ filteredSkills.length }}</strong> 个技能
        </el-col>
      </el-row>
    </el-card>

    <!-- 数据表 -->
    <el-card shadow="never" class="table-card">
      <el-table
        v-loading="loading"
        :data="filteredSkills"
        stripe
        style="width: 100%"
        class="custom-table"
        row-key="id"
      >
        <!-- Skill 标识 + 展示名 -->
        <el-table-column label="Skill 标识" min-width="360">
          <template #default="{ row }">
            <div class="skill-name-cell">
              <code class="code-badge">{{ row.skill_name }}</code>
              <span v-if="row.display_name" class="display-name-sub">{{ row.display_name }}</span>
            </div>
          </template>
        </el-table-column>

        <!-- 描述（发现阶段内容） -->
        <el-table-column label="描述（触发条件）" min-width="280" prop="description" show-overflow-tooltip />

        <!-- 分类标签 -->
        <el-table-column label="分类" width="110" align="center">
          <template #default="{ row }">
            <el-tag v-if="row.metadata_json?.category" size="small" type="info">
              {{ row.metadata_json.category }}
            </el-tag>
            <span v-else class="text-secondary">—</span>
          </template>
        </el-table-column>

        <!-- 兼容性 -->
        <el-table-column label="兼容性" width="130" show-overflow-tooltip>
          <template #default="{ row }">
            <span class="text-secondary text-sm">{{ row.compatibility || '—' }}</span>
          </template>
        </el-table-column>

        <!-- 指令正文（有无） -->
        <el-table-column label="指令" width="70" align="center">
          <template #default="{ row }">
            <el-tooltip :content="row.instructions_md ? '已填写 SKILL.md 正文' : '尚未填写指令正文'" placement="top">
              <el-icon :class="row.instructions_md ? 'icon-has-content' : 'icon-empty'">
                <Document />
              </el-icon>
            </el-tooltip>
          </template>
        </el-table-column>

        <!-- 启用状态 -->
        <el-table-column label="状态" width="90" align="center">
          <template #default="{ row }">
            <el-switch
              v-model="row.is_active"
              active-color="#13ce66"
              inactive-color="#dcdfe6"
              @change="handleStatusChange(row)"
            />
          </template>
        </el-table-column>

        <!-- 操作列（固定宽度，确保两个按钮始终同行） -->
        <el-table-column label="操作" width="180" fixed="right" align="center">
          <template #default="{ row }">
            <div class="actions-cell">
              <el-button type="primary" size="small" text :icon="Edit" @click="openEditDialog(row)">编辑</el-button>
              <el-button type="danger" size="small" text :icon="Delete" @click="handleDelete(row)">删除</el-button>
            </div>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- ===== 新建/编辑技能弹窗（全屏，Tab 布局）===== -->
    <el-dialog
      v-model="dialogVisible"
      :fullscreen="isFullscreen"
      class="skill-detail-dialog"
      draggable
      align-center
      destroy-on-close
    >
      <template #header>
        <div class="custom-dialog-header">
          <span class="el-dialog__title">{{ dialogTitle }}</span>
          <el-button
            type="info"
            text
            circle
            :icon="FullScreen"
            class="fullscreen-toggle-btn"
            @click="isFullscreen = !isFullscreen"
            title="切换全屏"
          />
        </div>
      </template>

      <el-tabs v-model="activeTab" class="skill-tabs">

        <!-- Tab 1：基本信息 -->
        <el-tab-pane label="基本信息" name="basic">
          <el-form :model="formModel" label-position="top" class="dialog-form">
            <el-row :gutter="24">
              <el-col :span="12">
                <el-form-item required>
                  <template #label>
                    <span>技能标识 <code class="field-label-code">skill_name</code></span>
                    <span class="field-hint">kebab-case，创建后不可修改</span>
                  </template>
                  <el-input
                    v-model="formModel.skill_name"
                    placeholder="例如：hci-disk-vendor-lifetime"
                    :disabled="isEdit"
                  />
                </el-form-item>
              </el-col>
              <el-col :span="12">
                <el-form-item label="中文展示名">
                  <el-input v-model="formModel.display_name" placeholder="例如：硬盘厂商识别与寿命判定" />
                </el-form-item>
              </el-col>
            </el-row>

            <el-form-item required>
              <template #label>
                <span>描述 <code class="field-label-code">description</code></span>
                <span class="field-hint">告诉 Agent "做什么"和"何时触发"，最长 1024 字符（当前 {{ descCharCount }}/1024）</span>
              </template>
              <el-input
                v-model="formModel.description"
                type="textarea"
                :rows="4"
                placeholder="例：识别磁盘厂商并判断寿命是否达到返修阈值。当用户报告磁盘 IO 异常、存储池降级、坏道告警时触发。"
                maxlength="1024"
              />
            </el-form-item>

            <el-row :gutter="24">
              <el-col :span="16">
                <el-form-item>
                  <template #label>
                    <span>兼容性 <code class="field-label-code">compatibility</code></span>
                    <span class="field-hint">环境依赖说明（可选，最长 500 字符）</span>
                  </template>
                  <el-input v-model="formModel.compatibility" placeholder="例：适用于 HCI v2.x 环境，需要能执行 smartctl 命令" maxlength="500" />
                </el-form-item>
              </el-col>
              <el-col :span="8">
                <el-form-item label="启用状态">
                  <el-switch v-model="formModel.is_active" active-text="启用" inactive-text="下线" />
                </el-form-item>
              </el-col>
            </el-row>

            <el-row :gutter="24">
              <el-col :span="12">
                <el-form-item>
                  <template #label>
                    <span>许可证 <code class="field-label-code">license</code></span>
                  </template>
                  <el-input v-model="formModel.license" placeholder="例：Proprietary" />
                </el-form-item>
              </el-col>
              <el-col :span="12">
                <el-form-item>
                  <template #label>
                    <span>预批准工具 <code class="field-label-code">allowed-tools</code></span>
                    <span class="field-hint">空格分隔（实验性）</span>
                  </template>
                  <el-input v-model="formModel.allowed_tools" placeholder="例：Bash(ssh:*) ReadFile" />
                </el-form-item>
              </el-col>
            </el-row>

            <!-- 元数据 metadata -->
            <el-form-item>
              <template #label>
                <span>扩展元数据 <code class="field-label-code">metadata</code></span>
              </template>
              <el-row :gutter="12">
                <el-col :span="8">
                  <el-input v-model="formModel.meta_author" placeholder="author（作者）" />
                </el-col>
                <el-col :span="8">
                  <el-select v-model="formModel.meta_category" placeholder="category（分类）" allow-create filterable style="width: 100%">
                    <el-option label="storage（存储）" value="storage" />
                    <el-option label="network（网络）" value="network" />
                    <el-option label="compute（计算）" value="compute" />
                    <el-option label="monitoring（监控）" value="monitoring" />
                    <el-option label="security（安全）" value="security" />
                  </el-select>
                </el-col>
                <el-col :span="8">
                  <el-input v-model="formModel.meta_tags" placeholder="tags（逗号分隔，如 disk,smart）" />
                </el-col>
              </el-row>
            </el-form-item>
          </el-form>
        </el-tab-pane>

        <!-- Tab 2：技能指令（SKILL.md 正文）-->
        <el-tab-pane label="技能指令（SKILL.md 正文）" name="instructions">
          <!-- 最佳实践提示 -->
          <el-collapse class="best-practice-collapse">
            <el-collapse-item name="tips">
              <template #title>
                <span class="collapse-title">💡 Agent Skills 最佳实践提示（点击展开）</span>
              </template>
              <div class="best-practice-content">
                <el-row :gutter="20">
                  <el-col :span="12">
                    <h4>推荐包含的内容</h4>
                    <ul>
                      <li><strong>Step-by-step 操作指引</strong>：逐步说明 Agent 的执行流程</li>
                      <li><strong>Gotchas 陷阱清单</strong>：环境特定的非显而易见注意事项（最高价值内容）</li>
                      <li><strong>输入/输出示例</strong>：帮助 Agent 正确处理边界情况</li>
                      <li><strong>输出格式模板</strong>：确保输出一致性</li>
                      <li><strong>多步骤检查清单</strong>：防止 Agent 跳过关键步骤</li>
                    </ul>
                  </el-col>
                  <el-col :span="12">
                    <h4>写作原则</h4>
                    <ul>
                      <li>写 Agent 不懂的内容，不要解释 Agent 已知的常识</li>
                      <li>提供默认选择，而非列出多个等价方案</li>
                      <li>说明"为什么"，帮助 Agent 做上下文相关的决策</li>
                      <li>建议正文不超过 <strong>500 行 / 5000 tokens</strong></li>
                    </ul>
                  </el-col>
                </el-row>
              </div>
            </el-collapse-item>
          </el-collapse>

          <!-- 编辑器 + 预览 -->
          <div class="md-editor-layout">
            <div class="md-editor-pane">
              <div class="pane-header">
                <span class="pane-title">Markdown 编辑</span>
                <span class="pane-hint">支持标准 Markdown 语法</span>
              </div>
              <el-input
                v-model="formModel.instructions_md"
                type="textarea"
                class="md-editor-textarea"
                :rows="30"
                placeholder="在此编写 SKILL.md 正文内容..."
                resize="none"
              />
            </div>
            <div class="md-preview-pane">
              <div class="pane-header">
                <span class="pane-title">预览</span>
              </div>
              <div
                class="md-preview-content"
                v-html="mdPreview"
              />
            </div>
          </div>
        </el-tab-pane>

        <!-- Tab 3：资源文件 -->
        <el-tab-pane label="资源文件（References & Assets）" name="resources">
          <!-- 参考文档 -->
          <div class="resource-section">
            <div class="resource-section-header">
              <h4>参考文档 <code class="field-label-code">references</code></h4>
              <el-button size="small" type="primary" text :icon="Plus" @click="addReference">添加参考文档</el-button>
            </div>
            <p class="resource-section-desc">
              对应标准 <code>references/</code> 目录，存放技术参考手册、Schema 文件等，Agent 在需要时按需加载。
            </p>
            <div v-if="formModel.references_json.length === 0" class="resource-empty">
              暂无参考文档，点击上方按钮添加
            </div>
            <div v-for="(ref, idx) in formModel.references_json" :key="idx" class="resource-item">
              <el-row :gutter="12" align="middle">
                <el-col :span="6">
                  <el-input v-model="ref.filename" placeholder="文件名，如 REFERENCE.md" size="small" />
                </el-col>
                <el-col :span="8">
                  <el-input v-model="ref.title" placeholder="标题（简要说明用途）" size="small" />
                </el-col>
                <el-col :span="8">
                  <el-input v-model="ref.content" type="textarea" :rows="2" placeholder="文件内容（Markdown / YAML / 纯文本）" size="small" />
                </el-col>
                <el-col :span="2">
                  <el-button type="danger" size="small" text :icon="Delete" @click="removeReference(idx)" />
                </el-col>
              </el-row>
            </div>
          </div>

          <el-divider />

          <!-- 资源文件 -->
          <div class="resource-section">
            <div class="resource-section-header">
              <h4>资源文件 <code class="field-label-code">assets</code></h4>
              <el-button size="small" type="primary" text :icon="Plus" @click="addAsset">添加资源文件</el-button>
            </div>
            <p class="resource-section-desc">
              对应标准 <code>assets/</code> 目录，存放输出模板、数据文件等静态资源。
            </p>
            <div v-if="formModel.assets_json.length === 0" class="resource-empty">
              暂无资源文件，点击上方按钮添加
            </div>
            <div v-for="(asset, idx) in formModel.assets_json" :key="idx" class="resource-item">
              <el-row :gutter="12" align="middle">
                <el-col :span="6">
                  <el-input v-model="asset.filename" placeholder="文件名，如 report-template.md" size="small" />
                </el-col>
                <el-col :span="4">
                  <el-select v-model="asset.type" placeholder="类型" size="small" style="width: 100%">
                    <el-option label="template" value="template" />
                    <el-option label="schema" value="schema" />
                    <el-option label="data" value="data" />
                    <el-option label="other" value="other" />
                  </el-select>
                </el-col>
                <el-col :span="12">
                  <el-input v-model="asset.content" type="textarea" :rows="2" placeholder="文件内容" size="small" />
                </el-col>
                <el-col :span="2">
                  <el-button type="danger" size="small" text :icon="Delete" @click="removeAsset(idx)" />
                </el-col>
              </el-row>
            </div>
          </div>
        </el-tab-pane>

      </el-tabs>

      <template #footer>
        <div class="dialog-footer">
          <el-button @click="dialogVisible = false">取消</el-button>
          <el-button type="primary" @click="submitForm">保存技能</el-button>
        </div>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.skill-manage-container {
  padding: 20px;
}

.page-header {
  margin-bottom: 20px;
}

.header-row {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
}

.page-title {
  margin: 0 0 6px;
  font-size: 22px;
  color: #303133;
}

.page-desc {
  margin: 0;
  color: #666;
  font-size: 13px;
}

.standard-link {
  color: #409eff;
  text-decoration: none;
}

.standard-link:hover {
  text-decoration: underline;
}

.filter-card {
  margin-bottom: 16px;
}

.table-card {
  min-height: 400px;
}

/* Skill 标识单元格 */
.skill-name-cell {
  display: flex;
  flex-direction: row;
  align-items: center;
  gap: 8px;
}

.display-name-sub {
  font-size: 12px;
  color: #606266;
}

.actions-cell {
  display: flex;
  justify-content: center;
  align-items: center;
  gap: 8px;
}

/* code badge */
.code-badge {
  background: #f8f9fa;
  border: 1px solid #e9ecef;
  color: #e83e8c;
  padding: 2px 7px;
  border-radius: 4px;
  font-family: Consolas, Monaco, 'Andale Mono', monospace;
  font-size: 12px;
  white-space: nowrap;
}

.text-secondary {
  color: #95a5a6;
  font-size: 13px;
}

.text-sm {
  font-size: 12px;
}

/* 指令文档图标 */
.icon-has-content {
  color: #67c23a;
  font-size: 18px;
}

.icon-empty {
  color: #c0c4cc;
  font-size: 18px;
}

/* 弹窗 */
:global(.skill-detail-dialog) {
  display: flex;
  flex-direction: column;
}

:global(.skill-detail-dialog .el-dialog) {
  display: flex;
  flex-direction: column;
  max-height: 90vh;
  border-radius: 8px;
  overflow: hidden;
  box-shadow: 0 12px 32px rgba(0, 0, 0, 0.1) !important;
}

:global(.skill-detail-dialog.is-fullscreen .el-dialog) {
  max-height: 100vh;
  height: 100vh;
  border-radius: 0;
}

:global(.skill-detail-dialog .el-dialog__header) {
  background-color: #f8f9fa;
  margin-right: 0;
  padding: 16px 20px;
  border-bottom: 1px solid #eee;
  flex-shrink: 0;
}

:global(.skill-detail-dialog .el-dialog__body) {
  padding: 0;
  flex: 1;
  overflow: hidden;
}

:global(.skill-detail-dialog .el-dialog__footer) {
  padding: 12px 24px;
  border-top: 1px solid #eee;
  background-color: #f8f9fa;
  flex-shrink: 0;
}

.custom-dialog-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding-right: 32px;
}

.fullscreen-toggle-btn {
  font-size: 16px;
  color: #606266;
}

/* Tab */
.skill-tabs {
  height: 100%;
}

.skill-tabs :deep(.el-tabs__header) {
  margin: 0;
  padding: 0 20px;
  background: #fff;
  border-bottom: 1px solid #eee;
}

.skill-tabs :deep(.el-tabs__content) {
  height: calc(100% - 41px);
  overflow-y: auto;
  padding: 20px 24px;
}

/* 表单 */
.dialog-form {
  width: 100%;
  max-width: 900px;
}

.field-label-code {
  background: #f0f0f0;
  border: none;
  color: #e83e8c;
  padding: 1px 5px;
  border-radius: 3px;
  font-size: 11px;
  font-family: Consolas, Monaco, monospace;
  margin-left: 4px;
}

.field-hint {
  font-size: 11px;
  color: #909399;
  margin-left: 8px;
  font-weight: normal;
}

/* 最佳实践折叠面板 */
.best-practice-collapse {
  margin-bottom: 16px;
  border-radius: 6px;
  border: 1px solid #e4f2fb;
}

.best-practice-collapse :deep(.el-collapse-item__header) {
  background: #f0f8ff;
  padding: 0 16px;
  border-radius: 6px 6px 0 0;
}

.collapse-title {
  font-size: 13px;
  color: #409eff;
}

.best-practice-content {
  padding: 12px 16px;
  font-size: 13px;
  line-height: 1.7;
}

.best-practice-content h4 {
  margin: 0 0 8px;
  color: #303133;
  font-size: 13px;
}

.best-practice-content ul {
  margin: 0;
  padding-left: 20px;
  color: #606266;
}

/* Markdown 编辑器布局 */
.md-editor-layout {
  display: flex;
  gap: 16px;
  height: calc(100vh - 300px);
  min-height: 400px;
}

.md-editor-pane,
.md-preview-pane {
  flex: 1;
  display: flex;
  flex-direction: column;
  border: 1px solid #e4e7ed;
  border-radius: 6px;
  overflow: hidden;
}

.pane-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px;
  background: #f8f9fa;
  border-bottom: 1px solid #e4e7ed;
}

.pane-title {
  font-size: 13px;
  font-weight: 600;
  color: #606266;
}

.pane-hint {
  font-size: 11px;
  color: #909399;
}

.md-editor-textarea {
  flex: 1;
  height: 100%;
}

.md-editor-textarea :deep(.el-textarea__inner) {
  height: 100%;
  font-family: Consolas, Monaco, 'Andale Mono', monospace;
  font-size: 13px;
  line-height: 1.6;
  background-color: #fafbfc;
  color: #24292e;
  border: none;
  border-radius: 0;
  resize: none;
}

.md-preview-content {
  flex: 1;
  padding: 16px;
  overflow-y: auto;
  font-size: 14px;
  line-height: 1.7;
  color: #303133;
}

.md-preview-content :deep(h2),
.md-preview-content :deep(h3),
.md-preview-content :deep(h4) {
  margin-top: 16px;
  margin-bottom: 8px;
}

.md-preview-content :deep(code) {
  background: #f8f9fa;
  padding: 2px 6px;
  border-radius: 3px;
  font-family: Consolas, Monaco, monospace;
  font-size: 12px;
}

.md-preview-content :deep(pre) {
  background: #f8f9fa;
  padding: 12px;
  border-radius: 6px;
  overflow-x: auto;
}

.md-preview-content :deep(table) {
  border-collapse: collapse;
  width: 100%;
  font-size: 13px;
}

.md-preview-content :deep(th),
.md-preview-content :deep(td) {
  border: 1px solid #e4e7ed;
  padding: 6px 12px;
}

.md-preview-content :deep(th) {
  background: #f8f9fa;
}

/* 资源文件区域 */
.resource-section {
  margin-bottom: 20px;
}

.resource-section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 6px;
}

.resource-section-header h4 {
  margin: 0;
  font-size: 14px;
  color: #303133;
}

.resource-section-desc {
  font-size: 12px;
  color: #909399;
  margin: 0 0 12px;
}

.resource-empty {
  text-align: center;
  color: #909399;
  font-size: 13px;
  padding: 20px;
  background: #fafafa;
  border-radius: 6px;
  border: 1px dashed #e4e7ed;
}

.resource-item {
  background: #fafafa;
  border: 1px solid #e4e7ed;
  border-radius: 6px;
  padding: 10px 12px;
  margin-bottom: 8px;
}

/* 弹窗底部 */
.dialog-footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
</style>
