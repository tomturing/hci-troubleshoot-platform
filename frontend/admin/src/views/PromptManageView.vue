<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, InfoFilled, Edit, Delete, FullScreen, Refresh } from '@element-plus/icons-vue'

interface SystemPrompt {
  id: number
  stage: string
  name: string
  description: string | null
  content_template: string
  version: string
  is_active: boolean
  created_at?: string
  updated_at?: string
}

const prompts = ref<SystemPrompt[]>([])
const loading = ref(false)
const activeTab = ref('BASE') // 当前选中的 Stage Tab

// 获取 internalToken
const internalToken = import.meta.env.VITE_INTERNAL_API_TOKEN || 'hci-dev-internal-token'
const authHeader = { Authorization: `Bearer ${internalToken}` }

// 定义各个诊断阶段的可用占位符说明
const placeholdersMap: Record<string, string[]> = {
  BASE: ['{tool_list}'],
  S0: ['{category_list}', '{case_title}', '{case_description}'],
  S1: ['{category_name}'],
  S2: ['{category_name}', '{kbd_context}', '{sop_content}'],
  S3: ['{hypotheses}', '{verification_steps}'],
  S4: ['{hypotheses}'],
  S5: ['{root_cause}'],
  S6: [],
  KBD: ['{count}', '{categories_text}', '{title}', '{problem_desc}', '{context}']
}

// 诊断阶段列表及其中文名
const stages = [
  { value: 'BASE', label: 'BASE 全局角色定义' },
  { value: 'S0', label: 'S0 故障意图识别' },
  { value: 'S1', label: 'S1 诊断信息采集' },
  { value: 'S2', label: 'S2 根因假设生成' },
  { value: 'S3', label: 'S3 假设证据验证' },
  { value: 'S4', label: 'S4 根因确认报告' },
  { value: 'S5', label: 'S5 解决方案输出' },
  { value: 'KBD', label: 'KBD 分类与识图' }
]

// 过滤出当前选定 Stage 的 Prompts
const currentPrompts = computed(() => {
  return prompts.value.filter(p => p.stage === activeTab.value)
})

// 加载 Prompt 列表
async function fetchPrompts() {
  loading.value = true
  try {
    const res = await fetch('/api/v1/prompts', { headers: authHeader })
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    prompts.value = await res.json()
  } catch (e) {
    console.error('加载 Prompt 列表失败:', e)
    ElMessage.error('加载 Prompt 列表失败')
  } finally {
    loading.value = false
  }
}

// 切换激活状态（同一个 stage 仅有一个 true 激活）
async function handleActiveChange(row: SystemPrompt) {
  if (!row.is_active) {
    // 强制保证每个 stage 至少有一个被激活
    // 如果用户尝试直接把唯一的激活关掉，给予提示并恢复
    ElMessage.warning('每个阶段必须保持至少一个激活版本！请直接启用其他版本进行切换。')
    row.is_active = true
    return
  }

  try {
    const res = await fetch(`/api/v1/prompts/${row.id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', ...authHeader },
      body: JSON.stringify({
        stage: row.stage,
        name: row.name,
        description: row.description,
        content_template: row.content_template,
        version: row.version,
        is_active: true // 触发将该版本设为启用，后台自动修改其他
      })
    })
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    ElMessage.success(`阶段 ${row.stage} 激活版本已成功切换为 [${row.name}]`)
    fetchPrompts() // 重新获取以刷新同 stage 其他的 is_active
  } catch (e) {
    console.error('切换激活 Prompt 失败:', e)
    row.is_active = false
    ElMessage.error('切换激活 Prompt 失败')
  }
}

// 弹框表单
const dialogVisible = ref(false)
const isEdit = ref(false)
const isFullscreen = ref(false)
const dialogTitle = computed(() => isEdit.value ? '编辑 Prompt 模板' : '新建 Prompt 模板')

const formModel = ref({
  id: 0,
  stage: 'S0',
  name: '',
  description: '',
  content_template: '',
  version: '1.0',
  is_active: true
})

// 打开新建
function openCreateDialog() {
  isEdit.value = false
  isFullscreen.value = false
  formModel.value = {
    id: 0,
    stage: activeTab.value,
    name: '',
    description: '',
    content_template: '',
    version: '1.0',
    is_active: true
  }
  dialogVisible.value = true
}

// 打开编辑
function openEditDialog(row: SystemPrompt) {
  isEdit.value = true
  isFullscreen.value = false
  formModel.value = {
    id: row.id,
    stage: row.stage,
    name: row.name,
    description: row.description || '',
    content_template: row.content_template,
    version: row.version,
    is_active: row.is_active
  }
  dialogVisible.value = true
}

// 提交表单
async function submitForm() {
  if (!formModel.value.name.trim()) {
    ElMessage.warning('请输入 Prompt 模板唯一标识名称')
    return
  }
  if (!formModel.value.content_template.trim()) {
    ElMessage.warning('请输入 Prompt 模板正文内容')
    return
  }

  const payload = {
    stage: formModel.value.stage,
    name: formModel.value.name.trim(),
    description: formModel.value.description.trim(),
    content_template: formModel.value.content_template,
    version: formModel.value.version.trim() || '1.0',
    is_active: formModel.value.is_active
  }

  try {
    const url = isEdit.value ? `/api/v1/prompts/${formModel.value.id}` : '/api/v1/prompts'
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
    fetchPrompts()
  } catch (e: any) {
    console.error('提交 Prompt 模板失败:', e)
    ElMessage.error(e.message || '操作失败')
  }
}

// 删除 Prompt 模板
async function handleDelete(row: SystemPrompt) {
  if (row.is_active) {
    ElMessage.warning('当前版本正在激活使用中，请先激活其他版本后再行删除！')
    return
  }

  ElMessageBox.confirm(
    `确认删除 Prompt 模板 "${row.name}" (v${row.version}) 吗？此操作无法撤销。`,
    '高危警示',
    {
      confirmButtonText: '极其确认',
      cancelButtonText: '取消',
      type: 'warning',
      confirmButtonClass: 'el-button--danger'
    }
  ).then(async () => {
    try {
      const res = await fetch(`/api/v1/prompts/${row.id}`, {
        method: 'DELETE',
        headers: authHeader
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      ElMessage.success('Prompt 模板已成功删除')
      fetchPrompts()
    } catch (e) {
      console.error('删除 Prompt 模板失败:', e)
      ElMessage.error('删除失败')
    }
  }  ).catch(() => {})
}

// ─── KBD 阶段 Prompt 测试触发（重新识图 / 重新分类 / 重新提交 关键信号）─────────────
const testDialogVisible = ref(false)
const testTarget = ref<SystemPrompt | null>(null)
const testKbdId = ref('')
const testLoading = ref(false)

function _triggerActionForPrompt(name: string): 'vision' | 'classify' | 'signals' {
  if (name.includes('vision')) return 'vision'
  if (name.includes('classify')) return 'classify'
  return 'signals'
}

function openTestTrigger(item: SystemPrompt) {
  testTarget.value = item
  testKbdId.value = ''
  testDialogVisible.value = true
}

async function submitTestTrigger() {
  if (!testTarget.value) return
  const id = Number(testKbdId.value)
  if (!id || id <= 0) {
    ElMessage.warning('请输入有效的 KBD 条目 ID')
    return
  }
  const action = _triggerActionForPrompt(testTarget.value.name)
  const url =
    action === 'vision' ? `/api/v1/kbd/${id}/reanalyze-images?sync=true`
    : action === 'classify' ? `/api/v1/kbd/${id}/reclassify`
    : `/api/v1/kbd/${id}/extract-signals?sync=true`
  testLoading.value = true
  try {
    const resp = await fetch(url, { method: 'POST', headers: authHeader })
    if (!resp.ok) {
      const errData = await resp.json().catch(() => ({}))
      throw new Error(errData.detail || `HTTP ${resp.status}`)
    }
    const data = await resp.json()
    let msg = '触发完成'
    if (action === 'vision') msg = `识图完成：成功 ${data.done ?? 0} 张，失败 ${data.failed ?? 0} 张`
    else if (action === 'classify') msg = `分类完成：${data.category_id}（置信度 ${data.confidence?.toFixed?.(2) ?? 'N/A'}）`
    else msg = `关键信号抽取完成：共 ${data.signals_count ?? 0} 条（拒绝 ${data.rejected_count ?? 0} 条）`
    ElMessage.success(msg)
    testDialogVisible.value = false
  } catch (err: any) {
    ElMessage.error(`触发失败：${err.message || '未知错误'}`)
  } finally {
    testLoading.value = false
  }
}

// 导入 SQL 初始种子数据
async function importSeedData() {
  loading.value = true
  try {
    const res = await fetch('/api/v1/prompts/import-seed-legacy', {
      method: 'POST',
      headers: authHeader
    })
    // 如果没有这个特化的 API 也可以做降级提示，我们通过执行 SQL 后直接刷新 fetch 即可
    fetchPrompts()
  } catch (e) {
    console.error('种子同步失败:', e)
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  fetchPrompts()
})
</script>

<template>
  <div class="prompt-manage-container">
    <!-- 页面头部 -->
    <div class="page-header">
      <div class="header-row">
        <div>
          <h2 class="page-title">Prompt 管理</h2>
          <p class="page-desc">配置智能排障助手在各个阶段所使用的 System Prompt 提示词内容。</p>
        </div>
        <el-button type="primary" @click="openCreateDialog">
          <el-icon class="el-icon--left"><Plus /></el-icon> 新增版本
        </el-button>
      </div>
    </div>

    <el-row :gutter="20">
      <!-- 左侧 Prompt 列表 (占 18 列) -->
      <el-col :span="18">
        <el-card class="box-card main-card" shadow="never">
          <template #header>
            <div class="card-header">
              <div class="header-left">
                <span class="card-title">模板版本列表 ({{ currentPrompts.length }})</span>
                <span class="card-sub">当前阶段：<strong>{{ stages.find(s => s.value === activeTab)?.label }}</strong></span>
              </div>
            </div>
          </template>

          <!-- 该阶段可用占位符说明 -->
          <div class="placeholder-tip-box" v-if="placeholdersMap[activeTab] && placeholdersMap[activeTab].length > 0">
            <span class="tip-title"><el-icon><InfoFilled /></el-icon> 该阶段可用的系统上下文占位符：</span>
            <div class="placeholders-tags">
              <el-tag
                v-for="p in placeholdersMap[activeTab]"
                :key="p"
                size="small"
                type="info"
                effect="dark"
                class="ph-tag"
              >
                {{ p }}
              </el-tag>
            </div>
            <span class="tip-desc">提示: 系统会在组装 Prompt 时自动使用运行时提取的数据替换这些占位符。</span>
          </div>

          <!-- 无数据占位 -->
          <el-empty v-if="currentPrompts.length === 0" description="该阶段下暂无任何 Prompt 模板版本">
            <span class="text-secondary" style="display:block;margin-bottom:15px">
              检测到 system_prompt 表目前在数据库中为空。你可以执行 SQL 导入种子数据：
              <br/>
              <code>database/seeds/02_system_prompts.sql</code>
            </span>
          </el-empty>

          <!-- 列表卡片 -->
          <div class="prompt-card-list" v-loading="loading">
            <div
              v-for="item in currentPrompts"
              :key="item.id"
              class="prompt-card"
              :class="{ active: item.is_active }"
            >
              <div class="prompt-card-header">
                <div class="p-header-left">
                  <span class="p-name">{{ item.name }}</span>
                  <el-tag size="small" class="version-tag">v{{ item.version }}</el-tag>
                </div>
                <div class="p-header-right">
                  <span class="active-label">{{ item.is_active ? '正在使用' : '未激活' }}</span>
                  <el-switch
                    v-model="item.is_active"
                    active-color="#13ce66"
                    inactive-color="#c0ccda"
                    @change="handleActiveChange(item)"
                  />
                </div>
              </div>

              <div class="prompt-description" v-if="item.description">
                <strong>模板用途说明：</strong> {{ item.description }}
              </div>

              <div class="prompt-template-preview">
                <pre class="template-pre"><code>{{ item.content_template }}</code></pre>
              </div>

              <div class="prompt-card-actions">
                <el-button type="primary" size="small" text :icon="Edit" @click="openEditDialog(item)">编辑修改</el-button>
                <el-button
                  type="danger"
                  size="small"
                  text
                  :icon="Delete"
                  :disabled="item.is_active"
                  @click="handleDelete(item)"
                >
                  删除模板
                </el-button>
                <el-button
                  v-if="item.stage === 'KBD'"
                  type="warning"
                  size="small"
                  text
                  :icon="Refresh"
                  @click="openTestTrigger(item)"
                >
                  测试触发
                </el-button>
              </div>
            </div>
          </div>
        </el-card>
      </el-col>

      <!-- 右侧阶段侧边栏 (占 6 列) -->
      <el-col :span="6">
        <div class="stage-sidebar">
          <div
            v-for="stage in stages"
            :key="stage.value"
            class="stage-item"
            :class="{ active: activeTab === stage.value }"
            @click="activeTab = stage.value"
          >
            <div class="stage-badge" :class="`badge-${stage.value.toLowerCase()}`">{{ stage.value }}</div>
            <span class="stage-label">{{ stage.label }}</span>
          </div>
        </div>
      </el-col>
    </el-row>

    <!-- 新建/编辑 Prompt 模板 Dialog -->
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
            <el-form-item label="诊断阶段 (Stage)" required>
              <el-select v-model="formModel.stage" style="width:100%" :disabled="isEdit">
                <el-option v-for="s in stages" :key="s.value" :label="s.label" :value="s.value" />
              </el-select>
            </el-form-item>
            <el-form-item label="版本号">
              <el-input v-model="formModel.version" placeholder="1.0" />
            </el-form-item>
            <el-form-item label="模板唯一标识名" required>
              <el-input v-model="formModel.name" placeholder="例如: s0_intent_recognition_v2" :disabled="isEdit" />
            </el-form-item>
            <el-form-item label="启用状态">
              <el-switch v-model="formModel.is_active" active-text="激活本版" inactive-text="暂存备用" />
            </el-form-item>
            <el-form-item label="模板用途说明">
              <el-input
                v-model="formModel.description"
                type="textarea"
                :rows="4"
                placeholder="说明本版 Prompt 相比于其他版本做出了什么优化，便于追溯和对比"
              />
            </el-form-item>
          </el-col>

          <!-- 右栏：代码/Prompt 模板大编辑器 -->
          <el-col :span="16" style="padding-left: 20px;">
            <el-form-item label="Prompt 模板正文" required>
              <div class="template-editor-wrapper">
                <div class="editor-header">
                  <span class="placeholder-tip">
                    支持占位符: <code v-for="p in placeholdersMap[formModel.stage]" :key="p" class="code-ph">{{ p }} </code>
                  </span>
                </div>
                <el-input
                  v-model="formModel.content_template"
                  type="textarea"
                  :rows="20"
                  class="code-textarea"
                  placeholder="请输入系统提示词内容模板..."
                />
              </div>
            </el-form-item>
          </el-col>
        </el-row>
      </el-form>

      <template #footer>
        <div class="dialog-footer">
          <el-button @click="dialogVisible = false">取消</el-button>
          <el-button type="primary" @click="submitForm">保存</el-button>
        </div>
      </template>
    </el-dialog>

    <!-- 测试触发 Dialog（KBD 阶段 Prompt：重新识图 / 重新分类 / 重新提交 关键信号） -->
    <el-dialog
      v-model="testDialogVisible"
      title="测试触发"
      width="460px"
    >
      <p style="color: #606266; margin: 0 0 12px">
        用当前 Prompt（<strong>{{ testTarget?.name }}</strong>）对指定 KBD 条目重新触发，
        立即验证效果（与 KBD 管理页的「重新识图 / 重新分类 / 重新提交」完全一致）。
      </p>
      <el-form label-width="96px">
        <el-form-item label="KBD 条目 ID" required>
          <el-input v-model="testKbdId" placeholder="如 123（在 KBD 管理页复制条目 ID）" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="testDialogVisible = false">取消</el-button>
        <el-button type="warning" :loading="testLoading" @click="submitTestTrigger">确认触发</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.prompt-manage-container {
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

.stage-sidebar {
  background: white;
  border-radius: 4px;
  padding: 10px 0;
  border: 1px solid #e4e7ed;
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.stage-item {
  display: flex;
  align-items: center;
  padding: 12px 20px;
  cursor: pointer;
  transition: all 0.3s ease;
  border-left: 4px solid transparent;
}

.stage-item:hover {
  background: #f8fafc;
  transform: translateX(3px);
}

.stage-item.active {
  background: #eef6ff;
  border-left-color: #3498db;
}

.stage-badge {
  font-family: Consolas, monospace;
  font-size: 11px;
  font-weight: bold;
  padding: 3px 6px;
  border-radius: 4px;
  margin-right: 12px;
  min-width: 42px;
  text-align: center;
}

.badge-base { background: #95a5a6; color: white; }
.badge-s0 { background: #3498db; color: white; }
.badge-s1 { background: #2ecc71; color: white; }
.badge-s2 { background: #9b59b6; color: white; }
.badge-s3 { background: #e67e22; color: white; }
.badge-s4 { background: #e74c3c; color: white; }
.badge-s5 { background: #f1c40f; color: #34495e; }
.badge-kbd { background: #1abc9c; color: white; }

.stage-label {
  font-size: 14px;
  font-weight: 500;
  color: #34495e;
}

.stage-item.active .stage-label {
  color: #2980b9;
  font-weight: 600;
}

.main-card {
  background: #fff;
  min-height: calc(100vh - 170px);
  display: flex;
  flex-direction: column;
}

.main-card :deep(.el-card__body) {
  flex: 1;
  display: flex;
  flex-direction: column;
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

.card-title {
  font-size: 18px;
  font-weight: 600;
  color: #2c3e50;
}

.card-sub {
  font-size: 13px;
  color: #7f8c8d;
  margin-top: 4px;
}

.placeholder-tip-box {
  background-color: #f7f9fa;
  border-left: 4px solid #95a5a6;
  padding: 15px 20px;
  border-radius: 0 4px 4px 0;
  margin-bottom: 25px;
}

.tip-title {
  font-size: 13px;
  font-weight: bold;
  color: #555;
  display: flex;
  align-items: center;
  gap: 5px;
}

.placeholders-tags {
  margin: 10px 0;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.ph-tag {
  font-family: Consolas, monospace;
}

.tip-desc {
  font-size: 12px;
  color: #7f8c8d;
  display: block;
}

.prompt-card-list {
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.prompt-card {
  border: 1px solid #e2e8f0;
  border-radius: 4px;
  padding: 20px;
  transition: all 0.3s ease;
  position: relative;
}

.prompt-card:hover {
  box-shadow: 0 4px 15px rgba(0, 0, 0, 0.05);
}

.prompt-card.active {
  border-color: #3498db;
  background-color: #f9fbfd;
}

.prompt-card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
}

.p-header-left {
  display: flex;
  align-items: center;
  gap: 10px;
}

.p-name {
  font-size: 16px;
  font-weight: bold;
  color: #2c3e50;
}

.version-tag {
  font-family: Consolas, monospace;
}

.p-header-right {
  display: flex;
  align-items: center;
  gap: 10px;
}

.active-label {
  font-size: 12px;
  color: #7f8c8d;
}

.prompt-card.active .active-label {
  color: #2ecc71;
  font-weight: bold;
}

.prompt-description {
  font-size: 13px;
  color: #555;
  margin-bottom: 12px;
  background: #f1f5f9;
  padding: 8px 12px;
  border-radius: 4px;
}

.prompt-template-preview {
  background-color: #1e293b;
  border-radius: 4px;
  padding: 15px;
  margin-bottom: 15px;
  max-height: 500px;
  overflow-y: auto;
}

.template-pre {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-all;
}

.template-pre code {
  color: #e2e8f0;
  font-family: Consolas, Monaco, monospace;
  font-size: 13px;
  line-height: 1.5;
}

.prompt-card-actions {
  display: flex;
  gap: 10px;
  justify-content: flex-end;
}

.template-editor-wrapper {
  border: 1px solid #dcdfe6;
  border-radius: 4px;
  overflow: hidden;
  width: 100%;
}

.editor-header {
  background-color: #f5f7fa;
  border-bottom: 1px solid #dcdfe6;
  padding: 8px 15px;
}

.placeholder-tip {
  font-size: 12px;
  color: #606266;
}

.code-ph {
  background: #eef1f6;
  color: #e83e8c;
  padding: 2px 4px;
  border-radius: 3px;
  font-family: Consolas, monospace;
  margin-right: 5px;
}

.code-textarea :deep(.el-textarea__inner) {
  font-family: Consolas, Monaco, monospace;
  font-size: 13px;
  border: none;
  border-radius: 0;
  background-color: #fafbfc;
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
  width: 100%;
}

.dialog-footer {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
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
</style>
