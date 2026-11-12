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
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'

import {
  getDestinationProgress,
  listAdminTasks,
  type AdminLogCollectTask,
  type AdminTaskFilters,
  type AdminTaskSummary,
  type DestinationProgress,
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

function formatBytes(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return '—'
  if (value < 1024) return `${value} B`
  const units = ['KiB', 'MiB', 'GiB', 'TiB']
  let size = value / 1024
  let index = 0
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024
    index += 1
  }
  return `${size.toFixed(1)} ${units[index]}`
}

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

function openDetail(task: AdminLogCollectTask) {
  detailTask.value = task
  detailVisible.value = true
}

async function copyPath(value: string | null) {
  if (!value) return
  try {
    await navigator.clipboard.writeText(value)
    ElMessage.success('路径已复制')
  } catch {
    ElMessage.warning('浏览器不允许复制，请手动选中路径文本')
  }
}

const summaryCells = computed(() => {
  const s = summary.value
  return [
    { label: '总数', value: s?.total ?? 0 },
    { label: '传输中', value: s?.transferring ?? 0 },
    { label: '已交付', value: s?.delivered ?? 0 },
    { label: '未核验', value: s?.unverified ?? 0 },
    { label: '失败', value: s?.failed ?? 0 },
  ]
})
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
        <el-table-column label="采集状态 / 进度" width="130">
          <template #default="{ row }">
            <div>{{ STATUS_TEXT[row.status] ?? row.status }} {{ row.progress }}%</div>
            <el-progress :percentage="row.progress" :show-text="false" :stroke-width="3" />
          </template>
        </el-table-column>
        <el-table-column label="日志服务器真实进度" min-width="240">
          <template #default="{ row }"><span class="upload-sub">{{ progressText(row) }}</span></template>
        </el-table-column>
        <el-table-column label="包大小" width="100">
          <template #default="{ row }">{{ formatBytes(row.archive_size) }}</template>
        </el-table-column>
        <el-table-column label="交付结论" width="100">
          <template #default="{ row }">
            <el-tag size="small" :type="row.delivery_status === 'delivered' ? 'success' : 'info'">
              {{ DELIVERY_TEXT[row.delivery_status] ?? row.delivery_status }}
            </el-tag>
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
</style>
