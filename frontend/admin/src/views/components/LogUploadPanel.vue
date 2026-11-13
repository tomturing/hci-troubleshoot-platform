<script setup lang="ts">
/**
 * 管理端「日志上传」：所有工单（P1/P2）的日志收集与上传情况，全只读。
 *
 * 第一性原理：列表里的「平台状态」与「日志服务器状态」是两套独立事实。
 * 平台查不到 5888 上的会话，只能说明"暂时没有记录"——会话 24h 过期、
 * acli 还没 init_upload、旧版 5888 没部署进度端点，都会造成同样的空结果。
 * 因此进度列只陈述观测结果，绝不写「失败」；只有 done + target_path 才算收妥。
 *
 * 刷新策略（已确认）：列表 10s 自动刷新；30s 真实进度轮询只对「进行中」的行，
 * 且并发分批，避免 N 个任务 × 轮询把单进程 5888 打爆。首次加载/查询时额外对
 * 已交付但尚无观测的行做一次性补查并缓存，让终态 P1 的归档结果可见，之后不再请求。
 * 组件卸载必须清定时器。
 */
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { CONCLUSION_TONES, formatLogCollectSize } from '@hci/shared'
import LogCollectAttribution from './LogCollectAttribution.vue'

import {
  getAdminTask,
  getArtifactManifest,
  getDestinationProgress,
  getNetworkProbe,
  getProcessLogContent,
  getProcessLogs,
  getTransferMetrics,
  listAdminTasks,
  type AdminLogCollectTask,
  type AdminTaskDetail,
  type AdminTaskFilters,
  type AdminTaskSummary,
  type ArtifactManifestItem,
  type DestinationProgress,
  type NetworkInfo,
  type ProcessLogItem,
  type TransferInfo,
  type TransferMetricsResponse,
} from '@/api/logCollectAdminTasks'

const LIST_REFRESH_MS = 10_000
const PROGRESS_REFRESH_MS = 30_000
const PROGRESS_CONCURRENCY = 5

const rows = ref<AdminLogCollectTask[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const loading = ref(false)
const summary = ref<AdminTaskSummary | null>(null)
const progressMap = ref<Record<string, DestinationProgress>>({})
const detailVisible = ref(false)
const detailTask = ref<AdminLogCollectTask | null>(null)
const detailData = ref<AdminTaskDetail | null>(null)
const detailTab = ref('transfer')

const evidenceLoading = ref(false)
const evidenceError = ref('')
const transferMetrics = ref<TransferMetricsResponse | null>(null)
const networkInfo = ref<NetworkInfo | null>(null)
const networkLoaded = ref(false)
const artifactManifests = ref<ArtifactManifestItem[]>([])
const processLogs = ref<ProcessLogItem[]>([])
const processLogContent = ref<Record<string, string>>({})
const processLogTruncated = ref<Record<string, boolean>>({})
const errorText = ref('')

const filters = reactive({
  days: 7,
  case_id: '',
  node_ip: '',
  upload_mode: '',
  status: '',
  delivery_status: '',
})

let listTimer: ReturnType<typeof setInterval> | undefined
let progressTimer: ReturnType<typeof setInterval> | undefined
let destroyed = false
let progressSeq = 0

const STATUS_TEXT: Record<string, string> = {
  pending: '待下发', probing: '探测中', installing: '安装中', collecting: '采集中',
  packaging: '打包中', uploading: '上传中', completed: '已完成', failed: '失败', cancelled: '已取消',
}
const DELIVERY_TEXT: Record<string, string> = {
  none: '未开始', queued: '排队中', pulling: '拉取中', uploading: '上传中',
  relaying: '代传中', delivered: '已交付', failed: '失败',
}
const MODE_TEXT: Record<string, string> = {
  acli_direct: 'P1 直传', bridge_relay: 'P2 中转', none: '未定',
}

/** 只有还在途的任务才值得查 5888：终态任务再查只会制造无效请求。
 * 口径与后端 _TRANSFERRING 一致（2026-11-12 修复）：
 * 采集活跃且交付未终结，或采集未终态且交付在途。
 * 旧口径用 OR 导致采集已 failed/completed/cancelled 但 delivery=queued 的僵尸被算入传输中。 */
const ACTIVE_STATUSES = ['collecting', 'packaging', 'uploading']
const TERMINAL_STATUSES = ['failed', 'cancelled', 'completed']
const IN_TRANSIT_DELIVERY = ['queued', 'pulling', 'uploading', 'relaying']
const TERMINAL_DELIVERY = ['delivered', 'failed']

function isActive(task: AdminLogCollectTask): boolean {
  // 结论只有平台一个来源：有 display 时直接用 conclusion，前端不再复算状态口径。
  // 旧后端/旧前端缓存没有 display 时才退回本地兼容判定。
  if (task.display) return task.display.conclusion === 'pending'
  // A: 采集活跃且交付未终结
  if (ACTIVE_STATUSES.includes(task.status) && !TERMINAL_DELIVERY.includes(task.delivery_status)) {
    return true
  }
  // B: 采集未终态且交付在途
  if (!TERMINAL_STATUSES.includes(task.status) && IN_TRANSIT_DELIVERY.includes(task.delivery_status)) {
    return true
  }
  return false
}

function buildQuery(): AdminTaskFilters {
  return {
    page: page.value,
    page_size: pageSize.value,
    days: filters.days,
    case_id: filters.case_id.trim() || undefined,
    node_ip: filters.node_ip.trim() || undefined,
    upload_mode: filters.upload_mode || undefined,
    status: filters.status || undefined,
    delivery_status: filters.delivery_status || undefined,
  }
}

async function load() {
  loading.value = true
  try {
    const result = await listAdminTasks(buildQuery())
    if (destroyed) return
    rows.value = result.items
    total.value = result.total
    summary.value = result.summary
    errorText.value = ''
  } catch (error: any) {
    if (!destroyed) errorText.value = `加载失败：${error?.response?.data?.detail ?? error?.message ?? error}`
  } finally {
    loading.value = false
  }
}

async function pollProgress(includeDelivered = false) {
  // 30s 定时器只查在途行（已确认策略，避免 N 个任务 × 轮询打爆单进程 5888）。
  // 首次加载/查询时额外对「已交付但尚无观测」的行做一次性补查：否则终态
  // 的 P1 永远不在轮询里，进度列会一直停在「查询中…」，用户看不到归档结果。
  // 结果写入 progressMap 缓存后不再请求，平台侧还有 10s TTL 缓存兜底。
  const targets = rows.value
    .filter((task) => isActive(task)
      || (includeDelivered && task.delivery_status === 'delivered'
        && !progressMap.value[task.task_id]))
    .map((task) => task.task_id)
  if (!targets.length || destroyed) return
  const seq = ++progressSeq
  for (let i = 0; i < targets.length; i += PROGRESS_CONCURRENCY) {
    const batch = targets.slice(i, i + PROGRESS_CONCURRENCY)
    const results = await Promise.allSettled(batch.map((id) => getDestinationProgress(id)))
    if (destroyed || seq !== progressSeq) return
    const next = { ...progressMap.value }
    results.forEach((result, index) => {
      if (result.status === 'fulfilled') next[batch[index]] = result.value
    })
    progressMap.value = next
  }
}

function search() {
  page.value = 1
  progressSeq += 1
  void load().then(() => pollProgress(true))
}

function resetFilters() {
  Object.assign(filters, {
    days: 7, case_id: '', node_ip: '', upload_mode: '', status: '', delivery_status: '',
  })
  search()
}

onMounted(() => {
  void load().then(() => pollProgress(true))
  listTimer = setInterval(() => void load(), LIST_REFRESH_MS)
  progressTimer = setInterval(() => void pollProgress(), PROGRESS_REFRESH_MS)
})

onUnmounted(() => {
  destroyed = true
  if (listTimer) clearInterval(listTimer)
  if (progressTimer) clearInterval(progressTimer)
  progressSeq += 1
})

/** 体积口径唯一来源：与后端 format_size / 客户面板共用契约生成的函数。 */
function formatBytes(value: number | null | undefined): string {
  return formatLogCollectSize(value)
}

function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return '—'
  const seconds = Math.max(0, Math.round(ms / 1000))
  if (seconds < 60) return `${seconds} 秒`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes} 分 ${seconds % 60} 秒`
  return `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`
}

function formatEta(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return '—'
  return formatDuration(seconds * 1000)
}

function elapsedText(task: AdminLogCollectTask): string {
  const start = task.created_at ? new Date(task.created_at).getTime() : NaN
  const end = task.delivery_finished_at || task.finished_at
  if (!Number.isFinite(start)) return '—'
  return formatDuration((end ? new Date(end).getTime() : Date.now()) - start)
}

function toneToTag(tone: string | undefined): 'success' | 'warning' | 'danger' | 'info' {
  if (tone === 'success') return 'success'
  if (tone === 'warning') return 'warning'
  if (tone === 'error') return 'danger'
  return 'info'
}

/** 没有证据时说清楚「为什么没有」，而不是拿 0/100 或绿色成功顶替。 */
const BRIDGE_OUTDATED_HINT = '桥接端版本较旧，暂无该数据'

function formatTime(value: string | null): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

/** 只陈述观测结果：查不到就是查不到，不替日志服务器下"失败"结论。 */
function progressText(task: AdminLogCollectTask): string {
  const item = progressMap.value[task.task_id]
  // 终态行不在 30s 轮询里；没有观测记录就留白，不能显示「查询中…」——
  // 那个结果永远等不到，会把「已交付」误呈现成「卡住」。
  if (!isActive(task) && !item) return '—'
  if (!item) return '查询中…'
  if (item.state === 'done') {
    return item.sha256 ? '日志服务器已收妥（sha256 与源包一致）' : '日志服务器已收妥'
  }
  if (item.state === 'uploading') {
    const received = formatBytes(item.uploaded_size)
    const totalSize = formatBytes(item.file_size)
    const chunks = item.chunk_total ? `，第 ${item.received_chunks ?? 0}/${item.chunk_total} 片` : ''
    return `日志服务器已收到 ${received}/${totalSize}（${item.progress ?? 0}%）${chunks}`
  }
  if (item.state === 'unsupported') return '该日志服务器不支持进度查询'
  if (item.state === 'unavailable') return '日志服务器暂时不可用，请稍后再查'
  return '日志服务器上还没有这次上传的记录'
}

/** 结论/环节/条件一律来自平台派生字段；缺失时降级为兼容文案，不自己拼。 */
function conclusionOf(task: AdminLogCollectTask) {
  const display = task.display
  const fallback = task.delivery_status === 'delivered' ? 'success' : 'pending'
  return {
    label: display?.conclusion_label ?? (fallback === 'success' ? '已交付' : '进行中'),
    tone: toneToTag(display?.conclusion_tone ?? CONCLUSION_TONES[fallback]),
    phase: display?.phase_label ?? '—',
    progress: display?.progress_percent ?? null,
    conditions: display?.conditions ?? [],
  }
}

function artifactText(task: AdminLogCollectTask): string {
  return task.artifact?.size_text ?? formatBytes(task.archive_size)
}

function rateText(transfer: TransferInfo | null | undefined): string {
  if (!transfer) return BRIDGE_OUTDATED_HINT
  if (transfer.stalled) return `已停滞 ${transfer.stalled_seconds ?? 0} 秒`
  return transfer.rate_text ?? '—'
}

function etaText(transfer: TransferInfo | null | undefined): string {
  if (!transfer) return '—'
  return formatEta(transfer.eta_sec)
}

async function loadEvidence(tab: string) {
  const task = detailTask.value
  if (!task) return
  evidenceLoading.value = true
  evidenceError.value = ''
  try {
    if (tab === 'transfer' && !transferMetrics.value) {
      transferMetrics.value = await getTransferMetrics(task.task_id)
    } else if (tab === 'artifact' && !artifactManifests.value.length) {
      artifactManifests.value = (await getArtifactManifest(task.task_id)).items
    } else if (tab === 'network' && !networkInfo.value && !networkLoaded.value) {
      const response = await getNetworkProbe(task.task_id)
      networkInfo.value = response.network
      networkLoaded.value = true
    } else if (tab === 'process' && !processLogs.value.length) {
      processLogs.value = (await getProcessLogs(task.task_id)).items
    }
  } catch (error: any) {
    evidenceError.value = `证据读取失败：${error?.response?.data?.detail ?? error?.message ?? error}`
  } finally {
    evidenceLoading.value = false
  }
}

async function loadProcessLogContent(logId: string) {
  const task = detailTask.value
  if (!task || processLogContent.value[logId] !== undefined) return
  try {
    const data = await getProcessLogContent(task.task_id, logId)
    processLogContent.value = { ...processLogContent.value, [logId]: data.content || '（空文件）' }
    processLogTruncated.value = { ...processLogTruncated.value, [logId]: data.truncated }
  } catch (error: any) {
    processLogContent.value = {
      ...processLogContent.value,
      [logId]: `读取失败：${error?.response?.data?.detail ?? error?.message ?? error}`,
    }
  }
}

function openDetail(task: AdminLogCollectTask) {
  detailTask.value = task
  detailData.value = null
  detailTab.value = 'transfer'
  transferMetrics.value = null
  networkInfo.value = null
  artifactManifests.value = []
  processLogs.value = []
  processLogContent.value = {}
  processLogTruncated.value = {}
  networkLoaded.value = false
  evidenceError.value = ''
  detailVisible.value = true
  void loadEvidence('transfer')
  // 单任务详情带最新传输样本与网络结论；接口不可用时其它页签仍能独立展示。
  void getAdminTask(task.task_id)
    .then((data) => {
      detailData.value = data
      if (data.network) {
        networkInfo.value = data.network
        networkLoaded.value = true
      }
      if (data.transfer && !transferMetrics.value) {
        transferMetrics.value = { task_id: task.task_id, items: [], latest: data.transfer }
      }
    })
    .catch(() => { detailData.value = null })
}

watch(detailTab, (tab) => void loadEvidence(tab))

async function copyPath(value: string | null) {
  if (!value) return
  try {
    await navigator.clipboard.writeText(value)
    ElMessage.success('路径已复制')
  } catch {
    ElMessage.warning('浏览器不允许复制，请手动选中路径文本')
  }
}

const bucketKeys = ['pending', 'success', 'partial', 'unfinished', 'failed', 'cancelled'] as const
const bucketLabels: Record<(typeof bucketKeys)[number], string> = {
  pending: '进行中', success: '已交付', partial: '已交付 · 有疑点',
  // 交付已登记但没被拉走/传完：补一次交付就能完成，不能并入「失败」劝退用户
  unfinished: '未完成 · 待补交付', failed: '失败', cancelled: '已取消',
}

/** 统计条只展示后端五桶，且现场给出可对账数字；口径不一致时显式告警。 */
const conclusionBuckets = computed(() => {
  const s = summary.value
  if (s?.by_conclusion) return s.by_conclusion
  return {
    pending: s?.transferring ?? 0,
    success: s?.delivered ?? 0,
    partial: 0,
    unfinished: 0,
    failed: s?.failed ?? 0,
    cancelled: 0,
  }
})

const bucketSum = computed(() =>
  bucketKeys.reduce((sum, key) => sum + (conclusionBuckets.value[key] ?? 0), 0),
)

const summaryIdentityOk = computed(() => {
  const s = summary.value
  if (!s?.by_conclusion) return true
  return bucketSum.value === s.total
})

const summaryCells = computed(() => [
  { label: '总数', value: summary.value?.total ?? 0 },
  ...bucketKeys.map((key) => ({ label: bucketLabels[key], value: conclusionBuckets.value[key] ?? 0 })),
])
</script>

<template>
  <div class="upload-panel">
    <div class="upload-summary">
      <div v-for="cell in summaryCells" :key="cell.label" class="upload-summary-cell">
        <div class="upload-summary-value">{{ cell.value }}</div>
        <div class="upload-sub">{{ cell.label }}</div>
      </div>
      <div class="upload-summary-cell">
        <div class="upload-summary-value">{{ summary?.p1.total ?? 0 }} / {{ summary?.p2.total ?? 0 }}</div>
        <div class="upload-sub">P1 直传 / P2 中转</div>
      </div>
      <div class="upload-summary-cell">
        <div class="upload-summary-value">{{ summary?.p1.transferring ?? 0 }} / {{ summary?.p2.transferring ?? 0 }}</div>
        <div class="upload-sub">传输中 P1 / P2</div>
      </div>
    </div>
    <div v-if="!summaryIdentityOk" class="upload-error">
      统计恒等式异常：五桶合计 {{ bucketSum }}，总数 {{ summary?.total ?? 0 }}；请刷新重试。
    </div>
    <div v-else-if="summary?.by_conclusion" class="upload-sub">
      统计口径可对账：进行中 + 已交付 + 有疑点 + 失败 + 已取消 = {{ bucketSum }} = 总数 {{ summary.total }}
    </div>

    <el-card shadow="never">
      <div class="upload-toolbar">
        <el-select v-model="filters.days" style="width: 120px">
          <el-option :value="7" label="近 7 天" />
          <el-option :value="14" label="近 14 天" />
          <el-option :value="30" label="近 30 天" />
        </el-select>
        <el-input v-model="filters.case_id" placeholder="工单号" clearable style="width: 170px" />
        <el-input v-model="filters.node_ip" placeholder="发起节点 IP" clearable style="width: 160px" />
        <el-select v-model="filters.upload_mode" clearable placeholder="交付路径" style="width: 140px">
          <el-option value="acli_direct" label="P1 直传" />
          <el-option value="bridge_relay" label="P2 中转" />
        </el-select>
        <el-select v-model="filters.status" clearable placeholder="采集状态" style="width: 140px">
          <el-option v-for="(text, value) in STATUS_TEXT" :key="value" :value="value" :label="text" />
        </el-select>
        <el-select v-model="filters.delivery_status" clearable placeholder="交付结论" style="width: 140px">
          <el-option v-for="(text, value) in DELIVERY_TEXT" :key="value" :value="value" :label="text" />
        </el-select>
        <el-button type="primary" @click="search">查询</el-button>
        <el-button @click="resetFilters">重置</el-button>
        <div class="upload-toolbar-right">
          <el-button @click="load">刷新</el-button>
        </div>
      </div>

      <div v-if="errorText" class="upload-error">{{ errorText }}</div>

      <el-table
        v-loading="loading"
        :data="rows"
        row-key="task_id"
        size="small"
        border
        empty-text="所选时间范围内没有日志收集任务"
      >
        <el-table-column label="工单号" width="165">
          <template #default="{ row }"><code class="upload-code">{{ row.case_id }}</code></template>
        </el-table-column>
        <el-table-column label="客户 / client_id" min-width="180">
          <template #default="{ row }">
            <div>{{ row.customer_name || '未关联客户' }}</div>
            <div class="upload-sub">{{ row.client_id || '—' }}</div>
          </template>
        </el-table-column>
        <el-table-column label="发起节点" width="130">
          <template #default="{ row }"><span class="upload-sub">{{ row.origin_node_ip || '—' }}</span></template>
        </el-table-column>
        <el-table-column label="交付路径" width="100">
          <template #default="{ row }">
            <el-tag size="small" :type="row.upload_mode === 'acli_direct' ? 'warning' : 'info'">
              {{ MODE_TEXT[row.upload_mode] ?? '未定' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="环节 / 结论" min-width="180">
          <template #default="{ row }">
            <div>
              <span class="upload-sub">{{ conclusionOf(row).phase }} · </span>
              <el-tag size="small" :type="conclusionOf(row).tone">{{ conclusionOf(row).label }}</el-tag>
            </div>
            <div v-if="conclusionOf(row).progress !== null" class="upload-sub">
              合成进度 {{ conclusionOf(row).progress }}%
            </div>
            <div v-else class="upload-sub">合成进度 —</div>
            <div v-if="conclusionOf(row).conditions.length" class="upload-sub">
              {{ conclusionOf(row).conditions.map(item => item.label).join('、') }}
            </div>
          </template>
        </el-table-column>
        <el-table-column label="日志服务器真实进度" min-width="240">
          <template #default="{ row }"><span class="upload-sub">{{ progressText(row) }}</span></template>
        </el-table-column>
        <el-table-column label="包大小 / 产物" width="150">
          <template #default="{ row }">
            <div>{{ artifactText(row) }}</div>
            <div v-if="row.artifact" class="upload-sub">
              {{ row.artifact.quality_label }} · {{ row.artifact.reason_text }}
            </div>
            <div v-else class="upload-sub">包内容未探测</div>
          </template>
        </el-table-column>
        <el-table-column label="速率 / ETA" width="170">
          <template #default="{ row }">
            <div class="upload-sub">{{ rateText(row.transfer) }}</div>
            <div class="upload-sub">预计完成 {{ etaText(row.transfer) }}</div>
          </template>
        </el-table-column>
        <el-table-column label="耗时" width="120">
          <template #default="{ row }"><span class="upload-sub">{{ elapsedText(row) }}</span></template>
        </el-table-column>
        <el-table-column label="交付事实" width="100">
          <template #default="{ row }">
            <el-tag size="small" type="info">{{ DELIVERY_TEXT[row.delivery_status] ?? row.delivery_status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="最近核验" width="140">
          <template #default="{ row }">
            <span class="upload-sub">
              {{ formatTime((row.archive_check?.checked_at as string) ?? row.delivery_finished_at) }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="创建 / 完成" width="140">
          <template #default="{ row }">
            <div class="upload-sub">{{ formatTime(row.created_at) }}</div>
            <div class="upload-sub">{{ formatTime(row.finished_at) }}</div>
          </template>
        </el-table-column>
        <el-table-column label="错误" min-width="140">
          <template #default="{ row }">
            <span class="upload-sub">{{ row.error_code || row.error_message || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="80" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openDetail(row)">详情</el-button>
          </template>
        </el-table-column>
      </el-table>

      <el-pagination
        v-model:current-page="page"
        v-model:page-size="pageSize"
        :total="total"
        :page-sizes="[20, 50, 100]"
        layout="total, sizes, prev, pager, next"
        class="upload-pagination"
        @change="search"
      />
    </el-card>

    <el-drawer v-model="detailVisible" title="日志收集任务详情（只读）" size="46%" destroy-on-close>
      <template v-if="detailTask">
        <el-descriptions :column="1" size="small" border>
          <el-descriptions-item label="任务 ID">{{ detailTask.task_id }}</el-descriptions-item>
          <el-descriptions-item label="工单号">{{ detailTask.case_id }}</el-descriptions-item>
          <el-descriptions-item label="客户名">{{ detailTask.customer_name || '未关联客户' }}</el-descriptions-item>
          <el-descriptions-item label="client_id">{{ detailTask.client_id || '—' }}</el-descriptions-item>
          <el-descriptions-item label="发起节点">{{ detailTask.origin_node_ip || '—' }}</el-descriptions-item>
          <el-descriptions-item label="交付路径">{{ MODE_TEXT[detailTask.upload_mode] ?? '未定' }}</el-descriptions-item>
          <el-descriptions-item label="采集状态">{{ STATUS_TEXT[detailTask.status] ?? detailTask.status }}（{{ detailTask.progress }}%）</el-descriptions-item>
          <el-descriptions-item label="交付结论">{{ DELIVERY_TEXT[detailTask.delivery_status] ?? detailTask.delivery_status }}</el-descriptions-item>
          <el-descriptions-item label="代传状态">{{ detailTask.relay_status }}</el-descriptions-item>
          <el-descriptions-item label="压缩包大小">{{ formatBytes(detailTask.archive_size) }}</el-descriptions-item>
          <el-descriptions-item label="日志服务器进度">{{ progressText(detailTask) }}</el-descriptions-item>
          <el-descriptions-item label="错误码 / 原因">{{ detailTask.error_code || '—' }} {{ detailTask.error_message || '' }}</el-descriptions-item>
          <el-descriptions-item label="调用链 trace_id">{{ detailTask.trace_id || '—' }}</el-descriptions-item>
          <el-descriptions-item label="创建 / 完成 / 交付完成">
            {{ formatTime(detailTask.created_at) }} / {{ formatTime(detailTask.finished_at) }} /
            {{ formatTime(detailTask.delivery_finished_at) }}
          </el-descriptions-item>
        </el-descriptions>

        <div class="upload-detail-paths">
          <div v-if="progressMap[detailTask.task_id]?.target_path" class="upload-path-row">
            <span class="upload-sub">日志服务器归档路径</span>
            <code class="upload-code">{{ progressMap[detailTask.task_id].target_path }}</code>
            <el-button size="small" @click="copyPath(progressMap[detailTask.task_id].target_path)">复制路径</el-button>
          </div>
          <div v-if="detailTask.archive_check" class="upload-sub">
            最近一次归档核验：{{ JSON.stringify(detailTask.archive_check) }}
          </div>
        </div>

        <div class="upload-conclusion">
          <span class="upload-sub">平台结论</span>
          <el-tag size="small" :type="conclusionOf(detailTask).tone">{{ conclusionOf(detailTask).label }}</el-tag>
          <span class="upload-sub">{{ conclusionOf(detailTask).phase }}</span>
          <el-tag
            v-for="item in conclusionOf(detailTask).conditions"
            :key="item.key"
            size="small"
            type="warning"
            effect="plain"
          >{{ item.label }}</el-tag>
        </div>

        <!--
          链路定界摘要：无论停在哪一个证据页签，管理员都能先看到首个异常点。
          客户只看到 customer_text；判据、证据入口与调用链只在管理端展开。
        -->
        <LogCollectAttribution
          variant="summary"
          :display="detailTask?.display"
          :error-code="detailTask?.error_code"
          :fallback-trace-id="detailTask?.trace_id"
        />

        <el-tabs v-model="detailTab" class="upload-tabs">
          <el-tab-pane label="链路定界" name="attribution">
            <LogCollectAttribution variant="panel" :display="detailTask?.display" />
          </el-tab-pane>

          <el-tab-pane label="传输时间线" name="transfer">
            <div v-if="evidenceError" class="upload-error">{{ evidenceError }}</div>
            <div v-if="!transferMetrics?.items?.length" class="upload-sub">{{ BRIDGE_OUTDATED_HINT }}（无传输样本）</div>
            <el-timeline v-else>
              <el-timeline-item
                v-for="(sample, index) in transferMetrics.items"
                :key="`${sample.updated_at ?? 'sample'}-${index}`"
                :timestamp="formatTime(sample.updated_at)"
              >
                <div>{{ sample.stage_label ?? '—' }} · {{ sample.percent ?? '—' }}%</div>
                <div class="upload-sub">
                  {{ formatBytes(sample.bytes_done) }} / {{ formatBytes(sample.bytes_total) }} ·
                  速率 {{ sample.rate_text ?? '—' }} · 耗时 {{ formatDuration(sample.elapsed_ms) }} ·
                  预计完成 {{ formatEta(sample.eta_sec) }}
                </div>
                <div v-if="sample.stalled" class="upload-error">
                  传输已停滞 {{ sample.stalled_seconds ?? 0 }} 秒（阈值 {{ sample.stall_threshold_seconds }} 秒）
                </div>
              </el-timeline-item>
            </el-timeline>
          </el-tab-pane>

          <el-tab-pane label="产物证据" name="artifact">
            <el-descriptions :column="1" size="small" border>
              <el-descriptions-item label="产物质量">
                {{ detailTask.artifact?.quality_label ?? '未探测' }} ·
                {{ detailTask.artifact?.reason_text ?? '包内容未探测' }}
              </el-descriptions-item>
              <el-descriptions-item label="包体积">{{ artifactText(detailTask) }}</el-descriptions-item>
              <el-descriptions-item label="条目数">
                {{ detailTask.artifact?.entry_count ?? '—' }}
              </el-descriptions-item>
              <el-descriptions-item label="条目总字节">
                {{ detailTask.artifact?.entry_total_text ?? '—' }}
              </el-descriptions-item>
              <el-descriptions-item label="证据来源">{{ detailTask.artifact?.source ?? '—' }}</el-descriptions-item>
            </el-descriptions>
            <div v-if="!artifactManifests.length" class="upload-sub">暂无内容探测记录（{{ BRIDGE_OUTDATED_HINT }}）</div>
            <div v-for="item in artifactManifests" :key="`${item.source}-${item.inspected_at}`" class="upload-manifest">
              <div>{{ item.source }} · {{ item.quality_label }} · {{ item.entry_count ?? 0 }} 条 ·
                {{ item.entry_total_text ?? '—' }}</div>
              <div class="upload-sub">{{ item.reason_text ?? '—' }} · 探测时间 {{ formatTime(item.inspected_at) }}</div>
              <div v-if="item.entries" class="upload-sub">
                前 {{ item.entries.length }} 条{{ item.entries_truncated ? '（已截断展示）' : '' }}：
                {{ JSON.stringify(item.entries.slice(0, 20)) }}
              </div>
            </div>
          </el-tab-pane>

          <el-tab-pane label="网络探测" name="network">
            <div v-if="!networkInfo" class="upload-sub">{{ BRIDGE_OUTDATED_HINT }}（尚未上报探测）</div>
            <template v-else>
              <div>网络质量：{{ networkInfo.grade_label }}（相关目标 {{ networkInfo.relevant_target ?? '未定' }}）</div>
              <div v-for="target in networkInfo.targets" :key="target.target" class="upload-network">
                <div>{{ target.target }} · {{ target.grade_label }} · 可达 {{ target.reachable === null ? '—' : (target.reachable ? '是' : '否') }}</div>
                <div class="upload-sub">
                  RTT {{ target.rtt_avg_ms ?? '—' }} ms · 抖动 {{ target.jitter_ms ?? '—' }} ms ·
                  丢包 {{ target.loss_rate ?? '—' }} · 上行 {{ target.up_text ?? '—' }} · 下行 {{ target.down_text ?? '—' }}
                </div>
                <div v-if="target.advice" class="upload-sub">{{ target.advice }}</div>
              </div>
            </template>
          </el-tab-pane>

          <el-tab-pane label="进程日志" name="process">
            <div v-if="!processLogs.length" class="upload-sub">暂无采集进程日志（{{ BRIDGE_OUTDATED_HINT }}）</div>
            <div v-for="item in processLogs" :key="item.log_id" class="upload-process-log">
              <div class="upload-path-row">
                <el-button size="small" @click="loadProcessLogContent(item.log_id)">查看内容</el-button>
                <code class="upload-code">{{ item.host }} · {{ item.path }}</code>
                <span class="upload-sub">{{ item.size_text ?? '—' }} · {{ item.status }}</span>
              </div>
              <div v-if="item.error" class="upload-error">{{ item.error }}</div>
              <pre v-if="processLogContent[item.log_id] !== undefined" class="upload-log-content">{{ processLogContent[item.log_id] }}</pre>
              <div v-if="processLogTruncated[item.log_id]" class="upload-sub">内容已按上限截断展示</div>
            </div>
          </el-tab-pane>
        </el-tabs>
      </template>
    </el-drawer>
  </div>
</template>

<style scoped>
.upload-panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
/* 链路定界：摘要卡 + 段/组件表 + 阶段时间线 */
.upload-attribution {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px 12px;
  border: 1px solid var(--el-border-color);
  border-left: 4px solid var(--el-color-warning);
  border-radius: 6px;
}
.upload-attribution-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
.upload-attribution-line {
  font-size: 14px;
  font-weight: 600;
  line-height: 1.6;
  word-break: break-word;
}
.upload-attribution .upload-sub {
  word-break: break-word;
}
.upload-section-title {
  margin: 14px 0 8px;
  font-size: 13px;
}
.upload-intro {
  font-size: 12px;
  line-height: 1.6;
}
.upload-summary {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
}
.upload-summary-cell {
  flex: 1 1 110px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  padding: 10px 12px;
  background: var(--el-bg-color);
}
.upload-summary-value {
  font-size: 20px;
  font-weight: 600;
}
.upload-sub {
  font-size: 11.5px;
  color: var(--el-text-color-secondary);
  line-height: 1.5;
}
.upload-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}
.upload-toolbar-right {
  margin-left: auto;
}
.upload-code {
  font-family: Consolas, Monaco, monospace;
  font-size: 11.5px;
  word-break: break-all;
}
.upload-error {
  color: var(--el-color-danger);
  font-size: 12px;
  margin-bottom: 8px;
}
.upload-pagination {
  margin-top: 12px;
  justify-content: flex-end;
}
.upload-detail-paths {
  margin-top: 14px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.upload-path-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.upload-conclusion {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 14px;
  padding: 10px 12px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
}
.upload-tabs {
  margin-top: 10px;
}
.upload-manifest,
.upload-network,
.upload-process-log {
  margin-top: 10px;
  padding: 10px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
}
.upload-log-content {
  max-height: 320px;
  overflow: auto;
  padding: 8px;
  background: var(--el-fill-color-light);
  font-family: Consolas, Monaco, monospace;
  font-size: 11.5px;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
