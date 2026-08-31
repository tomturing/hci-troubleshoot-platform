<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Back, Check, EditPen, Plus, RefreshRight, View, Document, SetUp } from '@element-plus/icons-vue'
import { useRouter } from 'vue-router'

type AssetType = 'template' | 'instance'
type AssetStatus = 'draft' | 'published' | 'retired'
type EditMode = 'form' | 'json'

interface Asset {
  id: string
  asset_key: string
  asset_type: AssetType
  signal_type: 'qkv_alert' | 'qkv_task' | 'qkv_dialog'
  revision: number
  status: AssetStatus
  content: Record<string, unknown>
  template_asset_key?: string
  template_revision?: number
  category_baseline: Record<string, unknown>
  catalog_baseline: Record<string, unknown>
  content_digest: string
  created_by: string
  trace_id: string
  updated_at: string
}

interface CustomBinding {
  key: string
  value: string
}

const router = useRouter()
const endpoint = (import.meta.env.VITE_HCI_SIM_CONTROL_PLANE_URL || '/api/hci-sim').replace(/\/$/, '')
const loading = ref(false)
const saving = ref(false)
const assets = ref<Asset[]>([])
const signalFilter = ref('')
const typeFilter = ref('')
const statusFilter = ref('')
const selected = ref<Asset | null>(null)
const editVisible = ref(false)
const editMode = ref<EditMode>('form')
const jsonSyntaxError = ref<string | null>(null)

// 标准基线默认快照
const DEFAULT_CATEGORY_BASELINE = {
  source: 'backend/kb-service/config/category_baseline.yaml',
  revision: '1.0',
  checksum: 'sha256:4aaa1e4811c5347efe2f270b62eb9a58eb7c7453927ccf5a115af281ebe82b21',
}
const DEFAULT_CATALOG_BASELINE = {
  source: 'backend/shared/resolution/catalogs/resolution_catalog.json',
  revision: '2026-08-13.1',
  checksum: 'sha256:3a85084e74dd6911472e0717a988f9692e59cd279d2a2c80c05a164bd19d5612',
}

// 基础表单状态
const form = ref({
  asset_key: '',
  asset_type: 'template' as AssetType,
  signal_type: 'qkv_task' as 'qkv_alert' | 'qkv_task' | 'qkv_dialog',
  template_asset_key: '',
  template_revision: 1,
  content: '{}',
  category_baseline: JSON.stringify(DEFAULT_CATEGORY_BASELINE, null, 2),
  catalog_baseline: JSON.stringify(DEFAULT_CATALOG_BASELINE, null, 2),
})

// 结构化表单状态（针对实例与模板的可视化编辑）
const instanceSelection = ref({
  keyword: '',
  default: false,
})

// 预设结构化 bindings 字段（严格对齐 Tool Registry 的 10 个标准产出变量 produces）
const taskBindings = ref({
  TYPE: '删除虚拟机',
  DESCRIPTION: '创建回收站目录失败',
  PROCESS: '完成',
  ERRCODE_TRACING: 'null',
  REQUEST_ID: ',a3a9e0350ab8121dd7ac9fbbe66bea77',
  TARGET: 'Ubuntu-26.04_import_1',
  VM: '1114365066966',
  HOST: 'SVR_aCloud_668',
  HOSTID: 'host-047bcb4bc820',
  END: '2026-06-23 22:54:03',
})

const alertBindings = ref({
  ALERT_TYPE: '',
  DESCRIPTION: '',
  OBJECT_NAME: 'SVR_aCloud_668',
  OBJECT_TYPE: '集群',
  TARGET: 'SVR_aCloud_668',
  START: '',
  END: '',
  URGENT_TYPE: '紧急',
})

const dialogBindings = ref({
  DAY: '26',
  END_MS: '',
  END: '',
  CONTEXT_MS: '',
  CONTEXT: '',
  PID: '6955',
  TRACE_ROOT: 'a8e4524c9151ac0956995f05d1289081',
  TRACE_SPAN: 'd41339',
  TRACE_SEGMENT: '45e4a7',
  CONTEXT_SEGMENT: '231e62',
  ERRCODE: '0x0100186F',
  ERRCODE_TRACE: '0x0100186F/0x010015BE/0x01002D46',
  VM_NAME: 'Ubuntu-26.04_import_1',
  ERROR_MESSAGE: '',
})

// 模板编辑的 stdout_template 文本
const templateStdout = ref('')

// 动态自定义键值对
const customBindings = ref<CustomBinding[]>([])

// 常见关键字推荐标签
const recommendedKeywords = computed(() => {
  if (form.value.signal_type === 'qkv_task') {
    return ['删除虚拟机', '启动虚拟机', '关闭虚拟机', '创建虚拟机', '迁移虚拟机', '备份']
  }
  if (form.value.signal_type === 'qkv_alert') {
    return ['ha_out_of_resource', '磁盘空间不足', '网络链路异常', '内存过高告警']
  }
  return ['启动虚拟机', '删除虚拟机', '虚拟机镜像忙', '存储卷无法挂载']
})

// 常用模板占位符插入芯片（严格对齐 Tool Registry 标准产出变量）
const availablePlaceholders = computed(() => {
  if (form.value.signal_type === 'qkv_task') {
    return ['TYPE', 'DESCRIPTION', 'PROCESS', 'ERRCODE_TRACING', 'REQUEST_ID', 'TARGET', 'VM', 'HOST', 'HOSTID', 'END']
  }
  if (form.value.signal_type === 'qkv_alert') {
    return ['ALERT_TYPE', 'DESCRIPTION', 'OBJECT_NAME', 'OBJECT_TYPE', 'TARGET', 'START', 'END', 'URGENT_TYPE']
  }
  return ['DAY', 'END_MS', 'END', 'CONTEXT_MS', 'CONTEXT', 'PID', 'TRACE_ROOT', 'TRACE_SPAN', 'TRACE_SEGMENT', 'CONTEXT_SEGMENT', 'ERRCODE', 'ERRCODE_TRACE', 'VM_NAME', 'ERROR_MESSAGE']
})

// 可选的同信号模板
const availableTemplates = computed(() => {
  return assets.value.filter((a) => a.asset_type === 'template' && a.signal_type === form.value.signal_type)
})

function formatCurrentTime() {
  const now = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} ${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`
}

function fillCurrentTime() {
  const nowStr = formatCurrentTime()
  if (form.value.signal_type === 'qkv_task') {
    taskBindings.value.END = nowStr
  } else if (form.value.signal_type === 'qkv_alert') {
    alertBindings.value.START = nowStr
    alertBindings.value.END = nowStr
  }
  ElMessage.success('已填入当前时间')
}

function insertPlaceholder(name: string) {
  templateStdout.value += `{{${name}}}`
}

function assetSelection(asset: Asset | null): { keyword?: string; default?: boolean } {
  if (!asset || !asset.content || typeof asset.content.selection !== 'object' || asset.content.selection === null) {
    return {}
  }
  return asset.content.selection as { keyword?: string; default?: boolean }
}

// 预设高保真模板快速填入（qkv_task 严格保持 10 个变量插槽）
function loadPresetTemplate(version: 'v2_full' | 'v1_minimal') {
  if (form.value.signal_type === 'qkv_task') {
    if (version === 'v2_full') {
      templateStdout.value = '{"data":[{"action_type":1,"alert_type":"{{TYPE}}","bcancel":0,"description":"{{DESCRIPTION}}","dest_host":"","end":"{{END}}","errcode_tracing":{{ERRCODE_TRACING}},"event_id":1245196,"ha_handle_action":"","ha_handle_result":"","host":"{{HOST}}","hostid":"{{HOSTID}}","hostname":"{{HOST}}","id":566,"log_id":"host-047bcb4bc820:9408:1782226435:1568763286126","module_type":1,"object_id":"","object_name":"{{TARGET}}","object_type":"虚拟机","otype":"虚拟机","pid":"UPID:host-047bcb4bc820:000024C0:78F6A31:6A3A9E03:task:1114365066966:admin@vtp:","process":"{{PROCESS}}","request_id":"{{REQUEST_ID}}","reserved2":"","reserved3":"","risk_level":1,"sched_effect":"","sched_reason":"","start":"{{END}}","status":2,"sysloged":0,"target":"{{TARGET}}","type":"{{TYPE}}","upid":"","user":"admin (172.28.24.22)","vm":"{{VM}}"}]}'
      ElMessage.success('已载入 acli task 全字段高保真模板 (v2，含10个标准变量插槽)')
    } else {
      templateStdout.value = '{"data":[{"alert_type":"{{TYPE}}","description":"{{DESCRIPTION}}","process":"{{PROCESS}}","target":"{{TARGET}}","end":"{{END}}","errcode_tracing":{{ERRCODE_TRACING}}}]}'
      ElMessage.success('已载入轻量精简模板 (v1)')
    }
  } else if (form.value.signal_type === 'qkv_alert') {
    templateStdout.value = '{"data":[{"alert_type":"{{ALERT_TYPE}}","description":"{{DESCRIPTION}}","object_name":"{{OBJECT_NAME}}","object_type":"{{OBJECT_TYPE}}","target":"{{TARGET}}","start":"{{START}}","end":"{{END}}","urgent_type":"{{URGENT_TYPE}}"}]}'
    ElMessage.success('已载入 qkv_alert 标准模板')
  } else if (form.value.signal_type === 'qkv_dialog') {
    templateStdout.value = '/sf/log/{{DAY}}/vt/sfvt_vtpdaemon.log:{{END_MS}} err [sfvt_vtpdaemon] {{END}} E {{PID}} QemuServer.pm(VTP::QemuServer::vm_start_error_deal):12936 | [{{TRACE_ROOT}}:{{TRACE_SPAN}}:{{TRACE_SEGMENT}}] [my_die_with_errcode {{ERRCODE}}] message: {{KEYWORD}}（{{VM_NAME}}）失败，错误信息：{{ERROR_MESSAGE}}\n/sf/log/{{DAY}}/vt/sfvt_vtpdaemon.log:{{CONTEXT_MS}} warning [sfvt_vtpdaemon] {{CONTEXT}} W {{PID}} OpLog.pm((eval)):586 | [{{TRACE_ROOT}}:{{TRACE_SPAN}}:{{CONTEXT_SEGMENT}}] Errcode tracing: {{ERRCODE_TRACE}}, message: {{KEYWORD}}（{{VM_NAME}}）失败，错误信息：{{ERROR_MESSAGE}}'
    ElMessage.success('已载入 qkv_dialog 标准模板')
  }
}

function addCustomBinding() {
  customBindings.value.push({ key: '', value: '' })
}

function removeCustomBinding(index: number) {
  customBindings.value.splice(index, 1)
}

function loadDefaultBaselines() {
  form.value.category_baseline = JSON.stringify(DEFAULT_CATEGORY_BASELINE, null, 2)
  form.value.catalog_baseline = JSON.stringify(DEFAULT_CATALOG_BASELINE, null, 2)
  ElMessage.success('已恢复系统标准基线快照')
}

// 表单 -> Content 组装
function buildContentFromForm(): Record<string, unknown> {
  if (form.value.asset_type === 'template') {
    return { stdout_template: templateStdout.value }
  }
  const bindings: Record<string, string> = {}
  if (form.value.signal_type === 'qkv_task') {
    Object.entries(taskBindings.value).forEach(([k, v]) => {
      if (v !== '' && v !== undefined) bindings[k] = v
    })
    // TYPE 与 selection.keyword 默认保持一致
    if (!bindings.TYPE && instanceSelection.value.keyword) {
      bindings.TYPE = instanceSelection.value.keyword
    }
  } else if (form.value.signal_type === 'qkv_alert') {
    Object.entries(alertBindings.value).forEach(([k, v]) => {
      if (v !== '' && v !== undefined) bindings[k] = v
    })
  } else if (form.value.signal_type === 'qkv_dialog') {
    Object.entries(dialogBindings.value).forEach(([k, v]) => {
      if (v !== '' && v !== undefined) bindings[k] = v
    })
  }
  // 附加自定义 bindings
  customBindings.value.forEach((item) => {
    const k = item.key.trim().toUpperCase()
    if (k) bindings[k] = item.value
  })

  return {
    selection: {
      keyword: instanceSelection.value.keyword,
      default: instanceSelection.value.default,
    },
    bindings,
  }
}

// Content -> 表单反向解析
function populateFormFromContent(content: Record<string, unknown>) {
  if (form.value.asset_type === 'template') {
    templateStdout.value = typeof content.stdout_template === 'string' ? content.stdout_template : ''
  } else {
    const selection = (content.selection || {}) as { keyword?: string; default?: boolean }
    instanceSelection.value.keyword = selection.keyword || ''
    instanceSelection.value.default = !!selection.default

    const bindings = (content.bindings || {}) as Record<string, string>
    const knownKeys = new Set<string>()

    if (form.value.signal_type === 'qkv_task') {
      Object.keys(taskBindings.value).forEach((k) => {
        knownKeys.add(k)
        if (bindings[k] !== undefined) (taskBindings.value as Record<string, string>)[k] = String(bindings[k])
      })
    } else if (form.value.signal_type === 'qkv_alert') {
      Object.keys(alertBindings.value).forEach((k) => {
        knownKeys.add(k)
        if (bindings[k] !== undefined) (alertBindings.value as Record<string, string>)[k] = String(bindings[k])
      })
    } else if (form.value.signal_type === 'qkv_dialog') {
      Object.keys(dialogBindings.value).forEach((k) => {
        knownKeys.add(k)
        if (bindings[k] !== undefined) (dialogBindings.value as Record<string, string>)[k] = String(bindings[k])
      })
    }

    // 收集剩余自定义 bindings
    customBindings.value = []
    Object.entries(bindings).forEach(([k, v]) => {
      if (!knownKeys.has(k)) {
        customBindings.value.push({ key: k, value: String(v) })
      }
    })
  }
}

// 模式切换
function switchEditMode(targetMode: EditMode) {
  if (targetMode === 'json') {
    // 从表单切换至 JSON
    const contentObj = buildContentFromForm()
    form.value.content = JSON.stringify(contentObj, null, 2)
    jsonSyntaxError.value = null
    editMode.value = 'json'
  } else {
    // 从 JSON 切换至表单
    try {
      const parsed = JSON.parse(form.value.content)
      if (typeof parsed !== 'object' || parsed === null) throw new Error('内容必须是 JSON 对象')
      populateFormFromContent(parsed)
      jsonSyntaxError.value = null
      editMode.value = 'form'
    } catch (err) {
      jsonSyntaxError.value = err instanceof Error ? err.message : 'JSON 格式错误'
      ElMessage.warning('当前 JSON 格式有误，无法切换回表单模式，请先修正语法')
    }
  }
}

function formatJsonDraft() {
  try {
    const parsed = JSON.parse(form.value.content)
    form.value.content = JSON.stringify(parsed, null, 2)
    jsonSyntaxError.value = null
    ElMessage.success('JSON 已格式化')
  } catch (err) {
    jsonSyntaxError.value = err instanceof Error ? err.message : '格式化失败'
  }
}

function resetJsonFromForm() {
  const contentObj = buildContentFromForm()
  form.value.content = JSON.stringify(contentObj, null, 2)
  jsonSyntaxError.value = null
  ElMessage.success('已从表单重置 JSON')
}

// API 请求与列表逻辑
function errorDetail(body: unknown, fallback: string) {
  return body && typeof body === 'object' && typeof (body as Record<string, unknown>).detail === 'string'
    ? String((body as Record<string, unknown>).detail) : fallback
}

async function request(path: string, init?: RequestInit) {
  const response = await fetch(`${endpoint}${path}`, init)
  const body = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(errorDetail(body, `控制面 HTTP ${response.status}`))
  return body as Record<string, unknown>
}

async function loadAssets() {
  loading.value = true
  try {
    const query = new URLSearchParams()
    if (signalFilter.value) query.set('signal_type', signalFilter.value)
    if (typeFilter.value) query.set('asset_type', typeFilter.value)
    if (statusFilter.value) query.set('status', statusFilter.value)
    const body = await request(`/v1/control-plane/fixture-assets${query.size ? `?${query}` : ''}`)
    assets.value = (body.assets || []) as Asset[]
    if (selected.value) selected.value = assets.value.find((asset) => asset.id === selected.value?.id) || null
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : String(error))
  } finally { loading.value = false }
}

function openCreate(type: AssetType) {
  form.value = {
    asset_key: '',
    asset_type: type,
    signal_type: 'qkv_task',
    template_asset_key: type === 'instance' ? 'qkv_task.template' : '',
    template_revision: 2,
    content: '{\n  \n}',
    category_baseline: JSON.stringify(DEFAULT_CATEGORY_BASELINE, null, 2),
    catalog_baseline: JSON.stringify(DEFAULT_CATALOG_BASELINE, null, 2),
  }
  instanceSelection.value = { keyword: '', default: false }
  customBindings.value = []
  templateStdout.value = ''
  if (type === 'template') {
    loadPresetTemplate('v2_full')
  } else {
    // 初始填入默认时间
    const nowStr = formatCurrentTime()
    taskBindings.value.END = nowStr
    taskBindings.value.DESCRIPTION = '任务执行成功'
    taskBindings.value.TARGET = 'SIM-VM-1'
    taskBindings.value.VM = '9001001'
    taskBindings.value.REQUEST_ID = `,${Date.now()}`
  }
  editMode.value = 'form'
  jsonSyntaxError.value = null
  editVisible.value = true
}

function openRevision(asset: Asset) {
  form.value = {
    asset_key: asset.asset_key,
    asset_type: asset.asset_type,
    signal_type: asset.signal_type,
    template_asset_key: asset.template_asset_key || (asset.asset_type === 'instance' ? `${asset.signal_type}.template` : ''),
    template_revision: asset.template_revision || 1,
    content: JSON.stringify(asset.content, null, 2),
    category_baseline: JSON.stringify(asset.category_baseline, null, 2),
    catalog_baseline: JSON.stringify(asset.catalog_baseline, null, 2),
  }
  populateFormFromContent(asset.content)
  editMode.value = 'form'
  jsonSyntaxError.value = null
  editVisible.value = true
}

function parseJSON(value: string, label: string) {
  try { return JSON.parse(value) } catch { throw new Error(`${label} 必须是有效 JSON`) }
}

async function saveRevision() {
  let payload: Record<string, unknown>
  try {
    let contentObj: Record<string, unknown>
    if (editMode.value === 'form') {
      contentObj = buildContentFromForm()
    } else {
      contentObj = parseJSON(form.value.content, '内容')
    }
    payload = {
      ...form.value,
      content: contentObj,
      category_baseline: parseJSON(form.value.category_baseline, '分类基线'),
      catalog_baseline: parseJSON(form.value.catalog_baseline, 'Catalog 基线'),
    }
    if (form.value.asset_type === 'template') {
      delete payload.template_asset_key
      delete payload.template_revision
    }
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : String(error))
    return
  }

  saving.value = true
  try {
    const body = await request('/v1/control-plane/fixture-assets', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    editVisible.value = false
    await loadAssets()
    selected.value = body.asset as Asset
    ElMessage.success('已创建新的草稿修订')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : String(error))
  } finally {
    saving.value = false
  }
}

async function transition(asset: Asset, action: 'publish' | 'retire') {
  const label = action === 'publish' ? '发布' : '退役'
  try {
    await ElMessageBox.confirm(`确认${label} ${asset.asset_key} r${asset.revision}？`, `${label}资产`, {
      type: action === 'publish' ? 'warning' : 'info',
    })
  } catch { return }

  saving.value = true
  try {
    await request(`/v1/control-plane/fixture-assets/${encodeURIComponent(asset.asset_key)}/${asset.revision}/${action}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    })
    await loadAssets()
    ElMessage.success(`资产已${label}`)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : String(error))
  } finally {
    saving.value = false
  }
}

onMounted(loadAssets)
</script>

<template>
  <main class="asset-page">
    <header class="page-header">
      <div>
        <h2>Bundle 资产库</h2>
        <p>管理 qkv stdout 高保真模板、实例修订及其基线快照。</p>
      </div>
      <div class="header-actions">
        <el-tooltip content="返回 Bundle 工厂"><el-button :icon="Back" circle @click="router.push('/simulation/bundle-factory')" /></el-tooltip>
        <el-button :icon="Plus" type="primary" @click="openCreate('template')">新建模板</el-button>
        <el-button :icon="Plus" @click="openCreate('instance')">新建实例</el-button>
      </div>
    </header>

    <section class="filters">
      <el-select v-model="signalFilter" clearable placeholder="全部信号" @change="loadAssets">
        <el-option label="qkv_task" value="qkv_task" />
        <el-option label="qkv_alert" value="qkv_alert" />
        <el-option label="qkv_dialog" value="qkv_dialog" />
      </el-select>
      <el-select v-model="typeFilter" clearable placeholder="模板和实例" @change="loadAssets">
        <el-option label="模板" value="template" />
        <el-option label="实例" value="instance" />
      </el-select>
      <el-select v-model="statusFilter" clearable placeholder="全部状态" @change="loadAssets">
        <el-option label="草稿" value="draft" />
        <el-option label="已发布" value="published" />
        <el-option label="已退役" value="retired" />
      </el-select>
      <el-tooltip content="刷新资产列表"><el-button :icon="RefreshRight" circle :loading="loading" @click="loadAssets" /></el-tooltip>
    </section>

    <section class="asset-layout" v-loading="loading">
      <el-table :data="assets" row-key="id" height="calc(100vh - 260px)" @row-click="(asset: Asset) => selected = asset">
        <el-table-column prop="signal_type" label="信号" width="120">
          <template #default="{ row }">
            <el-tag size="small" :type="row.signal_type === 'qkv_task' ? 'primary' : row.signal_type === 'qkv_alert' ? 'warning' : 'success'">
              {{ row.signal_type }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="asset_type" label="类型" width="80">
          <template #default="{ row }">
            <span :class="row.asset_type === 'template' ? 'type-template' : 'type-instance'">
              {{ row.asset_type === 'template' ? '模板' : '实例' }}
            </span>
          </template>
        </el-table-column>
        <el-table-column prop="asset_key" label="资产键" min-width="240" />
        <el-table-column prop="revision" label="修订" width="70">
          <template #default="{ row }">r{{ row.revision }}</template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="row.status === 'published' ? 'success' : row.status === 'draft' ? 'warning' : 'info'" size="small">
              {{ row.status === 'published' ? '已发布' : row.status === 'draft' ? '草稿' : '已退役' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="匹配规则 / 模板" min-width="180">
          <template #default="{ row }">
            <span v-if="row.asset_type === 'instance' && assetSelection(row).keyword" class="rule-chip">
              关键词: <strong>{{ assetSelection(row).keyword }}</strong>
            </span>
            <span v-else-if="row.asset_type === 'instance' && assetSelection(row).default" class="rule-chip is-default">
              全局默认兜底
            </span>
            <span v-else-if="row.asset_type === 'instance'" class="rule-chip is-empty">
              指向: {{ row.template_asset_key }} r{{ row.template_revision }}
            </span>
            <span v-else class="text-muted">核心骨架规范</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="150" fixed="right">
          <template #default="{ row }">
            <el-tooltip content="查看详情"><el-button :icon="View" circle text @click.stop="selected = row" /></el-tooltip>
            <el-tooltip content="创建新修订"><el-button :icon="EditPen" circle text type="primary" @click.stop="openRevision(row)" /></el-tooltip>
            <el-tooltip v-if="row.status === 'draft'" content="发布"><el-button :icon="Check" circle text type="success" @click.stop="transition(row, 'publish')" /></el-tooltip>
          </template>
        </el-table-column>
      </el-table>

      <aside class="detail">
        <template v-if="selected">
          <div class="detail-head">
            <h3>{{ selected.asset_key }} <small>r{{ selected.revision }}</small></h3>
            <el-tag size="small" :type="selected.status === 'published' ? 'success' : selected.status === 'draft' ? 'warning' : 'info'">
              {{ selected.status }}
            </el-tag>
          </div>

          <el-descriptions :column="1" size="small" border class="meta-desc">
            <el-descriptions-item label="信号 / 类型">
              <el-tag size="small">{{ selected.signal_type }}</el-tag>
              <span style="margin-left: 8px">{{ selected.asset_type === 'template' ? '格式模板' : '场景实例' }}</span>
            </el-descriptions-item>
            <el-descriptions-item v-if="selected.template_asset_key" label="引用模板">
              <code>{{ selected.template_asset_key }} (r{{ selected.template_revision }})</code>
            </el-descriptions-item>
            <el-descriptions-item v-if="selected.asset_type === 'instance'" label="命中规则">
              <span v-if="assetSelection(selected).keyword">关键词包含：<strong>{{ assetSelection(selected).keyword }}</strong></span>
              <span v-else-if="assetSelection(selected).default" class="badge-default">开启全局默认兜底 (Default)</span>
              <span v-else>未配置关键字</span>
            </el-descriptions-item>
            <el-descriptions-item label="内容摘要"><code>{{ selected.content_digest }}</code></el-descriptions-item>
            <el-descriptions-item label="调用链"><code>{{ selected.trace_id }}</code></el-descriptions-item>
          </el-descriptions>

          <template v-if="selected.asset_type === 'instance' && selected.content?.bindings">
            <h4>变量绑定事实 (Bindings)</h4>
            <div class="bindings-table">
              <div v-for="(v, k) in (selected.content.bindings as Record<string, string>)" :key="k" class="binding-row">
                <span class="b-key">{{ k }}</span>
                <span class="b-val">{{ v }}</span>
              </div>
            </div>
          </template>

          <h4>完整内容 JSON</h4>
          <pre>{{ JSON.stringify(selected.content, null, 2) }}</pre>

          <details class="baseline-details">
            <summary>分类与 Catalog 基线快照</summary>
            <h4>分类基线</h4>
            <pre>{{ JSON.stringify(selected.category_baseline, null, 2) }}</pre>
            <h4>Catalog 基线</h4>
            <pre>{{ JSON.stringify(selected.catalog_baseline, null, 2) }}</pre>
          </details>
        </template>
        <el-empty v-else description="选择一个资产修订查看详情" />
      </aside>
    </section>

    <!-- 创建 / 编辑修订 Dialog（支持表单编辑与 JSON 双模式） -->
    <el-dialog
      v-model="editVisible"
      :title="form.asset_key ? `创建资产新修订 (${form.asset_key})` : '创建资产'"
      width="min(980px, 95vw)"
      top="5vh"
      :close-on-click-modal="false"
      class="asset-dialog"
    >
      <!-- 模式切换与操作栏 -->
      <div class="dialog-mode-bar">
        <el-radio-group v-model="editMode" size="small" @change="(val: any) => switchEditMode(val)">
          <el-radio-button value="form">
            <el-icon style="margin-right: 4px"><SetUp /></el-icon>表单编辑向导
          </el-radio-button>
          <el-radio-button value="json">
            <el-icon style="margin-right: 4px"><Document /></el-icon>JSON 显式编辑
          </el-radio-button>
        </el-radio-group>
        <div v-if="editMode === 'json'" class="mode-actions">
          <el-button size="small" text type="primary" @click="formatJsonDraft">格式化 JSON</el-button>
          <el-button size="small" text @click="resetJsonFromForm">从表单重置</el-button>
        </div>
      </div>

      <el-form label-position="top">
        <!-- 基础元数据网格 -->
        <div class="form-grid-top">
          <el-form-item label="资产键 (Asset Key)" required>
            <el-input v-model="form.asset_key" :disabled="!!selected && !!form.asset_key" placeholder="如 qkv_task.instance.delete_vm" />
          </el-form-item>
          <el-form-item label="信号类型 (Signal Type)" required>
            <el-select v-model="form.signal_type" :disabled="!!selected">
              <el-option label="任务查询 (qkv_task)" value="qkv_task" />
              <el-option label="告警查询 (qkv_alert)" value="qkv_alert" />
              <el-option label="日志弹框 (qkv_dialog)" value="qkv_dialog" />
            </el-select>
          </el-form-item>
          <el-form-item label="资产类型 (Asset Type)" required>
            <el-radio-group v-model="form.asset_type" :disabled="!!selected">
              <el-radio value="template">模板 (Template)</el-radio>
              <el-radio value="instance">实例 (Instance)</el-radio>
            </el-radio-group>
          </el-form-item>
        </div>

        <!-- 表单可视化编辑模式 -->
        <template v-if="editMode === 'form'">
          <!-- A. 实例专属配置区 -->
          <template v-if="form.asset_type === 'instance'">
            <div class="section-card">
              <div class="card-header">
                <strong>1. 模板关联与版本</strong>
              </div>
              <div class="card-body form-grid-2">
                <el-form-item label="引用的模板资产键" required>
                  <el-select v-model="form.template_asset_key" filterable allow-create placeholder="请选择或输入模板键">
                    <el-option v-for="tpl in availableTemplates" :key="tpl.asset_key" :label="`${tpl.asset_key} (r${tpl.revision})`" :value="tpl.asset_key" />
                  </el-select>
                </el-form-item>
                <el-form-item label="模板修订号" required>
                  <el-input-number v-model="form.template_revision" :min="1" />
                </el-form-item>
              </div>
            </div>

            <div class="section-card">
              <div class="card-header">
                <strong>2. 匹配规则设置 (Selection)</strong>
              </div>
              <div class="card-body">
                <div class="form-grid-2">
                  <el-form-item label="匹配关键词 (-k / --keyword)">
                    <el-input v-model="instanceSelection.keyword" placeholder="命令行包含的检索词，如 删除虚拟机" />
                    <div class="rec-tags">
                      <span>常用推荐：</span>
                      <el-tag
                        v-for="kw in recommendedKeywords"
                        :key="kw"
                        size="small"
                        class="kw-tag"
                        @click="instanceSelection.keyword = kw"
                      >
                        {{ kw }}
                      </el-tag>
                    </div>
                  </el-form-item>
                  <el-form-item label="全局默认兜底 (Default Fallback)">
                    <el-switch v-model="instanceSelection.default" active-text="开启全局默认兜底" />
                    <div class="field-hint">开启后，若命令行关键词未命中任何特定实例，将自动使用本实例作为该信号的默认输出。</div>
                  </el-form-item>
                </div>
              </div>
            </div>

            <div class="section-card">
              <div class="card-header">
                <strong>3. 业务变量注入 (Bindings)</strong>
                <el-button size="small" text type="primary" @click="fillCurrentTime">填入当前时间</el-button>
              </div>
              <div class="card-body">
                <!-- qkv_task 专属表单项（严格对齐 Tool Registry 10 个标准产出变量 produces） -->
                <template v-if="form.signal_type === 'qkv_task'">
                  <div class="form-grid-2">
                    <el-form-item label="任务类型名称 (TYPE)" required>
                      <el-input v-model="taskBindings.TYPE" placeholder="如 删除虚拟机" />
                    </el-form-item>
                    <el-form-item label="任务异常描述 (DESCRIPTION)" required>
                      <el-input v-model="taskBindings.DESCRIPTION" placeholder="如 创建回收站目录失败" />
                    </el-form-item>
                  </div>
                  <div class="form-grid-3">
                    <el-form-item label="任务进度状态 (PROCESS)" required>
                      <el-input v-model="taskBindings.PROCESS" placeholder="状态 如 完成 / 失败" />
                    </el-form-item>
                    <el-form-item label="错误码链路 (ERRCODE_TRACING)">
                      <el-input v-model="taskBindings.ERRCODE_TRACING" placeholder="null 或 0x0100186F/..." />
                    </el-form-item>
                    <el-form-item label="调用链 ID (REQUEST_ID)">
                      <el-input v-model="taskBindings.REQUEST_ID" placeholder="如 ,a3a9e0350ab8121dd7ac9fbbe66bea77" />
                    </el-form-item>
                  </div>
                  <div class="form-grid-3">
                    <el-form-item label="目标对象名 (TARGET)">
                      <el-input v-model="taskBindings.TARGET" placeholder="如 Ubuntu-26.04_import_1" />
                    </el-form-item>
                    <el-form-item label="虚拟机标识 (VM)">
                      <el-input v-model="taskBindings.VM" placeholder="如 1114365066966" />
                    </el-form-item>
                    <el-form-item label="宿主机标识 (HOST / HOSTID)">
                      <div style="display:flex; gap:8px">
                        <el-input v-model="taskBindings.HOST" placeholder="主机名 如 SVR_aCloud_668" />
                        <el-input v-model="taskBindings.HOSTID" placeholder="主机ID 如 host-047bcb4bc820" />
                      </div>
                    </el-form-item>
                  </div>
                  <el-form-item label="任务发生时间 (END)">
                    <el-input v-model="taskBindings.END" placeholder="YYYY-MM-DD HH:MM:SS 如 2026-06-23 22:54:03" />
                  </el-form-item>
                </template>

                <!-- qkv_alert 专属表单项 -->
                <template v-else-if="form.signal_type === 'qkv_alert'">
                  <div class="form-grid-2">
                    <el-form-item label="告警类型 (ALERT_TYPE)" required>
                      <el-input v-model="alertBindings.ALERT_TYPE" placeholder="如 ha_out_of_resource" />
                    </el-form-item>
                    <el-form-item label="紧急程度 (URGENT_TYPE)">
                      <el-select v-model="alertBindings.URGENT_TYPE">
                        <el-option label="紧急" value="紧急" />
                        <el-option label="重要" value="重要" />
                        <el-option label="次要" value="次要" />
                        <el-option label="提示" value="提示" />
                      </el-select>
                    </el-form-item>
                  </div>
                  <el-form-item label="告警详情 (DESCRIPTION)" required>
                    <el-input v-model="alertBindings.DESCRIPTION" type="textarea" :rows="2" placeholder="预测性告警详细信息..." />
                  </el-form-item>
                  <div class="form-grid-2">
                    <el-form-item label="告警对象 (OBJECT_NAME / TARGET)">
                      <el-input v-model="alertBindings.TARGET" placeholder="如 SVR_aCloud_668" />
                    </el-form-item>
                    <el-form-item label="对象类型 (OBJECT_TYPE)">
                      <el-input v-model="alertBindings.OBJECT_TYPE" placeholder="如 集群 / 虚拟机" />
                    </el-form-item>
                  </div>
                </template>

                <!-- qkv_dialog 专属表单项 -->
                <template v-else-if="form.signal_type === 'qkv_dialog'">
                  <div class="form-grid-3">
                    <el-form-item label="日志日号 (DAY)">
                      <el-input v-model="dialogBindings.DAY" placeholder="如 26" />
                    </el-form-item>
                    <el-form-item label="进程号 (PID)">
                      <el-input v-model="dialogBindings.PID" placeholder="如 6955" />
                    </el-form-item>
                    <el-form-item label="虚拟机名 (VM_NAME)">
                      <el-input v-model="dialogBindings.VM_NAME" placeholder="如 Ubuntu-26.04_import_1" />
                    </el-form-item>
                  </div>
                  <el-form-item label="错误详细信息 (ERROR_MESSAGE)">
                    <el-input v-model="dialogBindings.ERROR_MESSAGE" placeholder="如 虚拟机镜像忙，正在执行其他操作！" />
                  </el-form-item>
                </template>

                <!-- 自定义附加变量 -->
                <div class="custom-bindings-block">
                  <div class="custom-head">
                    <span>附加自定义变量 (Custom Bindings)</span>
                    <el-button size="small" text type="primary" :icon="Plus" @click="addCustomBinding">添加变量</el-button>
                  </div>
                  <div v-for="(item, idx) in customBindings" :key="idx" class="custom-row">
                    <el-input v-model="item.key" size="small" placeholder="变量键 (如 RISK_LEVEL)" style="width: 200px" />
                    <el-input v-model="item.value" size="small" placeholder="变量值" />
                    <el-button size="small" text type="danger" @click="removeCustomBinding(idx)">删除</el-button>
                  </div>
                </div>
              </div>
            </div>
          </template>

          <!-- B. 模板专属配置区 -->
          <template v-else>
            <div class="section-card">
              <div class="card-header">
                <strong>模板预设与占位符规范 (Template Definition)</strong>
                <div class="preset-btns">
                  <el-button v-if="form.signal_type === 'qkv_task'" size="small" type="primary" plain @click="loadPresetTemplate('v2_full')">
                    加载 acli task 全字段高保真模板 (v2)
                  </el-button>
                  <el-button size="small" plain @click="loadPresetTemplate('v1_minimal')">
                    加载轻量精简模板
                  </el-button>
                </div>
              </div>
              <div class="card-body">
                <div class="chip-bar">
                  <span class="chip-label">常用占位符（点击快速插入）：</span>
                  <el-tag
                    v-for="ph in availablePlaceholders"
                    :key="ph"
                    size="small"
                    class="ph-chip"
                    @click="insertPlaceholder(ph)"
                  >
                    + &#123;&#123;{{ ph }}&#125;&#125;
                  </el-tag>
                </div>
                <el-form-item label="stdout 格式模板 (stdout_template)" required>
                  <el-input
                    v-model="templateStdout"
                    type="textarea"
                    :rows="12"
                    class="json-input"
                    placeholder="在此输入包含 {{VARIABLE}} 占位符的 JSON 或纯文本模板..."
                  />
                </el-form-item>
                <div class="field-hint">
                  提示：纯文本占位符请使用 <code>"&#123;&#123;KEYWORD&#125;&#125;"</code>；数值或 null 类型字段使用 <code>&#123;&#123;STATUS&#125;&#125;</code> / <code>&#123;&#123;ERRCODE_TRACING&#125;&#125;</code>。
                </div>
              </div>
            </div>
          </template>

          <!-- 基线快照折叠面板 -->
          <details class="baseline-accordion">
            <summary class="baseline-summary">
              <span>查看与配置基线快照 (Category & Catalog Baselines)</span>
              <el-button size="small" text type="primary" @click.stop="loadDefaultBaselines">一键载入系统标准基线</el-button>
            </summary>
            <div class="form-grid-2" style="margin-top: 10px">
              <el-form-item label="分类基线快照 JSON">
                <el-input v-model="form.category_baseline" type="textarea" :rows="4" class="json-input" />
              </el-form-item>
              <el-form-item label="Catalog 基线快照 JSON">
                <el-input v-model="form.catalog_baseline" type="textarea" :rows="4" class="json-input" />
              </el-form-item>
            </div>
          </details>
        </template>

        <!-- JSON 显式编辑模式 -->
        <template v-else>
          <div class="json-editor-wrapper">
            <el-form-item label="内容 JSON (content_json)" required>
              <el-input
                v-model="form.content"
                type="textarea"
                :rows="14"
                class="json-input"
                placeholder="直接编辑 JSON 对象..."
              />
            </el-form-item>
            <el-alert
              v-if="jsonSyntaxError"
              :title="jsonSyntaxError"
              type="error"
              :closable="false"
              show-icon
              style="margin-bottom: 12px"
            />
            <div class="form-grid-2">
              <el-form-item label="分类基线 JSON">
                <el-input v-model="form.category_baseline" type="textarea" :rows="4" class="json-input" />
              </el-form-item>
              <el-form-item label="Catalog 基线 JSON">
                <el-input v-model="form.catalog_baseline" type="textarea" :rows="4" class="json-input" />
              </el-form-item>
            </div>
          </div>
        </template>
      </el-form>

      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="saveRevision">
          {{ form.asset_key ? '创建草稿新修订' : '保存为草稿' }}
        </el-button>
      </template>
    </el-dialog>
  </main>
</template>

<style scoped>
.asset-page { min-width: 0; color: #303133; }
.page-header { display:flex; justify-content:space-between; gap:16px; align-items:flex-end; margin-bottom:14px; }
h2 { margin:0; font-size:22px; font-weight:600; }
.page-header p { margin:6px 0 0; color:#606266; font-size:13px; }
.header-actions, .filters { display:flex; align-items:center; gap:8px; }
.filters { margin-bottom:12px; }
.filters .el-select { width:150px; }

.asset-layout {
  display: grid;
  grid-template-columns: minmax(0, 1.35fr) minmax(330px, 0.65fr);
  border: 1px solid #e4e7ed;
  border-radius: 8px;
  overflow: hidden;
  background: #fff;
}

.detail {
  padding: 16px;
  border-left: 1px solid #e4e7ed;
  overflow: auto;
  max-height: calc(100vh - 260px);
  background: #fafafa;
}

.detail-head { display:flex; justify-content:space-between; align-items:center; margin-bottom:12px; }
.detail-head h3 { margin:0; font-size:16px; }
h4 { margin: 16px 0 8px; font-size:13px; color:#409eff; }
small { color:#909399; font-weight:400; font-size:13px; }

.meta-desc { margin-bottom: 12px; background: #fff; }
.badge-default { color: #e6a23c; font-weight: 500; }

.rule-chip { font-size: 12px; color: #303133; }
.rule-chip.is-default { color: #e6a23c; font-weight: 600; }
.rule-chip.is-empty { color: #909399; }
.type-template { color: #409eff; font-weight: 500; }
.type-instance { color: #67c23a; font-weight: 500; }

.bindings-table {
  background: #fff;
  border: 1px solid #e4e7ed;
  border-radius: 4px;
  overflow: hidden;
  margin-bottom: 12px;
}
.binding-row {
  display: flex;
  font-size: 12px;
  border-bottom: 1px solid #ebeef5;
}
.binding-row:last-child { border-bottom: none; }
.b-key {
  width: 140px;
  padding: 6px 10px;
  background: #f5f7fa;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-weight: 600;
  color: #606266;
  border-right: 1px solid #ebeef5;
  flex-shrink: 0;
}
.b-val {
  padding: 6px 10px;
  color: #303133;
  word-break: break-all;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}

pre {
  margin: 0;
  padding: 10px;
  background: #f6f8fa;
  border: 1px solid #ebeef5;
  border-radius: 4px;
  overflow: auto;
  font: 11px/1.45 ui-monospace, SFMono-Regular, Menlo, monospace;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 260px;
}

.baseline-details {
  margin-top: 14px;
  font-size: 12px;
  color: #606266;
}
.baseline-details summary { cursor: pointer; color: #409eff; margin-bottom: 6px; }

/* Dialog 表单样式 */
.dialog-mode-bar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
  padding-bottom: 10px;
  border-bottom: 1px solid #ebeef5;
}
.form-grid-top {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 14px;
  margin-bottom: 12px;
}
.form-grid-2 {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
}
.form-grid-3 {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
}

.section-card {
  border: 1px solid #e4e7ed;
  border-radius: 6px;
  background: #fafafa;
  margin-bottom: 14px;
  overflow: hidden;
}
.card-header {
  padding: 8px 14px;
  background: #f5f7fa;
  border-bottom: 1px solid #e4e7ed;
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 13px;
  color: #303133;
}
.card-body {
  padding: 14px;
  background: #fff;
}

.rec-tags {
  margin-top: 6px;
  font-size: 12px;
  color: #909399;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}
.kw-tag { cursor: pointer; transition: all 0.2s; }
.kw-tag:hover { color: #409eff; border-color: #409eff; }

.chip-bar {
  margin-bottom: 10px;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}
.chip-label { font-size: 12px; color: #606266; }
.ph-chip { cursor: pointer; }
.ph-chip:hover { transform: translateY(-1px); }

.field-hint {
  font-size: 12px;
  color: #909399;
  margin-top: 4px;
  line-height: 1.4;
}

.custom-bindings-block {
  margin-top: 14px;
  padding-top: 10px;
  border-top: 1px dashed #e4e7ed;
}
.custom-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
  font-size: 13px;
  color: #606266;
  font-weight: 500;
}
.custom-row {
  display: flex;
  gap: 8px;
  margin-bottom: 8px;
}

.baseline-accordion {
  border: 1px dashed #dcdfe6;
  border-radius: 4px;
  padding: 8px 12px;
  margin-top: 10px;
}
.baseline-summary {
  cursor: pointer;
  font-size: 12px;
  color: #606266;
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.json-input :deep(textarea) {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
}

@media (max-width: 900px) {
  .asset-layout { grid-template-columns: 1fr; }
  .detail { border-left: 0; border-top: 1px solid #e4e7ed; }
  .form-grid-top, .form-grid-2, .form-grid-3 { grid-template-columns: 1fr; }
  .page-header { align-items: flex-start; flex-direction: column; }
}
</style>
