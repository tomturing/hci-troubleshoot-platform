<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Search, Edit, Delete } from '@element-plus/icons-vue'

interface SkillDefinition {
  id: number
  skill_name: string
  display_name: string
  description: string
  parameters_schema: Record<string, any>
  output_schema: Record<string, any>
  is_active: boolean
  version: string
  created_at?: string
  updated_at?: string
}

const skills = ref<SkillDefinition[]>([])
const loading = ref(false)
const searchQuery = ref('')

// 获取 internalToken
const internalToken = import.meta.env.VITE_INTERNAL_API_TOKEN || 'hci-dev-internal-token'
const authHeader = { Authorization: `Bearer ${internalToken}` }

// 过滤后的技能列表
const filteredSkills = computed(() => {
  return skills.value.filter(s => {
    return (
      s.skill_name.toLowerCase().includes(searchQuery.value.toLowerCase()) ||
      s.display_name.toLowerCase().includes(searchQuery.value.toLowerCase()) ||
      s.description.toLowerCase().includes(searchQuery.value.toLowerCase())
    )
  })
})

// 加载技能列表
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

// 快速切换启用状态
async function handleStatusChange(row: SkillDefinition) {
  try {
    const res = await fetch(`/api/v1/skills/${row.id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', ...authHeader },
      body: JSON.stringify({
        skill_name: row.skill_name,
        display_name: row.display_name,
        description: row.description,
        parameters_schema: row.parameters_schema,
        output_schema: row.output_schema,
        is_active: row.is_active,
        version: row.version
      })
    })
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    ElMessage.success(`技能已${row.is_active ? '启用' : '禁用'}`)
  } catch (e) {
    console.error('更新技能状态失败:', e)
    row.is_active = !row.is_active // 恢复开关状态
    ElMessage.error('更新技能状态失败')
  }
}

// 弹窗表单状态
const dialogVisible = ref(false)
const isEdit = ref(false)
const dialogTitle = computed(() => isEdit.value ? '编辑技能定义' : '新建技能定义')

const formModel = ref({
  id: 0,
  skill_name: '',
  display_name: '',
  description: '',
  parameters_schema_str: '{}',
  output_schema_str: '{}',
  is_active: true,
  version: '1.0'
})

// 打开新建弹窗
function openCreateDialog() {
  isEdit.value = false
  formModel.value = {
    id: 0,
    skill_name: '',
    display_name: '',
    description: '',
    parameters_schema_str: '{\n  "type": "object",\n  "properties": {\n    "smart_info": {\n      "type": "string",\n      "description": "SMART 原始回显"\n    }\n  },\n  "required": ["smart_info"]\n}',
    output_schema_str: '{\n  "type": "object",\n  "properties": {\n    "status": {\n      "type": "string",\n      "enum": ["正常", "返修"]\n    }\n  },\n  "required": ["status"]\n}',
    is_active: true,
    version: '1.0'
  }
  dialogVisible.value = true
}

// 打开编辑弹窗
function openEditDialog(row: SkillDefinition) {
  isEdit.value = true
  formModel.value = {
    id: row.id,
    skill_name: row.skill_name,
    display_name: row.display_name,
    description: row.description,
    parameters_schema_str: JSON.stringify(row.parameters_schema, null, 2),
    output_schema_str: JSON.stringify(row.output_schema, null, 2),
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
  if (!formModel.value.skill_name.trim()) {
    ElMessage.warning('请输入技能标识名称')
    return
  }
  if (!isValidJson(formModel.value.parameters_schema_str)) {
    ElMessage.error('输入参数 Schema 格式不符合 JSON 规范')
    return
  }
  if (!isValidJson(formModel.value.output_schema_str)) {
    ElMessage.error('输出结果 Schema 格式不符合 JSON 规范')
    return
  }

  const payload = {
    skill_name: formModel.value.skill_name.trim(),
    display_name: formModel.value.display_name.trim() || formModel.value.skill_name.trim(),
    description: formModel.value.description.trim(),
    parameters_schema: JSON.parse(formModel.value.parameters_schema_str),
    output_schema: JSON.parse(formModel.value.output_schema_str),
    is_active: formModel.value.is_active,
    version: formModel.value.version
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

// 删除技能定义
async function handleDelete(row: SkillDefinition) {
  ElMessageBox.confirm(
    `确认删除技能定义 "${row.display_name}" (${row.skill_name}) 吗？此操作无法撤销。`,
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
      ElMessage.success('技能定义已成功删除')
      fetchSkills()
    } catch (e) {
      console.error('删除技能定义失败:', e)
      ElMessage.error('删除失败')
    }
  }).catch(() => {})
}

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
          <p class="page-desc">管理平台内置的、通用的诊断与分析 Skill 算法定义</p>
        </div>
        <el-button type="primary" @click="openCreateDialog">
          <el-icon class="el-icon--left"><Plus /></el-icon> 新建技能定义
        </el-button>
      </div>
    </div>

    <!-- 过滤栏 -->
    <el-card class="filter-card" shadow="never" style="margin-bottom: 16px;">
      <el-row :gutter="16" align="middle">
        <el-col :span="8">
          <el-input
            v-model="searchQuery"
            placeholder="搜索技能标识、展示名称、描述..."
            clearable
          >
            <template #prefix>
              <el-icon><Search /></el-icon>
            </template>
          </el-input>
        </el-col>
        <el-col :span="16" class="total-info" style="text-align: right; color: #909399; font-size: 14px;">
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
      >
        <el-table-column prop="skill_name" label="技能标识名 (skill_name)" min-width="180" show-overflow-tooltip>
          <template #default="{ row }">
            <code class="code-badge">{{ row.skill_name }}</code>
          </template>
        </el-table-column>

        <el-table-column prop="display_name" label="展示名称" min-width="160" />

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

        <el-table-column label="功能描述" min-width="260" prop="description" show-overflow-tooltip />

        <el-table-column label="操作" width="150" fixed="right" align="center">
          <template #default="{ row }">
            <el-button type="primary" size="small" text :icon="Edit" @click="openEditDialog(row)">编辑</el-button>
            <el-button type="danger" size="small" text :icon="Delete" @click="handleDelete(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 新建/编辑技能表单 Dialog -->
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
            <el-form-item label="技能唯一标识" required>
              <el-input v-model="formModel.skill_name" placeholder="例如: disk_vendor_lifetime" :disabled="isEdit" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="展示名称" required>
              <el-input v-model="formModel.display_name" placeholder="例如: 硬盘厂商识别与寿命判定" />
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

        <el-form-item label="功能描述" required>
          <el-input
            v-model="formModel.description"
            type="textarea"
            :rows="3"
            placeholder="说明技能的具体功能、实现原理以及在 SOP 中是如何被调用的。"
          />
        </el-form-item>

        <el-form-item label="输入参数 Schema" required>
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

        <el-form-item label="输出结果 Schema" required>
          <div class="json-editor-wrapper">
            <el-input
              v-model="formModel.output_schema_str"
              type="textarea"
              :rows="6"
              class="code-textarea"
              placeholder='{"type": "object", "properties": {}}'
            />
            <span class="json-validator-indicator" :class="isValidJson(formModel.output_schema_str) ? 'valid' : 'invalid'">
              {{ isValidJson(formModel.output_schema_str) ? '✓ JSON 格式正确' : '✗ 格式错误：请输入合法 JSON' }}
            </span>
          </div>
        </el-form-item>
      </el-form>

      <template #footer>
        <div class="dialog-footer">
          <el-button @click="dialogVisible = false">取消</el-button>
          <el-button type="primary" @click="submitForm">保存</el-button>
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
  border-radius: 4px;
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
