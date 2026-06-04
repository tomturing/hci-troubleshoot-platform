<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Edit, Delete } from '@element-plus/icons-vue'

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

// 打开新建弹窗
function openCreateDialog() {
  isEdit.value = false
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

// 提交表单
async function submitForm() {
  // 基础校验
  if (!formModel.value.tool_name.trim()) {
    ElMessage.warning('请输入工具标识名称')
    return
  }
  if (!isValidJson(formModel.value.parameters_schema_str)) {
    ElMessage.error('参数 Schema 格式不符合 JSON 规范')
    return
  }
  if (!isValidJson(formModel.value.examples_str)) {
    ElMessage.error('调用示例格式不符合 JSON 数组规范')
    return
  }

  const payload = {
    tool_name: formModel.value.tool_name.trim(),
    display_name: formModel.value.display_name.trim() || formModel.value.tool_name.trim(),
    category: formModel.value.category,
    description: formModel.value.description.trim(),
    usage_template: formModel.value.usage_template.trim() || null,
    parameters_schema: JSON.parse(formModel.value.parameters_schema_str),
    examples: JSON.parse(formModel.value.examples_str),
    risk_level: formModel.value.risk_level,
    is_active: formModel.value.is_active,
    version: formModel.value.version
  }

  try {
    const url = isEdit.value ? `/api/v1/tools/${formModel.value.id}` : '/api/v1/tools'
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
    fetchTools()
  } catch (e: any) {
    console.error('提交工具定义失败:', e)
    ElMessage.error(e.message || '操作失败')
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

onMounted(() => {
  fetchTools()
})
</script>

<template>
  <div class="tool-manage-container">
    <el-card class="box-card main-card">
      <template #header>
        <div class="card-header">
          <div class="header-left">
            <span class="title">AI 工具注册表 ({{ filteredTools.length }})</span>
            <span class="subtitle">管理 ReAct 引擎可调用的 HCI 诊断插件及 API 接口</span>
          </div>
          <el-button type="primary" class="gradient-btn" @click="openCreateDialog">
            <el-icon class="el-icon--left"><Plus /></el-icon> 新建工具定义
          </el-button>
        </div>
      </template>

      <!-- 过滤栏 -->
      <div class="filter-bar">
        <el-input
          v-model="searchQuery"
          placeholder="搜索工具标识、展示名称、描述..."
          class="search-input"
          clearable
        >
          <template #prefix>
            <el-icon><Search /></el-icon>
          </template>
        </el-input>

        <el-select v-model="categoryFilter" placeholder="执行后端分类" clearable class="category-select">
          <el-option label="ACLI 节点执行 (acli)" value="acli" />
          <el-option label="SCP 平台 API (scp)" value="scp" />
          <el-option label="SOP 导航引擎 (sop)" value="sop" />
        </el-select>
      </div>

      <!-- 数据表 -->
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
            <el-tag :type="row.category === 'acli' ? 'success' : row.category === 'scp' ? 'primary' : 'warning'">
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

        <el-table-column label="操作" width="150" fixed="right" align="center">
          <template #default="{ row }">
            <el-button-group>
              <el-button type="primary" size="small" :icon="Edit" @click="openEditDialog(row)">编辑</el-button>
              <el-button type="danger" size="small" :icon="Delete" @click="handleDelete(row)">删除</el-button>
            </el-button-group>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 新建/编辑工具表单 Dialog -->
    <el-dialog
      v-model="dialogVisible"
      :title="dialogTitle"
      width="720px"
      destroy-on-close
      class="custom-dialog"
    >
      <el-form :model="formModel" label-width="120px" class="dialog-form">
        <el-row :gutter="20">
          <el-col :span="12">
            <el-form-item label="工具唯一标识" required>
              <el-input v-model="formModel.tool_name" placeholder="例如: acli_vm_list" :disabled="isEdit" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="展示名称" required>
              <el-input v-model="formModel.display_name" placeholder="例如: 查询虚拟机列表" />
            </el-form-item>
          </el-col>
        </el-row>

        <el-row :gutter="20">
          <el-col :span="12">
            <el-form-item label="执行分类" required>
              <el-select v-model="formModel.category" style="width:100%">
                <el-option label="ACLI 节点执行 (acli)" value="acli" />
                <el-option label="SCP 平台 API (scp)" value="scp" />
                <el-option label="SOP 导航引擎 (sop)" value="sop" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="风险等级">
              <el-radio-group v-model="formModel.risk_level">
                <el-radio-button :value="1">只读 (1)</el-radio-button>
                <el-radio-button :value="2">写操作 (2)</el-radio-button>
                <el-radio-button :value="3">高危 (3)</el-radio-button>
              </el-radio-group>
            </el-form-item>
          </el-col>
        </el-row>

        <el-row :gutter="20">
          <el-col :span="12">
            <el-form-item label="接口版本">
              <el-input v-model="formModel.version" placeholder="1.0" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="状态">
              <el-switch v-model="formModel.is_active" active-text="启用" inactive-text="下线" />
            </el-form-item>
          </el-col>
        </el-row>

        <el-form-item label="使用命令模板">
          <el-input v-model="formModel.usage_template" placeholder="例如: acli vm list --formatter json (ACLI 插件可填，其余可为空)" />
        </el-form-item>

        <el-form-item label="功能描述" required>
          <el-input
            v-model="formModel.description"
            type="textarea"
            :rows="3"
            placeholder="说明工具的详细作用和场景，供大模型 ReAct 思路理解。例如: 查询 HCI 平台当前虚拟机列表，用于确认虚机状态..."
          />
        </el-form-item>

        <el-form-item label="参数 Schema (JSON)" required>
          <div class="json-editor-wrapper">
            <el-input
              v-model="formModel.parameters_schema_str"
              type="textarea"
              :rows="6"
              class="code-textarea"
              placeholder='{"type": "object", "properties": {}}'
            />
            <span class="json-validator-indicator" :class="isValidJson(formModel.parameters_schema_str) ? 'valid' : 'invalid'">
              {{ isValidJson(formModel.parameters_schema_str) ? '✓ JSON 格式正确' : '✗ 格式错误：请输入合法 JSON' }}
            </span>
          </div>
        </el-form-item>

        <el-form-item label="调用示例 (JSON 数组)">
          <div class="json-editor-wrapper">
            <el-input
              v-model="formModel.examples_str"
              type="textarea"
              :rows="4"
              class="code-textarea"
              placeholder='[{"cmd": "acli vm list", "desc": "列出虚拟机"}]'
            />
            <span class="json-validator-indicator" :class="isValidJson(formModel.examples_str) ? 'valid' : 'invalid'">
              {{ isValidJson(formModel.examples_str) ? '✓ JSON 格式正确' : '✗ 格式错误：请输入合法 JSON 数组' }}
            </span>
          </div>
        </el-form-item>
      </el-form>

      <template #footer>
        <div class="dialog-footer">
          <el-button @click="dialogVisible = false">取消</el-button>
          <el-button type="primary" class="gradient-btn" @click="submitForm">保存</el-button>
        </div>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.tool-manage-container {
  max-width: 1200px;
  margin: 0 auto;
}

.main-card {
  border-radius: 12px;
  box-shadow: 0 4px 20px rgba(0, 0, 0, 0.05);
  background: #fff;
  border: none;
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.header-left {
  display: flex;
  flex-direction: column;
}

.title {
  font-size: 18px;
  font-weight: 600;
  color: #2c3e50;
}

.subtitle {
  font-size: 13px;
  color: #7f8c8d;
  margin-top: 4px;
}

.gradient-btn {
  background: linear-gradient(135deg, #3498db, #2980b9);
  border: none;
  color: white;
  transition: all 0.3s ease;
}

.gradient-btn:hover {
  transform: translateY(-2px);
  box-shadow: 0 4px 12px rgba(41, 128, 185, 0.4);
}

.filter-bar {
  display: flex;
  gap: 15px;
  margin-bottom: 20px;
}

.search-input {
  max-width: 360px;
}

.category-select {
  width: 200px;
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

.custom-dialog :deep(.el-dialog) {
  border-radius: 12px;
  overflow: hidden;
}

.custom-dialog :deep(.el-dialog__header) {
  background-color: #f8f9fa;
  margin-right: 0;
  padding: 20px;
  border-bottom: 1px solid #eee;
}

.custom-dialog :deep(.el-dialog__title) {
  font-weight: 600;
  color: #2c3e50;
}

.custom-dialog :deep(.el-dialog__body) {
  padding: 25px 30px;
}

.custom-dialog :deep(.el-dialog__footer) {
  padding: 15px 30px;
  border-top: 1px solid #eee;
}

.dialog-form :deep(.el-form-item) {
  margin-bottom: 20px;
}
</style>
