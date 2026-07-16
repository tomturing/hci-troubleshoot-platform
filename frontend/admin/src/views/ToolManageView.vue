<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Search, Edit, Delete, FullScreen } from '@element-plus/icons-vue'
import { ProducesEditor, MatcherEditor } from '@/components/editors'

interface ToolDefinition {
  id: number
  tool_name: string
  display_name: string
  category: string
  description: string
  usage_template: string | null
  parameters_schema: Record<string, any>
  examples: any[]
  risk_level: number
  is_active: boolean
  version: string
  created_at?: string
  updated_at?: string
}

interface ToolPayload {
  tool_name: string
  display_name: string
  category: string
  description: string
  usage_template: string | null
  parameters_schema: Record<string, any>
  examples: any[]
  risk_level: number
  is_active: boolean
  version: string
}

interface ToolValidationIssue {
  level: string
  location: string
  message: string
  code?: string
}

interface ToolValidationResult {
  status: 'ok' | 'warning' | 'error'
  validation_issues: ToolValidationIssue[]
}

const tools = ref<ToolDefinition[]>([])
const loading = ref(false)
const searchQuery = ref('')
const categoryFilter = ref('')

// 获取 internalToken
const internalToken = import.meta.env.VITE_INTERNAL_API_TOKEN || 'hci-dev-internal-token'
const authHeader = { Authorization: `Bearer ${internalToken}` }

// 过滤后的工具列表
const filteredTools = computed(() => {
  return tools.value.filter(t => {
    const matchesSearch = 
      t.tool_name.toLowerCase().includes(searchQuery.value.toLowerCase()) ||
      t.display_name.toLowerCase().includes(searchQuery.value.toLowerCase()) ||
      t.description.toLowerCase().includes(searchQuery.value.toLowerCase())
    
    const matchesCategory = !categoryFilter.value || t.category === categoryFilter.value
    return matchesSearch && matchesCategory
  })
})

// 加载工具列表
async function fetchTools() {
  loading.value = true
  try {
    const res = await fetch('/api/v1/tools', { headers: authHeader })
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    tools.value = await res.json()
  } catch (e) {
    console.error('加载工具列表失败:', e)
    ElMessage.error('加载工具列表失败')
  } finally {
    loading.value = false
  }
}

// 快速切换启用状态
async function handleStatusChange(row: ToolDefinition) {
  try {
    const res = await fetch(`/api/v1/tools/${row.id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', ...authHeader },
      body: JSON.stringify({
        tool_name: row.tool_name,
        display_name: row.display_name,
        category: row.category,
        description: row.description,
        usage_template: row.usage_template,
        parameters_schema: row.parameters_schema,
        examples: row.examples,
        risk_level: row.risk_level,
        is_active: row.is_active,
        version: row.version
      })
    })
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    ElMessage.success(`工具已${row.is_active ? '启用' : '禁用'}`)
  } catch (e) {
    console.error('更新工具状态失败:', e)
    row.is_active = !row.is_active // 恢复开关状态
    ElMessage.error('更新工具状态失败')
  }
}

// 弹窗表单状态
const dialogVisible = ref(false)
const isEdit = ref(false)
const isFullscreen = ref(false)
const submitting = ref(false)
const validationLoading = ref(false)
const validationResult = ref<ToolValidationResult | null>(null)
const lastValidatedSignature = ref('')
const dialogTitle = computed(() => isEdit.value ? '编辑工具定义' : '新建工具定义')

const formModel = ref({
  id: 0,
  tool_name: '',
  display_name: '',
  category: 'acli',
  description: '',
  usage_template: '',
  parameters_schema_str: '{}',
  examples_str: '[]',
  risk_level: 1,
  is_active: true,
  version: '1.0'
})

// ─── QKV/QFK 可视化编辑状态 ───
const activeParamTab = ref<'json' | 'form'>('json')
const producesData = ref<Array<{ name: string; path: string }>>([])
const matcherData = ref<Record<string, any>>({ type: 'keyword', expected: true })

// 判断是否为 QKV/QFK 工具（需要可视化编辑器）
const isSignalTool = computed(() => ['qkv', 'qfk'].includes(formModel.value.category))

// 从 JSON Schema 解析 produces 字段（QKV 专用）
function parseProducesFromSchema(schemaStr: string): Array<{ name: string; path: string }> {
  try {
    const schema = JSON.parse(schemaStr)
    const produces = schema?.properties?.produces?.default || schema?.produces || []
    if (Array.isArray(produces)) {
      return produces.map((p: any) => ({
        name: p?.name || '',
        path: p?.path || '',
      }))
    }
  } catch {}
  return []
}

// 从 JSON Schema 解析 matcher 字段（QFK 专用）
function parseMatcherFromSchema(schemaStr: string): Record<string, any> {
  try {
    const schema = JSON.parse(schemaStr)
    // matcher 在 examples 的第一条记录中（模板）
    const examples = schema?.examples
    if (Array.isArray(examples) && examples[0]?.matcher) {
      return examples[0].matcher
    }
    // 或从 properties.matcher.default 中取
    if (schema?.properties?.matcher?.default) {
      return schema.properties.matcher.default
    }
  } catch {}
  return { type: 'keyword', expected: true }
}

// 当 parameters_schema_str 变化时，解析到表单（JSON → Form）
watch(
  () => formModel.value.parameters_schema_str,
  (newVal) => {
    if (isSignalTool.value && activeParamTab.value === 'form') {
      producesData.value = parseProducesFromSchema(newVal)
      matcherData.value = parseMatcherFromSchema(newVal)
    }
  },
  { immediate: false }
)

// 当 category 变化时，自动切换 Tab
watch(
  () => formModel.value.category,
  (newCat) => {
    if (['qkv', 'qfk'].includes(newCat)) {
      // QKV/QFK 工具打开时，尝试解析表单数据
      producesData.value = parseProducesFromSchema(formModel.value.parameters_schema_str)
      matcherData.value = parseMatcherFromSchema(formModel.value.parameters_schema_str)
    }
  }
)

// 当表单数据变化时，同步回 JSON Schema（Form → JSON）
function syncFormToJson() {
  if (!isSignalTool.value) return
  
  try {
    const schema = JSON.parse(formModel.value.parameters_schema_str || '{}')
    
    // QKV: 更新 produces.default
    if (formModel.value.category === 'qkv' && producesData.value.length > 0) {
      if (!schema.properties) schema.properties = {}
      if (!schema.properties.produces) {
        schema.properties.produces = {
          type: 'array',
          items: {
            type: 'object',
            properties: {
              name: { type: 'string', description: '输出变量名' },
              path: { type: 'string', description: 'JSON 字段路径' }
            },
            required: ['name', 'path']
          },
          default: []
        }
      }
      schema.properties.produces.default = producesData.value
    }
    
    // QFK: 更新 matcher.default
    if (formModel.value.category === 'qfk' && matcherData.value.type) {
      if (!schema.properties) schema.properties = {}
      if (!schema.properties.matcher) {
        schema.properties.matcher = {
          type: 'object',
          properties: {
            type: { type: 'string', enum: ['keyword', 'regex', 'state', 'threshold', 'json_path', 'exists'] },
            expected: { type: 'boolean', default: true }
          },
          required: ['type']
        }
      }
      schema.properties.matcher.default = matcherData.value
    }
    
    formModel.value.parameters_schema_str = JSON.stringify(schema, null, 2)
  } catch (e) {
    console.warn('同步表单到 JSON 失败:', e)
  }
}

// 监听表单数据变化，同步到 JSON
watch([producesData, matcherData], syncFormToJson, { deep: true })

const validationStatusText = computed(() => {
  if (!validationResult.value) return ''
  if (validationResult.value.status === 'ok') return '校验通过'
  if (validationResult.value.status === 'warning') return '存在警告'
  return '校验失败'
})

const isValidationStale = computed(() => {
  return Boolean(validationResult.value && lastValidatedSignature.value !== getValidationSignature())
})

function resetValidationResult() {
  validationResult.value = null
  lastValidatedSignature.value = ''
}

// 打开新建弹窗
function openCreateDialog() {
  isEdit.value = false
  isFullscreen.value = false
  resetValidationResult()
  formModel.value = {
    id: 0,
    tool_name: '',
    display_name: '',
    category: 'acli',
    description: '',
    usage_template: '',
    parameters_schema_str: '{\n  "type": "object",\n  "properties": {},\n  "required": []\n}',
    examples_str: '[]',
    risk_level: 1,
    is_active: true,
    version: '1.0'
  }
  dialogVisible.value = true
}

// 打开编辑弹窗
function openEditDialog(row: ToolDefinition) {
  isEdit.value = true
  isFullscreen.value = false
  resetValidationResult()
  formModel.value = {
    id: row.id,
    tool_name: row.tool_name,
    display_name: row.display_name,
    category: row.category,
    description: row.description,
    usage_template: row.usage_template || '',
    parameters_schema_str: JSON.stringify(row.parameters_schema, null, 2),
    examples_str: JSON.stringify(row.examples, null, 2),
    risk_level: row.risk_level,
    is_active: row.is_active,
    version: row.version
  }
  dialogVisible.value = true
}

// 校验 JSON 格式
function isValidJson(str: string) {
  try {
    JSON.parse(str)
    return true
  } catch (e) {
    return false
  }
}

function getValidationSignature() {
  return JSON.stringify({
    tool_name: formModel.value.tool_name.trim(),
    usage_template: formModel.value.usage_template.trim(),
    parameters_schema_str: formModel.value.parameters_schema_str,
  })
}

function normalizeValidationResult(raw: any): ToolValidationResult {
  const rawIssues = Array.isArray(raw?.validation_issues) ? raw.validation_issues : []
  const validationIssues = rawIssues.map((issue: any) => ({
    level: typeof issue?.level === 'string' ? issue.level : 'error',
    location: typeof issue?.location === 'string' ? issue.location : 'tool_definition',
    message: typeof issue?.message === 'string' ? issue.message : '工具定义校验失败',
    code: typeof issue?.code === 'string' ? issue.code : undefined,
  }))
  const status = raw?.status === 'error' || raw?.status === 'warning' || raw?.status === 'ok'
    ? raw.status
    : validationIssues.some((issue: ToolValidationIssue) => issue.level === 'error')
      ? 'error'
      : validationIssues.length > 0
        ? 'warning'
        : 'ok'
  return {
    status,
    validation_issues: validationIssues,
  }
}

function buildToolPayload(): { payload?: ToolPayload; error?: ToolValidationIssue } {
  const toolName = formModel.value.tool_name.trim()
  if (!toolName) {
    return {
      error: {
        level: 'error',
        location: 'tool_name',
        message: '请输入工具标识名称',
        code: 'TOOL_NAME_REQUIRED',
      },
    }
  }

  // 命名规范校验（与后端 TOOL_NAME_PATTERN、DB CHECK 约束保持一致）：snake_case，禁止点号
  const TOOL_NAME_RE = /^[a-z][a-z0-9_]{0,63}$/
  if (!TOOL_NAME_RE.test(toolName)) {
    return {
      error: {
        level: 'error',
        location: 'tool_name',
        message: '工具标识名称须以小写字母开头，仅含小写字母、数字、下划线，禁止点号(.)与大写字母',
        code: 'TOOL_NAME_INVALID_FORMAT',
      },
    }
  }

  let parametersSchema: Record<string, any>
  try {
    parametersSchema = JSON.parse(formModel.value.parameters_schema_str)
  } catch {
    return {
      error: {
        level: 'error',
        location: 'parameters_schema',
        message: '参数 Schema 格式不符合 JSON 规范',
        code: 'SCHEMA_JSON_INVALID',
      },
    }
  }

  let examples: any[]
  try {
    const parsedExamples = JSON.parse(formModel.value.examples_str)
    if (!Array.isArray(parsedExamples)) {
      return {
        error: {
          level: 'error',
          location: 'examples',
          message: '调用示例必须是 JSON 数组',
          code: 'EXAMPLES_NOT_ARRAY',
        },
      }
    }
    examples = parsedExamples
  } catch {
    return {
      error: {
        level: 'error',
        location: 'examples',
        message: '调用示例格式不符合 JSON 数组规范',
        code: 'EXAMPLES_JSON_INVALID',
      },
    }
  }

  return {
    payload: {
      tool_name: toolName,
      display_name: formModel.value.display_name.trim() || toolName,
      category: formModel.value.category,
      description: formModel.value.description.trim(),
      usage_template: formModel.value.usage_template.trim() || null,
      parameters_schema: parametersSchema,
      examples,
      risk_level: formModel.value.risk_level,
      is_active: formModel.value.is_active,
      version: formModel.value.version,
    },
  }
}

async function validateCurrentToolDefinition(options: { silent?: boolean } = {}) {
  const built = buildToolPayload()
  if (built.error) {
    validationResult.value = {
      status: 'error',
      validation_issues: [built.error],
    }
    lastValidatedSignature.value = getValidationSignature()
    if (!options.silent) {
      ElMessage.error(built.error.message)
    }
    return null
  }

  validationLoading.value = true
  try {
    const res = await fetch('/api/v1/tools/validate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeader },
      body: JSON.stringify(built.payload),
    })
    const body = await res.json().catch(() => ({}))
    if (!res.ok) {
      throw new Error(typeof body?.detail === 'string' ? body.detail : `HTTP ${res.status}`)
    }

    const normalized = normalizeValidationResult(body)
    validationResult.value = normalized
    lastValidatedSignature.value = getValidationSignature()

    if (!options.silent) {
      if (normalized.status === 'ok') {
        ElMessage.success('工具定义校验通过')
      } else if (normalized.status === 'warning') {
        ElMessage.warning('工具定义存在警告，请确认后再保存')
      } else {
        ElMessage.error('工具定义校验失败，请修复错误后再保存')
      }
    }
    return normalized
  } catch (e: any) {
    console.error('校验工具定义失败:', e)
    if (!options.silent) {
      ElMessage.error(e.message || '校验工具定义失败')
    }
    return null
  } finally {
    validationLoading.value = false
  }
}

// 提交表单
async function submitForm() {
  const built = buildToolPayload()
  if (built.error) {
    validationResult.value = {
      status: 'error',
      validation_issues: [built.error],
    }
    lastValidatedSignature.value = getValidationSignature()
    ElMessage.error(built.error.message)
    return
  }

  const validation = await validateCurrentToolDefinition({ silent: true })
  if (!validation) {
    return
  }
  if (validation.status === 'error') {
    ElMessage.error('工具定义校验未通过，请先修复错误')
    return
  }

  submitting.value = true
  try {
    const url = isEdit.value ? `/api/v1/tools/${formModel.value.id}` : '/api/v1/tools'
    const method = isEdit.value ? 'PUT' : 'POST'

    const res = await fetch(url, {
      method,
      headers: { 'Content-Type': 'application/json', ...authHeader },
      body: JSON.stringify(built.payload)
    })
    
    if (!res.ok) {
      const err = await res.json().catch(() => ({}))
      if (err?.detail && typeof err.detail === 'object') {
        validationResult.value = normalizeValidationResult(err.detail)
        lastValidatedSignature.value = getValidationSignature()
        throw new Error('工具定义校验未通过，请先修复错误')
      }
      throw new Error(err.detail || `HTTP ${res.status}`)
    }

    ElMessage.success(`${dialogTitle.value}成功`)
    dialogVisible.value = false
    resetValidationResult()
    fetchTools()
  } catch (e: any) {
    console.error('提交工具定义失败:', e)
    ElMessage.error(e.message || '操作失败')
  } finally {
    submitting.value = false
  }
}

// 删除工具定义
async function handleDelete(row: ToolDefinition) {
  ElMessageBox.confirm(
    `确认删除工具定义 "${row.display_name}" (${row.tool_name}) 吗？此操作无法撤销。`,
    '高危警示',
    {
      confirmButtonText: '极其确认',
      cancelButtonText: '取消',
      type: 'warning',
      confirmButtonClass: 'el-button--danger'
    }
  ).then(async () => {
    try {
      const res = await fetch(`/api/v1/tools/${row.id}`, {
        method: 'DELETE',
        headers: authHeader
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      ElMessage.success('工具定义已成功删除')
      fetchTools()
    } catch (e) {
      console.error('删除工具定义失败:', e)
      ElMessage.error('删除失败')
    }
  }).catch(() => {})
}

const route = useRoute()
onMounted(() => {
  // 支持从关键信号面板带 ?q=<采集器> 跳入并预填搜索（工具管理页自定义）
  const q = route.query.q
  if (typeof q === 'string' && q) searchQuery.value = q
  fetchTools()
})
</script>

<template>
  <div class="tool-manage-container">
    <!-- 页面头部 -->
    <div class="page-header">
      <div class="header-row">
        <div>
          <h2 class="page-title">工具注册表</h2>
          <p class="page-desc">管理 ReAct 引擎可调用的 HCI 诊断插件及 API 接口。</p>
        </div>
        <el-button type="primary" @click="openCreateDialog">
          <el-icon class="el-icon--left"><Plus /></el-icon> 新建工具定义
        </el-button>
      </div>
    </div>

    <!-- 过滤栏 -->
    <el-card class="filter-card" shadow="never" style="margin-bottom: 16px;">
      <el-row :gutter="16" align="middle">
        <el-col :span="8">
          <el-input
            v-model="searchQuery"
            placeholder="搜索工具标识、展示名称、描述..."
            clearable
          >
            <template #prefix>
              <el-icon><Search /></el-icon>
            </template>
          </el-input>
        </el-col>
        <el-col :span="6">
          <el-select v-model="categoryFilter" placeholder="执行后端分类" clearable style="width: 100%">
            <el-option label="ACLI 节点执行 (acli)" value="acli" />
            <el-option label="SCP 平台 API (scp)" value="scp" />
            <el-option label="SOP 导航引擎 (sop)" value="sop" />
            <el-option label="QKV 前端信号 (qkv)" value="qkv" />
            <el-option label="QFK 后端信号 (qfk)" value="qfk" />
          </el-select>
        </el-col>
        <el-col :span="10" class="total-info" style="text-align: right; color: #909399; font-size: 14px;">
          共 <strong>{{ filteredTools.length }}</strong> 个工具
        </el-col>
      </el-row>
    </el-card>

    <!-- 数据表 -->
    <el-card shadow="never" class="table-card">
      <el-table
        v-loading="loading"
        :data="filteredTools"
        stripe
        style="width: 100%"
        class="custom-table"
      >
        <el-table-column prop="tool_name" label="工具标识名 (tool_name)" min-width="160" show-overflow-tooltip>
          <template #default="{ row }">
            <code class="code-badge">{{ row.tool_name }}</code>
          </template>
        </el-table-column>

        <el-table-column prop="display_name" label="展示名称" min-width="140" />

        <el-table-column prop="category" label="分类" width="120" align="center">
          <template #default="{ row }">
            <el-tag :type="row.category === 'acli' ? 'success' : row.category === 'scp' ? 'primary' : row.category === 'qkv' ? 'info' : row.category === 'qfk' ? 'danger' : 'warning'">
              {{ row.category }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column prop="risk_level" label="风险等级" width="130" align="center">
          <template #default="{ row }">
            <span v-if="row.risk_level === 1" class="risk-indicator risk-low">● 只读</span>
            <span v-else-if="row.risk_level === 2" class="risk-indicator risk-med">▲ 写操作</span>
            <span v-else class="risk-indicator risk-high">■ 高危</span>
          </template>
        </el-table-column>

        <el-table-column prop="is_active" label="启用状态" width="110" align="center">
          <template #default="{ row }">
            <el-switch
              v-model="row.is_active"
              active-color="#13ce66"
              inactive-color="#ff4949"
              @change="handleStatusChange(row)"
            />
          </template>
        </el-table-column>

        <el-table-column prop="version" label="版本" width="80" align="center">
          <template #default="{ row }">
            <span class="text-secondary">{{ row.version }}</span>
          </template>
        </el-table-column>

        <el-table-column label="说明" min-width="220" prop="description" show-overflow-tooltip />

        <el-table-column label="操作" width="180" fixed="right" align="center">
          <template #default="{ row }">
            <el-button type="primary" size="small" text :icon="Edit" @click="openEditDialog(row)">编辑</el-button>
            <el-button type="danger" size="small" text :icon="Delete" @click="handleDelete(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 新建/编辑工具表单 Dialog -->
    <el-dialog
      v-model="dialogVisible"
      width="90%"
      class="premium-dialog"
      :fullscreen="isFullscreen"
      draggable
      align-center
      destroy-on-close
    >
      <template #header>
        <div class="custom-dialog-header" style="display: flex; justify-content: space-between; align-items: center; width: 100%; padding-right: 32px; box-sizing: border-box;">
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

      <el-form :model="formModel" label-position="top" class="dialog-form">
        <el-row :gutter="24">
          <!-- 左栏：基本元数据 -->
          <el-col :span="8" class="form-meta-col" style="border-right: 1px solid #e4e7ed; padding-right: 20px;">
            <el-form-item label="工具唯一标识" required>
              <el-input v-model="formModel.tool_name" placeholder="例如: acli_vm_list" :disabled="isEdit" />
            </el-form-item>
            <el-form-item label="展示名称" required>
              <el-input v-model="formModel.display_name" placeholder="例如: 查询虚拟机列表" />
            </el-form-item>
            <el-form-item label="执行分类" required>
              <el-select v-model="formModel.category" style="width:100%">
                <el-option label="ACLI 节点执行 (acli)" value="acli" />
                <el-option label="SCP 平台 API (scp)" value="scp" />
                <el-option label="SOP 导航引擎 (sop)" value="sop" />
                <el-option label="QKV 前端信号 (qkv)" value="qkv" />
                <el-option label="QFK 后端信号 (qfk)" value="qfk" />
              </el-select>
            </el-form-item>
            <el-form-item label="风险等级">
              <el-radio-group v-model="formModel.risk_level" style="width:100%">
                <el-radio-button :value="1">只读 (1)</el-radio-button>
                <el-radio-button :value="2">写操作 (2)</el-radio-button>
                <el-radio-button :value="3">高危 (3)</el-radio-button>
              </el-radio-group>
            </el-form-item>
            <el-form-item label="接口版本">
              <el-input v-model="formModel.version" placeholder="1.0" />
            </el-form-item>
            <el-form-item label="状态">
              <el-switch v-model="formModel.is_active" active-text="启用" inactive-text="下线" />
            </el-form-item>
            <el-form-item label="使用命令模板">
              <el-input v-model="formModel.usage_template" placeholder="例如: acli --formatter json vm list (ACLI 插件可填，其余可为空)" />
            </el-form-item>
          </el-col>

          <!-- 右栏：代码/JSON 大编辑器 -->
          <el-col :span="16" style="padding-left: 20px;">
            <el-form-item label="功能描述" required>
              <el-input
                v-model="formModel.description"
                type="textarea"
                :rows="3"
                placeholder="说明工具的详细作用和场景，供大模型 ReAct 思路理解。例如: 查询 HCI 平台当前虚拟机列表，用于确认虚机状态..."
              />
            </el-form-item>
            <el-form-item label="参数 Schema (JSON)" required class="flex-form-item">
              <!-- QKV/QFK 工具：可视化编辑器 -->
              <template v-if="isSignalTool">
                <el-tabs v-model="activeParamTab" class="param-tabs" @tab-change="() => {}">
                  <el-tab-pane label="可视化编辑" name="form">
                    <ProducesEditor v-if="formModel.category === 'qkv'" v-model="producesData" />
                    <MatcherEditor v-if="formModel.category === 'qfk'" v-model="matcherData" />
                  </el-tab-pane>
                  <el-tab-pane label="JSON 编辑" name="json">
                    <el-input
                      v-model="formModel.parameters_schema_str"
                      type="textarea"
                      :rows="12"
                      class="code-textarea"
                      placeholder='{"type": "object", "properties": {}}'
                    />
                  </el-tab-pane>
                </el-tabs>
                <div class="json-validator-indicator" :class="isValidJson(formModel.parameters_schema_str) ? 'valid' : 'invalid'">
                  {{ isValidJson(formModel.parameters_schema_str) ? '✓ JSON 格式正确' : '✗ 格式错误：请输入合法 JSON' }}
                </div>
              </template>
              <!-- 其他工具：JSON 编辑器 -->
              <template v-else>
                <div class="json-editor-wrapper">
                  <el-input
                    v-model="formModel.parameters_schema_str"
                    type="textarea"
                    :rows="12"
                    class="code-textarea"
                    placeholder='{"type": "object", "properties": {}}'
                  />
                  <span class="json-validator-indicator" :class="isValidJson(formModel.parameters_schema_str) ? 'valid' : 'invalid'">
                    {{ isValidJson(formModel.parameters_schema_str) ? '✓ JSON 格式正确' : '✗ 格式错误：请输入合法 JSON' }}
                  </span>
                </div>
              </template>
            </el-form-item>
            <el-form-item label="调用示例 (JSON 数组)" class="flex-form-item">
              <div class="json-editor-wrapper">
                <el-input
                  v-model="formModel.examples_str"
                  type="textarea"
                  :rows="8"
                  class="code-textarea"
                  placeholder='[{"cmd": "acli vm list", "desc": "列出虚拟机"}]'
                />
                <span class="json-validator-indicator" :class="isValidJson(formModel.examples_str) ? 'valid' : 'invalid'">
                  {{ isValidJson(formModel.examples_str) ? '✓ JSON 格式正确' : '✗ 格式错误：请输入合法 JSON 数组' }}
                </span>
              </div>
            </el-form-item>
            <div
              v-if="validationResult"
              class="validation-panel"
              :class="[`is-${validationResult.status}`, { 'is-stale': isValidationStale }]"
            >
              <div class="validation-panel__header">
                <div class="validation-panel__title">
                  <span>{{ validationStatusText }}</span>
                  <el-tag
                    size="small"
                    effect="plain"
                    :type="validationResult.status === 'ok' ? 'success' : validationResult.status === 'warning' ? 'warning' : 'danger'"
                  >
                    {{ validationResult.status }}
                  </el-tag>
                </div>
                <span v-if="isValidationStale" class="validation-stale">表单已修改</span>
              </div>

              <el-empty
                v-if="validationResult.validation_issues.length === 0"
                :image-size="48"
                description="未发现契约问题"
              />
              <ul v-else class="validation-list">
                <li
                  v-for="(issue, index) in validationResult.validation_issues"
                  :key="`${issue.code || issue.location}-${index}`"
                  class="validation-item"
                  :class="`is-${issue.level}`"
                >
                  <el-tag
                    size="small"
                    effect="dark"
                    :type="issue.level === 'error' ? 'danger' : issue.level === 'warning' ? 'warning' : 'info'"
                  >
                    {{ issue.level }}
                  </el-tag>
                  <code v-if="issue.code" class="validation-code">{{ issue.code }}</code>
                  <span class="validation-location">{{ issue.location }}</span>
                  <span class="validation-message">{{ issue.message }}</span>
                </li>
              </ul>
            </div>
          </el-col>
        </el-row>
      </el-form>

      <template #footer>
        <div class="dialog-footer">
          <el-button :disabled="submitting || validationLoading" @click="dialogVisible = false">取消</el-button>
          <el-button :icon="Search" :loading="validationLoading" @click="validateCurrentToolDefinition()">
            校验工具定义
          </el-button>
          <el-button type="primary" :loading="submitting" :disabled="validationLoading" @click="submitForm">
            保存
          </el-button>
        </div>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.tool-manage-container {
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
  margin: 0 0 8px;
  font-size: 22px;
  color: #303133;
}

.page-desc {
  margin: 0;
  color: #666;
  font-size: 14px;
}

.filter-card {
  margin-bottom: 16px;
}

.total-info {
  text-align: right;
  color: #909399;
  font-size: 14px;
}

.table-card {
  min-height: 400px;
}

.code-badge {
  background: #f8f9fa;
  border: 1px solid #e9ecef;
  color: #e83e8c;
  padding: 3px 8px;
  border-radius: 4px;
  font-family: Consolas, Monaco, 'Andale Mono', monospace;
  font-size: 13px;
}

.risk-indicator {
  font-size: 13px;
  font-weight: 500;
}

.risk-low {
  color: #2ecc71;
}

.risk-med {
  color: #f39c12;
}

.risk-high {
  color: #e74c3c;
}

.text-secondary {
  color: #95a5a6;
  font-size: 13px;
}

.json-editor-wrapper {
  position: relative;
  width: 100%;
}

.code-textarea :deep(.el-textarea__inner) {
  font-family: Consolas, Monaco, monospace;
  font-size: 12px;
  background-color: #fafbfc;
  color: #24292e;
}

.json-validator-indicator {
  display: block;
  font-size: 11px;
  margin-top: 4px;
  font-weight: bold;
}

.json-validator-indicator.valid {
  color: #2ecc71;
}

.json-validator-indicator.invalid {
  color: #e74c3c;
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
  transition: all 0.2s;
}

.fullscreen-toggle-btn:hover {
  background: #f1f5f9;
  color: #409eff;
  transform: scale(1.1);
}

.dialog-form {
  width: 100%;
}

.dialog-form :deep(.el-form-item) {
  margin-bottom: 20px;
}

.dialog-footer {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
}

.validation-panel {
  border: 1px solid #dcdfe6;
  border-radius: 6px;
  padding: 12px;
  background: #fff;
}

.validation-panel.is-ok {
  border-color: #b3e19d;
  background: #f0f9eb;
}

.validation-panel.is-warning {
  border-color: #f3d19e;
  background: #fdf6ec;
}

.validation-panel.is-error {
  border-color: #fab6b6;
  background: #fef0f0;
}

.validation-panel.is-stale {
  border-style: dashed;
}

.validation-panel__header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  margin-bottom: 8px;
}

.validation-panel__title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
  color: #303133;
}

.validation-stale {
  color: #909399;
  font-size: 12px;
}

.validation-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 0;
  margin: 0;
  list-style: none;
}

.validation-item {
  display: grid;
  grid-template-columns: auto minmax(120px, auto) minmax(120px, 180px) minmax(0, 1fr);
  align-items: center;
  gap: 8px;
  min-height: 30px;
  color: #303133;
}

.validation-code {
  color: #606266;
  font-size: 12px;
  background: rgba(255, 255, 255, 0.72);
  border: 1px solid rgba(220, 223, 230, 0.88);
  border-radius: 4px;
  padding: 2px 6px;
  overflow-wrap: anywhere;
}

.validation-location {
  color: #606266;
  font-size: 12px;
  overflow-wrap: anywhere;
}

.validation-message {
  color: #303133;
  font-size: 13px;
  overflow-wrap: anywhere;
}

/* 统一 premium-dialog 高端弹窗样式 */
:global(.premium-dialog) {
  display: flex;
  flex-direction: column;
}

:global(.premium-dialog .el-dialog) {
  display: flex;
  flex-direction: column;
  max-height: 90vh;
  border-radius: 8px;
  overflow: hidden;
  box-shadow: 0 12px 32px rgba(0, 0, 0, 0.1) !important;
}

:global(.premium-dialog.is-fullscreen .el-dialog) {
  max-height: 100vh;
  height: 100vh;
  border-radius: 0;
}

:global(.premium-dialog .el-dialog__header) {
  background-color: #f8f9fa;
  margin-right: 0;
  padding: 16px 24px;
  border-bottom: 1px solid #eee;
  flex-shrink: 0;
}

:global(.premium-dialog .el-dialog__title) {
  font-weight: 600;
  color: #2c3e50;
  font-size: 16px;
}

:global(.premium-dialog .el-dialog__body) {
  padding: 24px;
  flex: 1;
  overflow-y: auto;
}

:global(.premium-dialog .el-dialog__footer) {
  padding: 12px 24px;
  border-top: 1px solid #eee;
  background-color: #f8f9fa;
  flex-shrink: 0;
}

/* 全屏自适应，防止下方大面积留白 */
:global(.premium-dialog.is-fullscreen .el-dialog__body) {
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

:global(.premium-dialog.is-fullscreen .el-form),
:global(.premium-dialog.is-fullscreen .premium-form),
:global(.premium-dialog.is-fullscreen .premium-form-wrapper) {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
}

:global(.premium-dialog.is-fullscreen .flex-form-item) {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
  margin-bottom: 12px;
}

:global(.premium-dialog.is-fullscreen .flex-form-item .el-form-item__content) {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
}

:global(.premium-dialog.is-fullscreen .editor-textarea),
:global(.premium-dialog.is-fullscreen .code-editor-wrapper),
:global(.premium-dialog.is-fullscreen .json-editor-wrapper) {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
  height: 100% !important;
}

:global(.premium-dialog.is-fullscreen .editor-textarea .el-textarea__inner),
:global(.premium-dialog.is-fullscreen .json-editor-wrapper .el-textarea__inner) {
  flex: 1;
  height: 100% !important;
  resize: none;
}

:global(.premium-dialog.is-fullscreen .code-textarea) {
  flex: 1;
  height: 100% !important;
}

:global(.premium-dialog.is-fullscreen .el-row) {
  flex: 1;
  display: flex;
  min-height: 0;
}

:global(.premium-dialog.is-fullscreen .el-col) {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}

:global(.premium-dialog.is-fullscreen .form-meta-col) {
  overflow-y: auto;
}

/* QKV/QFK 参数可视化编辑 Tab 样式 */
.param-tabs {
  width: 100%;
  margin-bottom: 8px;
}

.param-tabs :deep(.el-tabs__header) {
  margin-bottom: 12px;
}

.param-tabs :deep(.el-tabs__item) {
  font-size: 13px;
  padding: 0 16px;
}

.param-tabs :deep(.el-tabs__content) {
  overflow: visible;
}

.param-tabs :deep(.el-tab-pane) {
  min-height: 120px;
}
</style>
