<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Back, Check, EditPen, Plus, RefreshRight, View } from '@element-plus/icons-vue'
import { useRouter } from 'vue-router'

type AssetType = 'template' | 'instance'
type AssetStatus = 'draft' | 'published' | 'retired'

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
const form = ref({ asset_key: '', asset_type: 'template' as AssetType, signal_type: 'qkv_alert', template_asset_key: '', template_revision: 1, content: '{}', category_baseline: '{}', catalog_baseline: '{}' })

const assetGroups = computed(() => {
  const grouped = new Map<string, Asset[]>()
  for (const asset of assets.value) grouped.set(asset.asset_key, [...(grouped.get(asset.asset_key) || []), asset])
  return [...grouped.entries()].map(([key, revisions]) => ({ key, latest: revisions[0], revisions }))
})

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
  form.value = { asset_key: '', asset_type: type, signal_type: 'qkv_alert', template_asset_key: '', template_revision: 1, content: '{\n  \n}', category_baseline: '{\n  \n}', catalog_baseline: '{\n  \n}' }
  editVisible.value = true
}

function openRevision(asset: Asset) {
  form.value = { asset_key: asset.asset_key, asset_type: asset.asset_type, signal_type: asset.signal_type, template_asset_key: asset.template_asset_key || '', template_revision: asset.template_revision || 1, content: JSON.stringify(asset.content, null, 2), category_baseline: JSON.stringify(asset.category_baseline, null, 2), catalog_baseline: JSON.stringify(asset.catalog_baseline, null, 2) }
  editVisible.value = true
}

function parseJSON(value: string, label: string) {
  try { return JSON.parse(value) } catch { throw new Error(`${label} 必须是有效 JSON`) }
}

async function saveRevision() {
  let payload: Record<string, unknown>
  try {
    payload = { ...form.value, content: parseJSON(form.value.content, '内容'), category_baseline: parseJSON(form.value.category_baseline, '分类基线'), catalog_baseline: parseJSON(form.value.catalog_baseline, 'Catalog 基线') }
    if (form.value.asset_type === 'template') { delete payload.template_asset_key; delete payload.template_revision }
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : String(error)); return }
  saving.value = true
  try {
    const body = await request('/v1/control-plane/fixture-assets', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
    editVisible.value = false
    await loadAssets()
    selected.value = body.asset as Asset
    ElMessage.success('已创建新的草稿修订')
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : String(error)) } finally { saving.value = false }
}

async function transition(asset: Asset, action: 'publish' | 'retire') {
  const label = action === 'publish' ? '发布' : '退役'
  try { await ElMessageBox.confirm(`确认${label} ${asset.asset_key} r${asset.revision}？`, `${label}资产`, { type: action === 'publish' ? 'warning' : 'info' }) } catch { return }
  saving.value = true
  try { await request(`/v1/control-plane/fixture-assets/${encodeURIComponent(asset.asset_key)}/${asset.revision}/${action}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }); await loadAssets(); ElMessage.success(`资产已${label}`) } catch (error) { ElMessage.error(error instanceof Error ? error.message : String(error)) } finally { saving.value = false }
}

onMounted(loadAssets)
</script>

<template>
  <main class="asset-page">
    <header class="page-header">
      <div><h2>Bundle 资产库</h2><p>管理 qkv stdout 模板、实例修订及其基线快照。</p></div>
      <div class="header-actions">
        <el-tooltip content="返回 Bundle 工厂"><el-button :icon="Back" circle @click="router.push('/simulation/bundle-factory')" /></el-tooltip>
        <el-button :icon="Plus" type="primary" @click="openCreate('template')">新建模板</el-button>
        <el-button :icon="Plus" @click="openCreate('instance')">新建实例</el-button>
      </div>
    </header>
    <section class="filters">
      <el-select v-model="signalFilter" clearable placeholder="全部信号" @change="loadAssets"><el-option label="qkv_alert" value="qkv_alert" /><el-option label="qkv_task" value="qkv_task" /><el-option label="qkv_dialog" value="qkv_dialog" /></el-select>
      <el-select v-model="typeFilter" clearable placeholder="模板和实例" @change="loadAssets"><el-option label="模板" value="template" /><el-option label="实例" value="instance" /></el-select>
      <el-select v-model="statusFilter" clearable placeholder="全部状态" @change="loadAssets"><el-option label="草稿" value="draft" /><el-option label="已发布" value="published" /><el-option label="已退役" value="retired" /></el-select>
      <el-tooltip content="刷新资产列表"><el-button :icon="RefreshRight" circle :loading="loading" @click="loadAssets" /></el-tooltip>
    </section>
    <section class="asset-layout" v-loading="loading">
      <el-table :data="assets" row-key="id" height="calc(100vh - 260px)" @row-click="(asset: Asset) => selected = asset">
        <el-table-column prop="signal_type" label="信号" width="130" /><el-table-column prop="asset_type" label="类型" width="85" /><el-table-column prop="asset_key" label="资产键" min-width="230" /><el-table-column prop="revision" label="修订" width="70" /><el-table-column label="状态" width="90"><template #default="{ row }"><el-tag :type="row.status === 'published' ? 'success' : row.status === 'draft' ? 'warning' : 'info'" size="small">{{ row.status }}</el-tag></template></el-table-column>
        <el-table-column label="操作" width="150" fixed="right"><template #default="{ row }"><el-tooltip content="查看"><el-button :icon="View" circle text @click.stop="selected = row" /></el-tooltip><el-tooltip content="创建新修订"><el-button :icon="EditPen" circle text @click.stop="openRevision(row)" /></el-tooltip><el-tooltip v-if="row.status === 'draft'" content="发布"><el-button :icon="Check" circle text type="success" @click.stop="transition(row, 'publish')" /></el-tooltip></template></el-table-column>
      </el-table>
      <aside class="detail"><template v-if="selected"><h3>{{ selected.asset_key }} <small>r{{ selected.revision }}</small></h3><el-descriptions :column="1" size="small" border><el-descriptions-item label="内容摘要"><code>{{ selected.content_digest }}</code></el-descriptions-item><el-descriptions-item label="调用链"><code>{{ selected.trace_id }}</code></el-descriptions-item><el-descriptions-item v-if="selected.template_asset_key" label="模板引用">{{ selected.template_asset_key }} r{{ selected.template_revision }}</el-descriptions-item></el-descriptions><h4>内容</h4><pre>{{ JSON.stringify(selected.content, null, 2) }}</pre><h4>分类基线</h4><pre>{{ JSON.stringify(selected.category_baseline, null, 2) }}</pre><h4>Catalog 基线</h4><pre>{{ JSON.stringify(selected.catalog_baseline, null, 2) }}</pre></template><el-empty v-else description="选择一个资产修订" /></aside>
    </section>
    <el-dialog v-model="editVisible" :title="form.asset_key ? '创建资产新修订' : '创建资产'" width="min(860px, 94vw)" :close-on-click-modal="false"><el-form label-position="top"><div class="form-grid"><el-form-item label="资产键"><el-input v-model="form.asset_key" :disabled="!!form.asset_key && !!selected" /></el-form-item><el-form-item label="信号"><el-select v-model="form.signal_type"><el-option label="qkv_alert" value="qkv_alert" /><el-option label="qkv_task" value="qkv_task" /><el-option label="qkv_dialog" value="qkv_dialog" /></el-select></el-form-item><el-form-item label="类型"><el-radio-group v-model="form.asset_type"><el-radio value="template">模板</el-radio><el-radio value="instance">实例</el-radio></el-radio-group></el-form-item></div><div v-if="form.asset_type === 'instance'" class="form-grid"><el-form-item label="模板资产键"><el-input v-model="form.template_asset_key" /></el-form-item><el-form-item label="模板修订"><el-input-number v-model="form.template_revision" :min="1" /></el-form-item></div><el-form-item label="内容 JSON"><el-input v-model="form.content" type="textarea" :rows="10" class="json-input" /></el-form-item><div class="form-grid"><el-form-item label="分类基线 JSON"><el-input v-model="form.category_baseline" type="textarea" :rows="5" class="json-input" /></el-form-item><el-form-item label="Catalog 基线 JSON"><el-input v-model="form.catalog_baseline" type="textarea" :rows="5" class="json-input" /></el-form-item></div></el-form><template #footer><el-button @click="editVisible = false">取消</el-button><el-button type="primary" :loading="saving" @click="saveRevision">创建草稿修订</el-button></template></el-dialog>
  </main>
</template>

<style scoped>
.asset-page { min-width: 0; color: #303133; } .page-header { display:flex; justify-content:space-between; gap:16px; align-items:flex-end; margin-bottom:14px; } h2 { margin:0; font-size:22px; } .page-header p { margin:6px 0 0; color:#606266; } .header-actions,.filters { display:flex; align-items:center; gap:8px; } .filters { margin-bottom:12px; } .filters .el-select { width:150px; } .asset-layout { display:grid; grid-template-columns:minmax(0, 1.25fr) minmax(310px,.75fr); border:1px solid #e4e7ed; border-radius:8px; overflow:hidden; } .detail { padding:16px; border-left:1px solid #e4e7ed; overflow:auto; max-height:calc(100vh - 260px); } h3,h4 { margin:0 0 10px; } h4 { margin-top:16px; font-size:13px; color:#606266; } small { color:#909399; font-weight:400; } pre { margin:0; padding:10px; background:#f6f8fa; border:1px solid #ebeef5; overflow:auto; font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace; white-space:pre-wrap; word-break:break-word; } .form-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; } .form-grid:has(.el-textarea) { grid-template-columns:repeat(2,minmax(0,1fr)); } .json-input :deep(textarea) { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; } @media (max-width: 900px) { .asset-layout { grid-template-columns:1fr; } .detail { border-left:0; border-top:1px solid #e4e7ed; } .form-grid { grid-template-columns:1fr; } .page-header { align-items:flex-start; flex-direction:column; } }
</style>
