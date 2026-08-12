<script setup lang="ts">
import { ref, reactive, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  Collection,
  Document,
  Download,
  Upload,
  Check,
  Plus,
  Delete,
  Search,
  MagicStick,
  CircleCheck,
  CircleClose,
  Tickets,
  Setting,
  Finished
} from '@element-plus/icons-vue'
import {
  getCatalogList,
  getCatalogDetail,
  validateCatalogContent,
  updateCatalogContent,
  type CatalogMeta,
  type CatalogDetail
} from '@/api/catalog'

// ──────────────────────────────────────────────────────────────────────────────
// 状态定义
// ──────────────────────────────────────────────────────────────────────────────
const loading = ref(false)
const saving = ref(false)
const catalogs = ref<CatalogMeta[]>([])
const activeCatalogName = ref<string>('acli_command_catalog.json')

// 当前激活的视图模式: 'visual' (结构化卡片/表格) | 'code' (JSON 代码源码)
const viewMode = ref<'visual' | 'code'>('visual')

// 编辑器数据状态
const currentDetail = ref<CatalogDetail | null>(null)
const jsonCodeText = ref<string>('')
const jsonSyntaxValid = ref<boolean>(true)
const syntaxErrorMessage = ref<string>('')

// aCLI 命令目录列表搜索
const commandSearchQuery = ref<string>('')

// 结构化编辑状态：aCLI 命令列表
interface AcliCommandRow {
  command: string
  description?: string
}
const acliCommandsList = ref<AcliCommandRow[]>([])

// 结构化编辑状态：resolution_catalog.json 内容
interface ResolutionCatalogStructure {
  schema_version: number
  catalog_version: string
  log_aliases: Record<string, string>
  domain_command_requirements: Array<{
    domain: string
    path: string[]
    required_options: string[]
  }>
  qkv_actions: Array<{
    action_id: string
    query: string
    canonical_keywords: string[]
    aliases: string[]
    negative_aliases?: string[]
  }>
}
const resolutionData = reactive<ResolutionCatalogStructure>({
  schema_version: 1,
  catalog_version: '1.0.0',
  log_aliases: {},
  domain_command_requirements: [],
  qkv_actions: []
})

// 别名编辑临时表格数据
const logAliasRows = ref<Array<{ key: string; value: string }>>([])

// 新增 aCLI 命令对话框
const addCommandDialogVisible = ref(false)
const newCommandText = ref('')

// 新增 Log 别名对话框
const addAliasDialogVisible = ref(false)
const newAliasKey = ref('')
const newAliasValue = ref('')

// 新增 Domain 要求对话框
const addDomainReqDialogVisible = ref(false)
const newDomainForm = reactive({
  domain: 'vm',
  pathStr: 'config get',
  optionsStr: '--vm-id, -v'
})

// 新增 QKV Action 对话框
const addQkvDialogVisible = ref(false)
const newQkvForm = reactive({
  action_id: 'vm.power_on',
  query: 'task',
  canonical_keywords: '启动虚拟机',
  aliases: '开启虚拟机, 启动 vm',
  negative_aliases: '重启虚拟机'
})

// ──────────────────────────────────────────────────────────────────────────────
// 数据加载与初始化
// ──────────────────────────────────────────────────────────────────────────────
async function fetchCatalogList() {
  try {
    const list = await getCatalogList()
    catalogs.value = list
  } catch (err: any) {
    ElMessage.error(`获取 Catalog 列表失败: ${err.message}`)
  }
}

async function loadCatalog(filename: string) {
  loading.value = true
  try {
    const detail = await getCatalogDetail(filename)
    currentDetail.value = detail
    jsonCodeText.value = detail.content_text

    // 解析填充结构化编辑状态
    parseContentToStructure(filename, detail.content_json)
    validateCodeJsonSilent(jsonCodeText.value)
  } catch (err: any) {
    ElMessage.error(`加载 Catalog [${filename}] 失败: ${err.message}`)
  } finally {
    loading.value = false
  }
}

function parseContentToStructure(filename: string, jsonObj: any) {
  if (filename === 'acli_command_catalog.json') {
    const rawCmds = jsonObj?.commands || []
    acliCommandsList.value = rawCmds.map((item: any) => {
      if (typeof item === 'string') return { command: item }
      return { command: item.command || '', description: item.description || '' }
    })
  } else if (filename === 'resolution_catalog.json') {
    resolutionData.schema_version = jsonObj?.schema_version || 1
    resolutionData.catalog_version = jsonObj?.catalog_version || '1.0.0'
    resolutionData.log_aliases = jsonObj?.log_aliases || {}
    resolutionData.domain_command_requirements = jsonObj?.domain_command_requirements || []
    resolutionData.qkv_actions = jsonObj?.qkv_actions || []

    logAliasRows.value = Object.entries(resolutionData.log_aliases).map(([k, v]) => ({
      key: k,
      value: v
    }))
  }
}

/** 结构化数据同步回 JSON 源码 */
function syncStructureToCode() {
  if (activeCatalogName.value === 'acli_command_catalog.json') {
    const payload = {
      schema_version: 1,
      catalog_version: currentDetail.value?.content_json?.catalog_version || '2026-08-07.1',
      commands: acliCommandsList.value.map(row => {
        if (row.description) return { command: row.command, description: row.description }
        return { command: row.command }
      })
    }
    jsonCodeText.value = JSON.stringify(payload, null, 2)
  } else if (activeCatalogName.value === 'resolution_catalog.json') {
    const aliasesObj: Record<string, string> = {}
    logAliasRows.value.forEach(row => {
      if (row.key.trim()) {
        aliasesObj[row.key.trim()] = row.value.trim()
      }
    })

    const payload = {
      schema_version: resolutionData.schema_version,
      catalog_version: resolutionData.catalog_version,
      log_aliases: aliasesObj,
      domain_command_requirements: resolutionData.domain_command_requirements,
      qkv_actions: resolutionData.qkv_actions
    }
    jsonCodeText.value = JSON.stringify(payload, null, 2)
  }
  validateCodeJsonSilent(jsonCodeText.value)
}

/** 结构化变动时自动提交保存写回配置文件 */
async function saveStructureToBackend(customSuccessMsg?: string) {
  syncStructureToCode()
  if (!jsonSyntaxValid.value) {
    ElMessage.error(`自动保存失败：当前 JSON 语法错误（${syntaxErrorMessage.value}）`)
    return
  }
  saving.value = true
  try {
    const res = await updateCatalogContent(activeCatalogName.value, jsonCodeText.value)
    ElMessage.success(customSuccessMsg || `已自动保存写入配置文件并完成热重载！`)
    jsonCodeText.value = res.content_text
    currentDetail.value = {
      meta: res.meta,
      content_text: res.content_text,
      content_json: JSON.parse(res.content_text)
    }
    fetchCatalogList()
  } catch (err: any) {
    ElMessage.error(`保存失败: ${err.message}`)
  } finally {
    saving.value = false
  }
}

function handleTabClick(tabName: string) {
  activeCatalogName.value = tabName
  loadCatalog(tabName)
}

// ──────────────────────────────────────────────────────────────────────────────
// 计算属性与过滤
// ──────────────────────────────────────────────────────────────────────────────
const filteredAcliCommands = computed(() => {
  if (!commandSearchQuery.value.trim()) return acliCommandsList.value
  const q = commandSearchQuery.value.toLowerCase().trim()
  return acliCommandsList.value.filter(
    item => item.command.toLowerCase().includes(q) || (item.description && item.description.toLowerCase().includes(q))
  )
})

// ──────────────────────────────────────────────────────────────────────────────
// JSON 校验与格式化
// ──────────────────────────────────────────────────────────────────────────────
function validateCodeJsonSilent(text: string) {
  try {
    JSON.parse(text)
    jsonSyntaxValid.value = true
    syntaxErrorMessage.value = ''
  } catch (err: any) {
    jsonSyntaxValid.value = false
    syntaxErrorMessage.value = err.message
  }
}

function handleCodeInput() {
  validateCodeJsonSilent(jsonCodeText.value)
  if (jsonSyntaxValid.value) {
    try {
      const parsed = JSON.parse(jsonCodeText.value)
      parseContentToStructure(activeCatalogName.value, parsed)
    } catch (_) {}
  }
}

function formatJsonCode() {
  try {
    const parsed = JSON.parse(jsonCodeText.value)
    jsonCodeText.value = JSON.stringify(parsed, null, 2)
    jsonSyntaxValid.value = true
    syntaxErrorMessage.value = ''
    ElMessage.success('JSON 格式化美化完成')
  } catch (err: any) {
    ElMessage.error(`格式化失败：JSON 语法存在错误 - ${err.message}`)
  }
}

async function handleValidateBtn() {
  try {
    const res = await validateCatalogContent(activeCatalogName.value, jsonCodeText.value)
    if (res.valid) {
      ElMessage.success(res.message)
    } else {
      ElMessage.error(`校验未通过：${res.message}`)
    }
  } catch (err: any) {
    ElMessage.error(`校验错误: ${err.message}`)
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// 代码模式手动保存 & 导入导出
// ──────────────────────────────────────────────────────────────────────────────
async function handleSaveCatalog() {
  validateCodeJsonSilent(jsonCodeText.value)
  if (!jsonSyntaxValid.value) {
    ElMessage.error(`保存失败：当前 JSON 语法错误（${syntaxErrorMessage.value}）`)
    return
  }

  try {
    await ElMessageBox.confirm(
      `确定要保存并更新 [${activeCatalogName.value}] 吗？保存后后端将自动重载该配置并实时生效。`,
      '确认在线保存',
      {
        confirmButtonText: '保存并热生效',
        cancelButtonText: '取消',
        type: 'warning'
      }
    )

    await saveStructureToBackend('保存成功！后端 Shared Resolution Runtime 已感知变更自动热加载生效')
  } catch (err: any) {
    if (err !== 'cancel') {
      ElMessage.error(`保存失败: ${err.message}`)
    }
  }
}

function exportCatalogJson() {
  if (!jsonCodeText.value) return
  const blob = new Blob([jsonCodeText.value], { type: 'application/json;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = activeCatalogName.value
  document.body.appendChild(link)
  link.click()
  document.body.removeChild(link)
  URL.revokeObjectURL(url)
  ElMessage.success(`已导出 ${activeCatalogName.value}`)
}

function handleFileImport(uploadFile: any) {
  const file = uploadFile.raw
  if (!file) return
  const reader = new FileReader()
  reader.onload = async (e) => {
    try {
      const text = e.target?.result as string
      const parsed = JSON.parse(text)
      jsonCodeText.value = JSON.stringify(parsed, null, 2)
      parseContentToStructure(activeCatalogName.value, parsed)
      validateCodeJsonSilent(jsonCodeText.value)
      await saveStructureToBackend(`成功导入文件 [${file.name}] 并保存写入配置文件！`)
    } catch (err: any) {
      ElMessage.error(`解析导入文件失败：不是合法 JSON - ${err.message}`)
    }
  }
  reader.readAsText(file)
}

// ──────────────────────────────────────────────────────────────────────────────
// 结构化项增删逻辑（含二次弹框确认防误触）
// ──────────────────────────────────────────────────────────────────────────────
async function handleAddAcliCommand() {
  if (!newCommandText.value.trim()) return
  const cmd = newCommandText.value.trim()
  acliCommandsList.value.unshift({ command: cmd })
  newCommandText.value = ''
  addCommandDialogVisible.value = false
  await saveStructureToBackend(`已添加命令 [${cmd}] 并写入配置文件热生效！`)
}

async function removeAcliCommand(index: number) {
  const row = acliCommandsList.value[index]
  if (!row) return
  try {
    await ElMessageBox.confirm(
      `确定要删除 aCLI 命令 [${row.command}] 吗？`,
      '确认删除',
      {
        confirmButtonText: '确定删除',
        cancelButtonText: '取消',
        type: 'warning'
      }
    )
    acliCommandsList.value.splice(index, 1)
    await saveStructureToBackend(`已删除命令 [${row.command}] 并写入配置文件！`)
  } catch (_) {}
}

async function handleAddAlias() {
  if (!newAliasKey.value.trim() || !newAliasValue.value.trim()) return
  logAliasRows.value.unshift({
    key: newAliasKey.value.trim(),
    value: newAliasValue.value.trim()
  })
  newAliasKey.value = ''
  newAliasValue.value = ''
  addAliasDialogVisible.value = false
  await saveStructureToBackend('已添加 Log 别名并保存热生效！')
}

async function removeLogAlias(index: number) {
  const row = logAliasRows.value[index]
  try {
    await ElMessageBox.confirm(
      `确定要删除 Log 别名 [${row?.key || '此项'}] 吗？`,
      '确认删除',
      {
        confirmButtonText: '确定删除',
        cancelButtonText: '取消',
        type: 'warning'
      }
    )
    logAliasRows.value.splice(index, 1)
    await saveStructureToBackend('已删除 Log 别名并保存热生效！')
  } catch (_) {}
}

async function handleAddDomainReq() {
  const pathArr = newDomainForm.pathStr.trim().split(/\s+/)
  const optionsArr = newDomainForm.optionsStr.split(',').map(s => s.trim()).filter(Boolean)
  resolutionData.domain_command_requirements.unshift({
    domain: newDomainForm.domain.trim(),
    path: pathArr,
    required_options: optionsArr
  })
  addDomainReqDialogVisible.value = false
  await saveStructureToBackend('已添加 Domain 参数契约并保存热生效！')
}

async function removeDomainReq(index: number) {
  const row = resolutionData.domain_command_requirements[index]
  try {
    await ElMessageBox.confirm(
      `确定要删除 Domain [${row?.domain}] 命令契约配置吗？`,
      '确认删除',
      {
        confirmButtonText: '确定删除',
        cancelButtonText: '取消',
        type: 'warning'
      }
    )
    resolutionData.domain_command_requirements.splice(index, 1)
    await saveStructureToBackend('已删除 Domain 参数契约并保存热生效！')
  } catch (_) {}
}

async function handleAddQkvAction() {
  const canonicalArr = newQkvForm.canonical_keywords.split(',').map(s => s.trim()).filter(Boolean)
  const aliasArr = newQkvForm.aliases.split(',').map(s => s.trim()).filter(Boolean)
  const negArr = newQkvForm.negative_aliases.split(',').map(s => s.trim()).filter(Boolean)

  resolutionData.qkv_actions.unshift({
    action_id: newQkvForm.action_id.trim(),
    query: newQkvForm.query.trim(),
    canonical_keywords: canonicalArr,
    aliases: aliasArr,
    negative_aliases: negArr.length ? negArr : undefined
  })
  addQkvDialogVisible.value = false
  await saveStructureToBackend('已添加 QKV Action 映射并保存热生效！')
}

async function removeQkvAction(index: number) {
  const row = resolutionData.qkv_actions[index]
  try {
    await ElMessageBox.confirm(
      `确定要删除 QKV Action [${row?.action_id}] 映射配置吗？`,
      '确认删除',
      {
        confirmButtonText: '确定删除',
        cancelButtonText: '取消',
        type: 'warning'
      }
    )
    resolutionData.qkv_actions.splice(index, 1)
    await saveStructureToBackend('已删除 QKV Action 映射并保存热生效！')
  } catch (_) {}
}

onMounted(async () => {
  await fetchCatalogList()
  loadCatalog(activeCatalogName.value)
})
</script>

<template>
  <div class="catalog-manage-container">
    <!-- 头部卡片 -->
    <el-card class="header-card" shadow="never">
      <div class="header-content">
        <div class="header-left">
          <div class="header-title">
            <el-icon class="header-icon"><Collection /></el-icon>
            <h2>Catalog 基线与关键信号规则</h2>
          </div>
          <p class="header-desc">
            统一管理 Shared Resolution Runtime 的命令快照与审查目录。支持结构化卡片与 JSON 代码模式在线编辑，结构化添加与删除动作将**自动写回配置文件并实时热重载**。
          </p>
        </div>
        <div class="header-actions">
          <el-upload
            action="#"
            :auto-upload="false"
            :show-file-list="false"
            :on-change="handleFileImport"
            accept=".json"
          >
            <el-button :icon="Upload">导入 JSON</el-button>
          </el-upload>
          <el-button :icon="Download" @click="exportCatalogJson">导出 JSON</el-button>
          <!-- 仅在 JSON 源码编辑器模式下展示【保存并热生效】提交按钮 -->
          <el-button
            v-if="viewMode === 'code'"
            type="primary"
            :icon="Check"
            :loading="saving"
            @click="handleSaveCatalog"
          >
            保存并热生效
          </el-button>
        </div>
      </div>
    </el-card>

    <!-- Catalog 切换 Tabs 与 工具栏 -->
    <div class="catalog-toolbar">
      <div class="tabs-wrapper">
        <el-radio-group v-model="activeCatalogName" size="large" @change="handleTabClick">
          <el-radio-button value="acli_command_catalog.json">
            <el-icon><Finished /></el-icon> aCLI 命令目录 (336+条)
          </el-radio-button>
          <el-radio-button value="resolution_catalog.json">
            <el-icon><Setting /></el-icon> Runtime 规则与别名 Catalog
          </el-radio-button>
        </el-radio-group>
      </div>

      <div class="mode-switch">
        <el-radio-group v-model="viewMode" size="default">
          <el-radio-button value="visual">
            <el-icon><Tickets /></el-icon> 结构化卡片视图
          </el-radio-button>
          <el-radio-button value="code">
            <el-icon><Document /></el-icon> JSON 源码编辑器
          </el-radio-button>
        </el-radio-group>

        <template v-if="viewMode === 'code'">
          <el-button size="default" :icon="MagicStick" @click="formatJsonCode">美化格式</el-button>
          <el-button size="default" :icon="Check" @click="handleValidateBtn">语法校验</el-button>
        </template>
      </div>
    </div>

    <!-- 主要内容区 -->
    <div v-loading="loading" class="main-body">

      <!-- TAB 1: acli_command_catalog.json 可视化 -->
      <template v-if="activeCatalogName === 'acli_command_catalog.json'">
        <div v-if="viewMode === 'visual'" class="acli-visual-container">
          <el-card shadow="never" class="table-card">
            <template #header>
              <div class="card-header">
                <div class="search-box">
                  <el-input
                    v-model="commandSearchQuery"
                    placeholder="输入关键词搜索 aCLI 命令路径 (如: system ps, vm config)..."
                    :prefix-icon="Search"
                    clearable
                    style="width: 400px"
                  />
                  <span class="count-tag">
                    共 {{ acliCommandsList.length }} 条命令规则 (匹配 {{ filteredAcliCommands.length }} 条)
                  </span>
                </div>
                <el-button type="primary" :icon="Plus" @click="addCommandDialogVisible = true">
                  添加 aCLI 命令
                </el-button>
              </div>
            </template>

            <el-table :data="filteredAcliCommands" height="600" stripe style="width: 100%">
              <el-table-column type="index" label="#" width="60" align="center" />
              <el-table-column prop="command" label="aCLI 命令路径" min-width="320">
                <template #default="{ row }">
                  <span class="cmd-code">{{ row.command }}</span>
                </template>
              </el-table-column>
              <el-table-column prop="description" label="说明 / 用途描述" min-width="300">
                <template #default="{ row }">
                  <el-input
                    v-model="row.description"
                    size="small"
                    placeholder="可选说明 (修改自动保存)"
                    @change="() => saveStructureToBackend('修改描述已保存')"
                  />
                </template>
              </el-table-column>
              <el-table-column label="操作" width="100" align="center">
                <template #default="{ $index }">
                  <el-button
                    type="danger"
                    link
                    :icon="Delete"
                    @click="removeAcliCommand($index)"
                  >
                    删除
                  </el-button>
                </template>
              </el-table-column>
            </el-table>
          </el-card>
        </div>
      </template>

      <!-- TAB 2: resolution_catalog.json 可视化 -->
      <template v-if="activeCatalogName === 'resolution_catalog.json'">
        <div v-if="viewMode === 'visual'" class="resolution-visual-container">
          <!-- 区域 1: Log 日志别名映射表 -->
          <el-card shadow="never" class="res-card">
            <template #header>
              <div class="card-header">
                <div>
                  <h3>Log 模块文件别名 (log_aliases)</h3>
                  <p class="sub-text">将误写或非标准的日志名称映射为标准的 sfvt_*.log 系统文件名</p>
                </div>
                <el-button type="primary" size="small" :icon="Plus" @click="addAliasDialogVisible = true">
                  添加 Log 别名
                </el-button>
              </div>
            </template>

            <el-table :data="logAliasRows" size="small" stripe style="width: 100%">
              <el-table-column label="原始/误写名称 (Alias Key)" min-width="250">
                <template #default="{ row }">
                  <el-input v-model="row.key" size="small" @change="() => saveStructureToBackend('更新 Log 别名已保存')" />
                </template>
              </el-table-column>
              <el-table-column label="标准日志文件名 (Canonical Value)" min-width="250">
                <template #default="{ row }">
                  <el-input v-model="row.value" size="small" @change="() => saveStructureToBackend('更新 Log 别名已保存')" />
                </template>
              </el-table-column>
              <el-table-column label="操作" width="80" align="center">
                <template #default="{ $index }">
                  <el-button type="danger" link size="small" :icon="Delete" @click="removeLogAlias($index)" />
                </template>
              </el-table-column>
            </el-table>
          </el-card>

          <!-- 区域 2: Domain 命令必填选项依赖 -->
          <el-card shadow="never" class="res-card">
            <template #header>
              <div class="card-header">
                <div>
                  <h3>Domain 领域命令契约依赖 (domain_command_requirements)</h3>
                  <p class="sub-text">规范特定领域命令（如 vm config get）执行时必须包含的选项参数</p>
                </div>
                <el-button type="primary" size="small" :icon="Plus" @click="addDomainReqDialogVisible = true">
                  添加契约规则
                </el-button>
              </div>
            </template>

            <el-table :data="resolutionData.domain_command_requirements" size="small" stripe style="width: 100%">
              <el-table-column prop="domain" label="领域 (Domain)" width="120">
                <template #default="{ row }">
                  <el-tag size="small" type="info">{{ row.domain }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="命令子路径 (Path)" min-width="200">
                <template #default="{ row }">
                  <span class="cmd-code">{{ row.path ? row.path.join(' ') : '' }}</span>
                </template>
              </el-table-column>
              <el-table-column label="必须包含的参数之一 (Required Options)" min-width="250">
                <template #default="{ row }">
                  <el-tag v-for="opt in row.required_options" :key="opt" size="small" class="opt-tag">
                    {{ opt }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="操作" width="80" align="center">
                <template #default="{ $index }">
                  <el-button type="danger" link size="small" :icon="Delete" @click="removeDomainReq($index)" />
                </template>
              </el-table-column>
            </el-table>
          </el-card>

          <!-- 区域 3: QKV Action 同义词关联 -->
          <el-card shadow="never" class="res-card">
            <template #header>
              <div class="card-header">
                <div>
                  <h3>QKV Action 同义词与动作关联 (qkv_actions)</h3>
                  <p class="sub-text">将自然语言或告警关键字映射到标准的全局 Action ID</p>
                </div>
                <el-button type="primary" size="small" :icon="Plus" @click="addQkvDialogVisible = true">
                  添加 QKV Action
                </el-button>
              </div>
            </template>

            <el-table :data="resolutionData.qkv_actions" size="small" stripe style="width: 100%">
              <el-table-column prop="action_id" label="Action ID" width="180">
                <template #default="{ row }">
                  <strong>{{ row.action_id }}</strong>
                </template>
              </el-table-column>
              <el-table-column prop="query" label="类型" width="100">
                <template #default="{ row }">
                  <el-tag size="small" type="success">{{ row.query }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="标准关键词" min-width="160">
                <template #default="{ row }">
                  <el-tag v-for="kw in row.canonical_keywords" :key="kw" size="small" type="warning" class="opt-tag">
                    {{ kw }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="支持的同义词 (Aliases)" min-width="220">
                <template #default="{ row }">
                  <el-tag v-for="al in row.aliases" :key="al" size="small" class="opt-tag">
                    {{ al }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="排除/负向词 (Negative)" min-width="160">
                <template #default="{ row }">
                  <el-tag v-for="neg in row.negative_aliases || []" :key="neg" size="small" type="danger" class="opt-tag">
                    {{ neg }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="操作" width="80" align="center">
                <template #default="{ $index }">
                  <el-button type="danger" link size="small" :icon="Delete" @click="removeQkvAction($index)" />
                </template>
              </el-table-column>
            </el-table>
          </el-card>
        </div>
      </template>

      <!-- JSON 源码编辑器模式 -->
      <div v-if="viewMode === 'code'" class="code-editor-container">
        <el-card shadow="never" class="editor-card">
          <template #header>
            <div class="editor-header">
              <div class="status-indicator">
                <el-icon v-if="jsonSyntaxValid" color="#67c23a" size="18"><CircleCheck /></el-icon>
                <el-icon v-else color="#f56c6c" size="18"><CircleClose /></el-icon>
                <span :class="jsonSyntaxValid ? 'valid-text' : 'invalid-text'">
                  {{ jsonSyntaxValid ? 'JSON 语法正常' : `语法错误: ${syntaxErrorMessage}` }}
                </span>
              </div>
              <div class="meta-info">
                <span>文件路径: shared/resolution/catalogs/{{ activeCatalogName }}</span>
              </div>
            </div>
          </template>

          <el-input
            v-model="jsonCodeText"
            type="textarea"
            :rows="24"
            resize="vertical"
            spellcheck="false"
            class="json-textarea"
            @input="handleCodeInput"
          />
        </el-card>
      </div>

    </div>

    <!-- 新增 aCLI 命令对话框 -->
    <el-dialog v-model="addCommandDialogVisible" title="新增 aCLI 命令路径" width="500px">
      <el-form label-position="top">
        <el-form-item label="完整 aCLI 命令路径 (如: acli system new_cmd)">
          <el-input v-model="newCommandText" placeholder="acli system ..." />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="addCommandDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="handleAddAcliCommand">确定添加并保存生效</el-button>
      </template>
    </el-dialog>

    <!-- 新增 Log 别名对话框 -->
    <el-dialog v-model="addAliasDialogVisible" title="新增 Log 文件名别名" width="500px">
      <el-form label-position="top">
        <el-form-item label="原始/误写文件名 (Alias Key)">
          <el-input v-model="newAliasKey" placeholder="例如: vtpdeamon" />
        </el-form-item>
        <el-form-item label="标准日志文件名 (Canonical Value)">
          <el-input v-model="newAliasValue" placeholder="例如: sfvt_vtpdaemon.log" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="addAliasDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="handleAddAlias">确定添加并保存生效</el-button>
      </template>
    </el-dialog>

    <!-- 新增 Domain 契约对话框 -->
    <el-dialog v-model="addDomainReqDialogVisible" title="新增 Domain 命令参数契约" width="500px">
      <el-form label-position="top">
        <el-form-item label="技术域 (Domain)">
          <el-input v-model="newDomainForm.domain" placeholder="vm / network / storage 等" />
        </el-form-item>
        <el-form-item label="命令路径 (Path 空格分隔)">
          <el-input v-model="newDomainForm.pathStr" placeholder="例如: config get" />
        </el-form-item>
        <el-form-item label="必须包含的参数之一 (逗号分隔)">
          <el-input v-model="newDomainForm.optionsStr" placeholder="例如: --vm-id, -v" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="addDomainReqDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="handleAddDomainReq">确定添加并保存生效</el-button>
      </template>
    </el-dialog>

    <!-- 新增 QKV Action 对话框 -->
    <el-dialog v-model="addQkvDialogVisible" title="新增 QKV Action 映射" width="520px">
      <el-form label-position="top">
        <el-form-item label="Action ID">
          <el-input v-model="newQkvForm.action_id" placeholder="例如: vm.power_on" />
        </el-form-item>
        <el-form-item label="查询类型 (Query Type)">
          <el-input v-model="newQkvForm.query" placeholder="task / alert / dialog" />
        </el-form-item>
        <el-form-item label="标准关键词 (逗号分隔)">
          <el-input v-model="newQkvForm.canonical_keywords" placeholder="例如: 启动虚拟机" />
        </el-form-item>
        <el-form-item label="支持的同义词/别名 (逗号分隔)">
          <el-input v-model="newQkvForm.aliases" placeholder="例如: 开启虚拟机, 启动 vm" />
        </el-form-item>
        <el-form-item label="排除/负向词 (可选，逗号分隔)">
          <el-input v-model="newQkvForm.negative_aliases" placeholder="例如: 重启虚拟机" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="addQkvDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="handleAddQkvAction">确定添加并保存生效</el-button>
      </template>
    </el-dialog>

  </div>
</template>

<style scoped>
.catalog-manage-container {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.header-card {
  border-radius: 8px;
  background: #fff;
}

.header-content {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.header-title {
  display: flex;
  align-items: center;
  gap: 10px;
}

.header-icon {
  font-size: 24px;
  color: #409eff;
}

.header-title h2 {
  font-size: 18px;
  font-weight: 600;
  color: #303133;
}

.header-desc {
  margin-top: 6px;
  font-size: 13px;
  color: #606266;
  line-height: 1.5;
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}

.catalog-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  background: #fff;
  padding: 12px 16px;
  border-radius: 8px;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.05);
}

.mode-switch {
  display: flex;
  align-items: center;
  gap: 12px;
}

.main-body {
  min-height: 500px;
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.search-box {
  display: flex;
  align-items: center;
  gap: 16px;
}

.count-tag {
  font-size: 13px;
  color: #909399;
}

.cmd-code {
  background: #f4f4f5;
  color: #303133;
  padding: 3px 8px;
  border-radius: 4px;
  font-family: monospace;
  font-size: 13px;
}

.resolution-visual-container {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.res-card h3 {
  font-size: 15px;
  font-weight: 600;
  color: #303133;
}

.sub-text {
  font-size: 12px;
  color: #909399;
  margin-top: 4px;
}

.opt-tag {
  margin-right: 4px;
  margin-bottom: 4px;
}

.editor-card {
  border-radius: 8px;
}

.editor-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.status-indicator {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
}

.valid-text {
  color: #67c23a;
  font-weight: 500;
}

.invalid-text {
  color: #f56c6c;
  font-weight: 500;
}

.meta-info {
  font-size: 12px;
  color: #909399;
}

.json-textarea :deep(.el-textarea__inner) {
  font-family: Consolas, Monaco, 'Courier New', monospace;
  font-size: 13px;
  line-height: 1.5;
  background-color: #282c34;
  color: #abb2bf;
  border-radius: 6px;
  padding: 14px;
}
</style>
