<script setup lang="ts">
/**
 * 自定义场景管理面板：平台只有两条下发通道，产物形态完全不同。
 *
 * 第一性原理：场景最终物化为**节点上的一个文件**，两条通道写的不是同一个东西：
 *  - `-c`：`{"paths":"a,b,c"}`，插件逐条 `cp -r`，不认 filters、不 glob、不执行命令；
 *  - spc：覆写 `spc/<slot>.json`，是数组，保留 action/filters/{date}/note 分组。
 *
 * 由此推出的硬规则：
 *  1. 通道是**结构选择**而不是展示偏好：切到 -c 时，带 filters/通配符/非 copy 的
 *     条目装不下（插件会静默跳过），必须在保存前拦住，不能等到任务"成功但少收"；
 *  2. 保存前必须试算：条数/字节数/最终下发文本都要在保存前可见；
 *  3. 编辑不覆盖历史：后端每次生成新 revision，本页只展示当前最新版；
 *  4. 勾选清单时按 path 去重：后端 `_normalize_items` 也按 path 去重，
 *     other/system/host_offline 三份清单条目高度重合，跨模块重复勾选会被
 *     静默合并成一条，必须当场提示而不是让用户以为选了 150 条。
 *
 * 预览文本一律来自服务端 `validate`（`preview_text`），前端不复刻序列化协议。
 */
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import {
  createCustomScenario,
  deleteCustomScenario,
  getSpcCatalog,
  isSpcSource,
  listCustomScenarios,
  updateCustomScenario,
  validateCustomScenario,
  type CustomScenario,
  type CustomScenarioAction,
  type CustomScenarioCategory,
  type CustomScenarioCreatePayload,
  type CustomScenarioItem,
  type CustomScenarioReviewState,
  type CustomScenarioSource,
  type SpcCatalog,
  type SpcCatalogItem,
} from '@/api/logCollectCustomScenario'

const rows = ref<CustomScenario[]>([])
const loading = ref(false)
const saving = ref(false)
const drawerVisible = ref(false)
const editing = ref<CustomScenario | null>(null)

const form = reactive({
  code: '',
  label: '',
  summary: '',
  applies_to: '',
  source: 'visual' as CustomScenarioSource,
  sort_order: 100,
  enabled: true,
  review_state: 'pending_review' as CustomScenarioReviewState,
})

const items = ref<CustomScenarioItem[]>([])
const bulkText = ref('')
const bulkError = ref('')

const isEdit = computed(() => Boolean(editing.value))
const spcMode = computed(() => isSpcSource(form.source))

const CATEGORY_TEXT: Record<CustomScenarioCategory, string> = {
  log: '日志',
  config: '配置',
  file: '其他文件',
}
const ACTION_TEXT: Record<CustomScenarioAction, string> = {
  copy: '拷贝',
  command: '命令',
  script: '脚本',
}
const SOURCE_TEXT: Record<CustomScenarioSource, string> = {
  spc: '日志清单（spc）',
  visual: '-c 路径',
  json: '-c 路径',
}

// ── 试算与预览（服务端真源）─────────────────────────────────────────────
const previewText = ref('')
const previewKind = ref<'config' | 'spc'>('config')
const previewBytes = ref(0)
const previewMessages = ref<string[]>([])
const previewPending = ref(false)
let previewSeq = 0
let previewTimer: ReturnType<typeof setTimeout> | undefined

const codeValid = computed(() => /^[a-z][a-z0-9_]{1,31}$/.test(form.code.trim()))

function buildPayload(): CustomScenarioCreatePayload {
  return {
    code: form.code.trim(),
    label: form.label.trim(),
    summary: form.summary.trim() || null,
    applies_to: form.applies_to.trim() || null,
    items: items.value.map(normalizeItem),
    source: form.source,
    enabled: form.enabled,
    sort_order: form.sort_order,
    review_state: form.review_state,
  }
}

function normalizeItem(item: CustomScenarioItem): CustomScenarioItem {
  // 只裁剪空白，**不按通道丢掉 action/filters**：切到 -c 后若静默剥离这些字段，
  // 保存会"成功"，但节点上的配置已经和用户勾选的不一样了。让服务端拒绝才对。
  return {
    ...item,
    path: item.path.trim(),
    note: item.note?.trim() || null,
  }
}

async function refreshPreview() {
  // 抽屉可能已经关掉（或组件已卸载），排队中的试算不该再发请求，
  // 否则上一个页面/上一个测试的定时器会污染下一个用例，也会打到线上接口。
  if (!drawerVisible.value) return
  if (!codeValid.value) {
    previewText.value = ''
    previewBytes.value = 0
    previewMessages.value = []
    return
  }
  const seq = ++previewSeq
  previewPending.value = true
  try {
    const result = await validateCustomScenario(buildPayload())
    if (seq !== previewSeq) return
    previewText.value = result.preview_text
    previewKind.value = result.preview_kind
    previewBytes.value = result.config_bytes
    previewMessages.value = result.issues.map((issue) => issue.message)
  } catch (error: any) {
    if (seq !== previewSeq) return
    previewText.value = ''
    previewMessages.value = [error?.response?.data?.detail ?? String(error)]
  } finally {
    if (seq === previewSeq) previewPending.value = false
  }
}

watch(
  [items, () => form.source, () => form.code],
  () => {
    if (!drawerVisible.value) return
    if (previewTimer) clearTimeout(previewTimer)
    previewTimer = setTimeout(refreshPreview, 400)
  },
  { deep: true },
)

// ── 清单勾选（spc 快照）────────────────────────────────────────────────
const catalog = ref<SpcCatalog | null>(null)
const catalogLoading = ref(false)
const pickerVisible = ref(false)
const activeModule = ref('')
const catalogKeyword = ref('')

const selectedPaths = computed(() => new Set(items.value.map((item) => item.path)))
const activeModuleInfo = computed(
  () => catalog.value?.modules.find((module) => module.module === activeModule.value) ?? null,
)
const catalogRows = computed<SpcCatalogItem[]>(() => {
  const module = activeModuleInfo.value
  if (!module) return []
  const keyword = catalogKeyword.value.trim().toLowerCase()
  const result: SpcCatalogItem[] = []
  for (const group of module.groups) {
    for (const item of group.items) {
      if (keyword && !item.path.toLowerCase().includes(keyword) && !group.note.toLowerCase().includes(keyword)) {
        continue
      }
      result.push(item)
    }
  }
  return result
})

async function ensureCatalog() {
  if (catalog.value || catalogLoading.value) return
  catalogLoading.value = true
  try {
    catalog.value = await getSpcCatalog()
    activeModule.value = catalog.value.modules[0]?.module ?? ''
    catalogReady.value = true
  } catch (error: any) {
    ElMessage.error(`加载 spc 清单失败：${error?.response?.data?.detail ?? error?.message ?? error}`)
  } finally {
    catalogLoading.value = false
  }
}

async function togglePicker() {
  pickerVisible.value = !pickerVisible.value
  if (!pickerVisible.value) return
  await ensureCatalog()
}

// ── 主区清单速选 ────────────────────────────────────────────────────────
// 这里不是"再来一份编辑器"：清单展示与勾选放在第一屏，保存仍走同一个抽屉，
// 避免两处各自拼装条目、各自序列化，产生两份不一致的下发内容。
const draftPaths = ref<Set<string>>(new Set())
const catalogModule = ref('')
const catalogAction = ref('')
const onlyConfigUsable = ref(false)
const mainKeyword = ref('')
const catalogReady = ref(false)

const allCatalogItems = computed<SpcCatalogItem[]>(() =>
  (catalog.value?.modules ?? []).flatMap((module) => module.groups.flatMap((group) => group.items)),
)

const mainCatalogRows = computed<SpcCatalogItem[]>(() => {
  const keyword = mainKeyword.value.trim().toLowerCase()
  return allCatalogItems.value.filter((item) => {
    if (catalogModule.value && item.module !== catalogModule.value) return false
    if (catalogAction.value && item.action !== catalogAction.value) return false
    if (onlyConfigUsable.value && !item.usable_by_config) return false
    return !keyword
      || item.path.toLowerCase().includes(keyword)
      || item.group.toLowerCase().includes(keyword)
      || item.module.toLowerCase().includes(keyword)
  })
})

const selectedDraftItems = computed<SpcCatalogItem[]>(() => {
  const byPath = new Map(allCatalogItems.value.map((item) => [item.path, item]))
  return [...draftPaths.value]
    .map((path) => byPath.get(path))
    .filter((item): item is SpcCatalogItem => Boolean(item))
})

function catalogToItem(item: SpcCatalogItem): CustomScenarioItem {
  return {
    action: item.action,
    path: item.path,
    category: 'log',
    note: item.group || null,
    filters: item.filters.map((entry) => ({ ...entry })),
    date_placeholders: item.date_placeholders.map((entry) => ({ ...entry })),
    docker: item.docker,
    interpreter: item.interpreter,
  }
}

function toggleDraftItem(item: SpcCatalogItem, checked: boolean) {
  // 通道是「场景」的属性，不是「清单」的属性。主区只负责从快照里挑条目，
  // 通道在抽屉保存时决定；切到 -c 后装不下的条目由抽屉当场提示并拒绝保存。
  // 页面上放两个同名「下发通道」控件会让人分不清改的是哪一个，故此处不设通道。
  const next = new Set(draftPaths.value)
  if (checked) next.add(item.path)
  else next.delete(item.path)
  draftPaths.value = next
}

function clearDraft() {
  draftPaths.value = new Set()
}

/** 勾选结果只做一次「带入」：保存、校验、序列化仍全部复用抽屉的既有链路。 */
function bringDraftToDrawer() {
  const selected = selectedDraftItems.value
  if (!selected.length) {
    ElMessage.warning('请先在清单中勾选至少一条')
    return
  }
  openCreate()
  items.value = selected.map(catalogToItem)
  ElMessage.success(`已带入 ${selected.length} 条，确认后保存`)
}

const jsonDialogVisible = ref(false)
const jsonText = ref('')
const jsonPending = ref(false)

/** 只读 JSON 视图同样取服务端序列化结果，不在前端复刻下发协议。 */
async function openJsonView() {
  jsonDialogVisible.value = true
  jsonPending.value = true
  try {
    const result = await validateCustomScenario({
      code: 'draft_preview',
      label: '草稿预览',
      source: 'spc',
      items: selectedDraftItems.value.map(catalogToItem),
    })
    jsonText.value = result.ok
      ? result.preview_text
      : result.issues.map((issue) => issue.message).join('\n') || '当前条目无法按所选通道序列化'
  } catch (error: any) {
    jsonText.value = `试算失败：${error?.response?.data?.detail ?? error?.message ?? error}`
  } finally {
    jsonPending.value = false
  }
}

/** 该条目放进 -c 通道会被插件静默跳过：说明原因而不是静默丢弃。 */
function configIncompatibleReason(item: CustomScenarioItem | SpcCatalogItem): string {
  const action = (item as CustomScenarioItem).action ?? (item as SpcCatalogItem).action ?? 'copy'
  const filters = (item as CustomScenarioItem).filters ?? (item as SpcCatalogItem).filters ?? []
  if (action !== 'copy') return '-c 只支持 copy 条目'
  if (item.path.includes('*')) return '-c 不支持通配符'
  if (filters.length) return '-c 不支持 filters'
  return ''
}

const incompatibleItems = computed(() => items.value.filter((item) => configIncompatibleReason(item)))

function onSourceChange(next: CustomScenarioSource) {
  if (isSpcSource(next) || !incompatibleItems.value.length) return
  const sample = incompatibleItems.value.slice(0, 3).map((item) => item.path).join('、')
  ElMessage.warning(
    `已切到 -c 通道，但有 ${incompatibleItems.value.length} 条条目 -c 装不下（${sample}…）。`
    + '这些条目会被插件静默跳过，请先移除或改回 spc 通道再保存。',
  )
}

function toggleCatalogItem(item: SpcCatalogItem, checked: boolean) {
  if (!checked) {
    items.value = items.value.filter((existing) => existing.path !== item.path)
    return
  }
  if (selectedPaths.value.has(item.path)) {
    ElMessage.info(`该路径已在清单中（可能来自其他模块）：${item.path}`)
    return
  }
  if (!spcMode.value && !item.usable_by_config) {
    ElMessage.warning(`-c 通道用不了这条：${configIncompatibleReason(item)}`)
    return
  }
  items.value.push({
    action: item.action,
    path: item.path,
    category: 'log',
    note: item.group || null,
    filters: item.filters.map((entry) => ({ ...entry })),
    date_placeholders: item.date_placeholders.map((entry) => ({ ...entry })),
    docker: item.docker,
    interpreter: item.interpreter,
  })
}

// ── 列表与编辑 ─────────────────────────────────────────────────────────
async function load() {
  loading.value = true
  try {
    rows.value = await listCustomScenarios()
  } catch (error: any) {
    ElMessage.error(`加载自定义场景失败：${error?.message ?? error}`)
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  void load()
  // 清单随镜像发布，页面打开即拉：第一屏必须能看到可勾选项，
  // 不能因为"库里还没有场景"就呈现一张空表。
  void ensureCatalog()
})

onUnmounted(() => {
  if (previewTimer) clearTimeout(previewTimer)
  // 让在途响应的序号失效，卸载后不再写回任何状态。
  previewSeq += 1
})

function resetForm() {
  Object.assign(form, {
    code: '',
    label: '',
    summary: '',
    applies_to: '',
    source: 'spc' as CustomScenarioSource,
    sort_order: 100,
    enabled: true,
    review_state: 'pending_review' as CustomScenarioReviewState,
  })
  items.value = []
  bulkText.value = ''
  bulkError.value = ''
  previewText.value = ''
  previewBytes.value = 0
  previewMessages.value = []
  pickerVisible.value = false
  catalogKeyword.value = ''
}

function openCreate() {
  editing.value = null
  resetForm()
  drawerVisible.value = true
}

function openEdit(row: CustomScenario) {
  editing.value = row
  Object.assign(form, {
    code: row.code,
    label: row.label,
    summary: row.summary ?? '',
    applies_to: row.applies_to ?? '',
    source: row.source,
    sort_order: row.sort_order,
    enabled: row.enabled,
    review_state: row.review_state,
  })
  items.value = row.items.map((item) => ({ ...item }))
  bulkText.value = ''
  bulkError.value = ''
  previewText.value = ''
  previewBytes.value = 0
  previewMessages.value = []
  drawerVisible.value = true
}

/** 与 bridge / 后端同源的轻校验：前端只为即时反馈，真防线在服务端与 bridge。 */
function pathError(raw: string): string {
  const value = raw.trim()
  if (!value) return '路径不能为空'
  if (!value.startsWith('/')) return '必须是绝对路径（以 / 开头）'
  if (value.includes('..')) return '不允许包含 ..'
  if (value.includes(',')) return '不能包含逗号'
  const probe = value.replace(/\{date\}/g, 'D').replace(/\*/g, 'D')
  if (!/^\/[A-Za-z0-9_./*-]+$/.test(probe)) return '只允许字母、数字、下划线、点、斜杠、短横线、* 与 {date}'
  if (!spcMode.value && value.includes('*')) return '-c 通道不支持通配符'
  return ''
}

function addBulk() {
  bulkError.value = ''
  const candidates = bulkText.value
    .split(/[\n,]/)
    .map((entry) => entry.trim())
    .filter(Boolean)
  if (!candidates.length) return

  const existing = new Set(items.value.map((item) => item.path))
  const rejected: string[] = []
  let added = 0
  for (const candidate of candidates) {
    const reason = pathError(candidate)
    if (reason) {
      rejected.push(`${candidate}：${reason}`)
      continue
    }
    if (existing.has(candidate)) continue
    existing.add(candidate)
    items.value.push({ action: 'copy', path: candidate, category: 'log', note: null })
    added += 1
  }
  bulkText.value = ''
  if (rejected.length) {
    bulkError.value = `已忽略 ${rejected.length} 条：${rejected.slice(0, 3).join('；')}`
  }
  if (added === 0 && !rejected.length) {
    bulkError.value = '没有新增路径（可能全部重复）'
  }
}

function removeItem(index: number) {
  items.value.splice(index, 1)
}

async function submit() {
  const label = form.label.trim()
  if (!label) {
    ElMessage.warning('场景名称必填')
    return
  }
  if (!isEdit.value && !codeValid.value) {
    ElMessage.warning('场景标识需为小写字母开头，仅含小写字母、数字与下划线')
    return
  }
  if (!items.value.length) {
    ElMessage.warning('至少添加一条收集条目')
    return
  }
  if (!spcMode.value && incompatibleItems.value.length) {
    ElMessage.error(
      `有 ${incompatibleItems.value.length} 条条目 -c 通道装不下（${configIncompatibleReason(incompatibleItems.value[0])}），请移除后再保存`,
    )
    return
  }

  const payload = buildPayload()

  saving.value = true
  try {
    // 保存前试算：把"多大一份配置、最终下发什么"变成保存前就能看到的事实
    const validation = await validateCustomScenario(payload)
    if (!validation.ok) {
      ElMessage.error(validation.issues.map((issue) => issue.message).join('；') || '配置校验未通过')
      return
    }
    if (validation.config_bytes > 256 * 1024) {
      ElMessage.error(`配置过大（${validation.config_bytes} 字节），请精简条目后再保存`)
      return
    }
    if (isEdit.value && editing.value) {
      await updateCustomScenario(editing.value.code, payload)
      ElMessage.success(`已保存，当前为第 ${editing.value.revision + 1} 版`)
    } else {
      await createCustomScenario(payload)
      ElMessage.success('已创建自定义场景')
    }
    drawerVisible.value = false
    await load()
  } catch (error: any) {
    ElMessage.error(`保存失败：${error?.response?.data?.detail ?? error?.message ?? error}`)
  } finally {
    saving.value = false
  }
}

async function toggle(row: CustomScenario) {
  try {
    await updateCustomScenario(row.code, { enabled: !row.enabled })
    await load()
  } catch (error: any) {
    ElMessage.error(`操作失败：${error?.response?.data?.detail ?? error?.message ?? error}`)
  }
}

async function remove(row: CustomScenario) {
  try {
    await ElMessageBox.confirm(
      `删除「${row.label}」后不可恢复；若该场景已被历史任务引用，平台会拒绝删除并要求改为停用。`,
      '确认删除',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await deleteCustomScenario(row.code)
    ElMessage.success('已删除')
    await load()
  } catch (error: any) {
    ElMessage.error(`删除失败：${error?.response?.data?.detail ?? error?.message ?? error}`)
  }
}

function formatTime(value: string | null): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}
</script>

<template>
  <div class="custom-scenario">
    <el-alert type="info" :closable="false" show-icon class="cs-intro">
      <template #title>自定义场景 = 一份下发给 acli 的收集定义，两条通道产物不同</template>
      <p>
        <b>日志清单（spc）</b>：覆写节点 <code>spc/&lt;槽位&gt;.json</code>，支持
        <code>copy / command / script</code>、filters、<code>{date}</code> 与分组，能力与插件原生清单一致。
      </p>
      <p>
        <b>-c 路径清单</b>：写入节点上一个 JSON 文件，只做逐条 <code>cp -r</code>。
        不支持通配符与 filters —— 放进去会被插件静默跳过，任务仍显示成功但少收了内容。
      </p>
    </el-alert>

    <!-- 第一屏就是可勾选清单：不依赖库里有没有场景，也不把入口藏进抽屉。 -->
    <el-card shadow="never" class="cs-catalog-card">
      <div class="cs-items-head">
        <span>插件清单速选（{{ mainCatalogRows.length }} / {{ allCatalogItems.length }} 条）</span>
        <span class="cs-sub">随平台发布的 11 份 spc 快照，不依赖节点在线</span>
      </div>
      <div class="cs-catalog-toolbar">
        <el-tag size="small" type="warning">spc 清单原文</el-tag>
        <span class="cs-sub">通道在保存时选；此处只挑条目</span>
        <el-select v-model="catalogModule" clearable placeholder="全部模块" size="small" style="width: 200px">
          <el-option
            v-for="module in catalog?.modules ?? []"
            :key="module.module"
            :label="`${module.label}（${module.item_count}）`"
            :value="module.module"
          />
        </el-select>
        <el-select v-model="catalogAction" clearable placeholder="全部动作" size="small" style="width: 130px">
          <el-option v-for="(text, value) in ACTION_TEXT" :key="value" :label="text" :value="value" />
        </el-select>
        <el-input v-model="mainKeyword" clearable placeholder="按路径 / 分组 / 模块搜索" size="small" style="width: 220px" />
        <el-checkbox v-model="onlyConfigUsable">只看 -c 可用</el-checkbox>
      </div>

      <el-table
        v-loading="catalogLoading"
        :data="mainCatalogRows"
        size="small"
        border
        row-key="path"
        max-height="360"
      >
        <el-table-column width="52">
          <template #default="{ row }">
            <el-checkbox
              :model-value="draftPaths.has(row.path)"
              @change="(value: any) => toggleDraftItem(row, Boolean(value))"
            />
          </template>
        </el-table-column>
        <el-table-column label="模块" width="150">
          <template #default="{ row }"><span class="cs-sub">{{ row.module }}</span></template>
        </el-table-column>
        <el-table-column label="分组" width="160">
          <template #default="{ row }"><span class="cs-sub">{{ row.group }}</span></template>
        </el-table-column>
        <el-table-column label="动作" width="80">
          <template #default="{ row }">
            <el-tag size="small" type="info">{{ ACTION_TEXT[row.action as CustomScenarioAction] }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="路径 / 命令" min-width="240">
          <template #default="{ row }"><code class="cs-path">{{ row.path }}</code></template>
        </el-table-column>
        <el-table-column label="-c 可用" width="90">
          <template #default="{ row }">
            <el-tag :type="row.usable_by_config ? 'success' : 'danger'" size="small">
              {{ row.usable_by_config ? '可用' : '不可用' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="附加" width="140">
          <template #default="{ row }">
            <div class="cs-sub">
              <span v-if="row.filters.length">filters {{ row.filters.length }} </span>
              <span v-if="row.has_wildcard">通配符 </span>
              <span v-if="row.needs_date_expand">{date}</span>
            </div>
          </template>
        </el-table-column>
      </el-table>

      <div class="cs-draft-bar">
        <span>已选 <b>{{ selectedDraftItems.length }}</b> 条</span>
        <el-button size="small" :disabled="!selectedDraftItems.length" @click="clearDraft">清空</el-button>
        <el-button size="small" @click="openJsonView">查看 JSON（只读）</el-button>
        <el-button size="small" type="primary" :disabled="!selectedDraftItems.length" @click="bringDraftToDrawer">
          带入新增场景
        </el-button>
      </div>
    </el-card>

    <el-card shadow="never">
      <div class="cs-toolbar">
        <el-button @click="load">刷新</el-button>
        <div class="cs-toolbar-right">
          <el-button type="primary" @click="openCreate">新增自定义场景</el-button>
        </div>
      </div>

      <el-table
        v-loading="loading"
        :data="rows"
        row-key="code"
        size="small"
        border
        empty-text="还没有自定义场景：客户面板只会显示默认场景"
      >
        <el-table-column label="场景名称" min-width="200">
          <template #default="{ row }">
            <div class="cs-name">{{ row.label }}</div>
            <div class="cs-sub">{{ row.summary || '未填写释义' }}</div>
          </template>
        </el-table-column>
        <el-table-column label="标识（只读）" width="170">
          <template #default="{ row }">
            <code class="cs-code">{{ row.code }}</code>
          </template>
        </el-table-column>
        <el-table-column label="通道" width="130">
          <template #default="{ row }">
            <el-tag :type="isSpcSource(row.source) ? 'warning' : 'info'" size="small">
              {{ SOURCE_TEXT[row.source as CustomScenarioSource] }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="条目数" width="80">
          <template #default="{ row }">{{ row.item_count }}</template>
        </el-table-column>
        <el-table-column label="版本" width="70">
          <template #default="{ row }">v{{ row.revision }}</template>
        </el-table-column>
        <el-table-column label="状态" width="80">
          <template #default="{ row }">
            <el-tag :type="row.enabled ? 'success' : 'info'" size="small">
              {{ row.enabled ? '启用' : '停用' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="最后修改" width="150">
          <template #default="{ row }">
            <div class="cs-sub">{{ formatTime(row.updated_at) }}</div>
            <div class="cs-sub">{{ row.updated_by || '—' }}</div>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="150" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openEdit(row)">编辑</el-button>
            <el-button link size="small" @click="toggle(row)">{{ row.enabled ? '停用' : '启用' }}</el-button>
            <el-button link type="danger" size="small" @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-drawer
      v-model="drawerVisible"
      :title="isEdit ? `编辑：${form.code}` : '新增自定义场景'"
      size="70%"
      destroy-on-close
    >
      <div class="cs-editor">
        <div class="cs-form">
          <el-form label-position="top" size="small">
            <el-form-item label="下发通道（决定节点上写什么）">
              <el-radio-group v-model="form.source" @change="onSourceChange">
                <el-radio value="spc">日志清单（spc）</el-radio>
                <el-radio value="visual">-c 路径清单</el-radio>
              </el-radio-group>
              <div class="cs-sub">
                {{ spcMode
                  ? '覆写节点 spc/<槽位>.json：支持 copy/command/script、filters、{date} 与分组。'
                  : '写入节点 -c 配置：只做逐条 cp -r，仅 copy、无通配符、无 filters 的条目可用。' }}
              </div>
            </el-form-item>
            <el-form-item label="场景名称（客户勾选项第一行）">
              <el-input v-model="form.label" maxlength="64" placeholder="例如：存储链路完整取证" />
            </el-form-item>
            <el-form-item label="场景标识（创建后不可改）">
              <el-input v-model="form.code" :disabled="isEdit" placeholder="例如：cs_storage_full" />
            </el-form-item>
            <el-form-item label="一句话释义（客户勾选项第二行）">
              <el-input v-model="form.summary" maxlength="200" placeholder="例如：卡慢盘 + 存储挂起时使用" />
            </el-form-item>
            <el-form-item label="适用说明（选填）">
              <el-input v-model="form.applies_to" maxlength="200" placeholder="例如：仅适用 aSV 6.8 及以上" />
            </el-form-item>
            <el-form-item label="排序 / 状态 / 审核">
              <div class="cs-row">
                <el-input-number v-model="form.sort_order" :min="0" :max="9999" :step="10" controls-position="right" />
                <el-switch v-model="form.enabled" active-text="启用" />
                <el-radio-group v-model="form.review_state">
                  <el-radio value="pending_review">待审核</el-radio>
                  <el-radio value="approved">已审核</el-radio>
                </el-radio-group>
              </div>
            </el-form-item>
          </el-form>
        </div>

        <div class="cs-items">
          <div class="cs-items-head">
            <span>收集条目（{{ items.length }} 条）</span>
            <span class="cs-sub">预计下发 {{ previewBytes }} 字节</span>
          </div>

          <div class="cs-actions">
            <el-button size="small" :loading="catalogLoading" @click="togglePicker">
              {{ pickerVisible ? '收起清单勾选' : '从插件清单勾选' }}
            </el-button>
            <span class="cs-sub">
              清单来自随平台发布的 11 份 spc 快照，不依赖节点在线
            </span>
          </div>

          <div v-if="pickerVisible" class="cs-picker">
            <div class="cs-picker-bar">
              <el-select v-model="activeModule" size="small" placeholder="选择模块" style="width: 260px">
                <el-option
                  v-for="module in catalog?.modules ?? []"
                  :key="module.module"
                  :label="`${module.label}（${module.item_count} 条）`"
                  :value="module.module"
                />
              </el-select>
              <el-input v-model="catalogKeyword" size="small" placeholder="按路径或分组过滤" style="width: 220px" />
              <span v-if="activeModuleInfo" class="cs-sub">
                <template v-if="!activeModuleInfo.m_whitelisted">该模块只能被 sfd_main -s 使用；</template>
                -c 可用 {{ activeModuleInfo.config_usable_count }} 条
              </span>
            </div>
            <el-table :data="catalogRows" size="small" border max-height="260" row-key="path">
              <el-table-column width="52">
                <template #default="{ row }">
                  <el-checkbox
                    :model-value="selectedPaths.has(row.path)"
                    @change="(value: any) => toggleCatalogItem(row, Boolean(value))"
                  />
                </template>
              </el-table-column>
              <el-table-column label="分组" width="150">
                <template #default="{ row }">
                  <span class="cs-sub">{{ row.group }}</span>
                </template>
              </el-table-column>
              <el-table-column label="动作" width="80">
                <template #default="{ row }">
                  <el-tag size="small" type="info">{{ ACTION_TEXT[row.action as CustomScenarioAction] }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="路径 / 命令" min-width="260">
                <template #default="{ row }">
                  <code class="cs-path">{{ row.path }}</code>
                </template>
              </el-table-column>
              <el-table-column label="-c 可用" width="90">
                <template #default="{ row }">
                  <el-tag :type="row.usable_by_config ? 'success' : 'danger'" size="small">
                    {{ row.usable_by_config ? '可用' : '不可用' }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="附加" width="150">
                <template #default="{ row }">
                  <div class="cs-sub">
                    <span v-if="row.filters.length">filters {{ row.filters.length }}</span>
                    <span v-if="row.has_wildcard"> 通配符</span>
                    <span v-if="row.needs_date_expand"> {date}</span>
                    <span v-if="row.docker"> docker</span>
                  </div>
                </template>
              </el-table-column>
            </el-table>
          </div>

          <div class="cs-bulk">
            <el-input
              v-model="bulkText"
              type="textarea"
              :rows="2"
              placeholder="手工追加：每行一条绝对路径，或用英文逗号分隔；spc 下支持 {date} 与 *"
            />
            <el-button type="primary" plain @click="addBulk">批量添加</el-button>
          </div>
          <div v-if="bulkError" class="cs-error">{{ bulkError }}</div>

          <el-table :data="items" size="small" border empty-text="还没有条目" max-height="320">
            <el-table-column v-if="spcMode" label="动作" width="110">
              <template #default="{ row }">
                <el-select v-model="row.action" size="small">
                  <el-option v-for="(text, value) in ACTION_TEXT" :key="value" :label="text" :value="value" />
                </el-select>
              </template>
            </el-table-column>
            <el-table-column label="路径 / 命令" min-width="240">
              <template #default="{ row }">
                <el-input v-model="row.path" size="small" @change="row.path = row.path.trim()" />
              </template>
            </el-table-column>
            <el-table-column label="分类" width="110">
              <template #default="{ row }">
                <el-select v-model="row.category" size="small">
                  <el-option v-for="(text, value) in CATEGORY_TEXT" :key="value" :label="text" :value="value" />
                </el-select>
              </template>
            </el-table-column>
            <el-table-column label="分组备注" width="150">
              <template #default="{ row }">
                <el-input v-model="row.note" size="small" maxlength="100" placeholder="选填" />
              </template>
            </el-table-column>
            <el-table-column label="附加" width="120">
              <template #default="{ row }">
                <div class="cs-sub">
                  <span v-if="row.filters?.length">filters {{ row.filters.length }}</span>
                  <span v-if="row.docker"> docker</span>
                  <span v-if="row.path?.includes('*')"> 通配符</span>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="" width="60">
              <template #default="{ $index }">
                <el-button link type="danger" size="small" @click="removeItem($index)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>

          <div v-if="!spcMode && incompatibleItems.length" class="cs-error">
            当前有 {{ incompatibleItems.length }} 条条目 -c 通道装不下（会被插件静默跳过），保存会被拒绝。
          </div>

          <div class="cs-preview">
            <div class="cs-items-head">
              <span>
                下发内容预览（服务端序列化）
                <el-tag size="small" type="info">{{ previewKind === 'spc' ? 'spc 清单' : '-c 配置' }}</el-tag>
              </span>
              <span class="cs-sub">{{ previewPending ? '试算中…' : '编辑后自动试算' }}</span>
            </div>
            <pre v-if="previewText">{{ previewText }}</pre>
            <div v-else class="cs-sub">
              {{ codeValid ? '尚未试算' : '填写合法的场景标识后自动试算' }}
            </div>
            <div v-for="(message, index) in previewMessages" :key="index" class="cs-error">{{ message }}</div>
          </div>
        </div>
      </div>

      <template #footer>
        <el-button @click="drawerVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submit">保存</el-button>
      </template>
    </el-drawer>

    <el-dialog v-model="jsonDialogVisible" title="当前勾选的下发内容（服务端序列化 · 只读）" width="720px">
      <pre v-loading="jsonPending" class="cs-json-pre">{{ jsonText || '尚未勾选任何条目' }}</pre>
      <template #footer>
        <el-button @click="jsonDialogVisible = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.custom-scenario {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.cs-intro p {
  margin: 4px 0 0;
  font-size: 12px;
  line-height: 1.6;
}
.cs-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}
.cs-toolbar-right {
  margin-left: auto;
}
.cs-name {
  font-weight: 600;
}
.cs-sub {
  font-size: 11.5px;
  color: var(--el-text-color-secondary);
  line-height: 1.5;
}
.cs-code {
  font-family: Consolas, Monaco, monospace;
  font-size: 11.5px;
  background: var(--el-fill-color-light);
  padding: 1px 6px;
  border-radius: 3px;
}
.cs-path {
  font-family: Consolas, Monaco, monospace;
  font-size: 11.5px;
  word-break: break-all;
}
.cs-editor {
  display: grid;
  grid-template-columns: 300px 1fr;
  gap: 16px;
}
.cs-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.cs-items-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 12px;
  font-weight: 600;
  margin-bottom: 8px;
}
.cs-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
}
.cs-picker {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 4px;
  padding: 10px;
  margin-bottom: 10px;
}
.cs-picker-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  flex-wrap: wrap;
}
.cs-bulk {
  display: flex;
  gap: 8px;
  align-items: flex-start;
  margin-bottom: 6px;
  margin-top: 10px;
}
.cs-error {
  color: var(--el-color-danger);
  font-size: 11.5px;
  line-height: 1.5;
  margin-bottom: 6px;
}
.cs-preview {
  margin-top: 14px;
}
.cs-preview pre {
  margin: 0;
  padding: 10px;
  background: #1e1f22;
  color: #abb2bf;
  border-radius: 4px;
  font-family: Consolas, Monaco, monospace;
  font-size: 11.5px;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 140px;
  overflow: auto;
}
.cs-catalog-card {
  border-radius: 6px;
}
.cs-catalog-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
  flex-wrap: wrap;
}
.cs-draft-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 10px;
  font-size: 12px;
}
.cs-json-pre {
  margin: 0;
  padding: 12px;
  background: #1e1f22;
  color: #abb2bf;
  border-radius: 4px;
  font-family: Consolas, Monaco, monospace;
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 420px;
  overflow: auto;
}
</style>
