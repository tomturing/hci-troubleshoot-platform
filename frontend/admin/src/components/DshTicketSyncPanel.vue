<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import {
  cancelSyncJob,
  cleanupKindText,
  createSyncJob,
  errorText,
  fetchCleanupEvents,
  fetchPercent,
  fetchSyncArtifacts,
  fetchSyncJob,
  isRunning,
  mib,
  phaseText,
  TICKET_ID_PATTERN,
  type CleanupEvent,
  type CleanupThresholds,
  type SyncArtifact,
  type SyncJob,
} from '@/api/ticketSync'

// 工单同步服务只有一套；旧 DSH 与离线 DSH 页面共用本组件和同一组 API。
const POLL_INTERVAL_MS = 2000
const ticketInput = ref('')
const activeTicket = ref('')
const job = ref<SyncJob | null>(null)
const artifacts = ref<SyncArtifact[]>([])
const artifactQuery = ref('')
const syncBusy = ref(false)
const syncTip = ref('')
const syncRoot = ref('')
const cleanupDialogVisible = ref(false)
const cleanupEvents = ref<CleanupEvent[]>([])
const cleanupThresholds = ref<CleanupThresholds | null>(null)
const cleanupLoading = ref(false)
let pollTimer: number | null = null
let artifactPollTimer: number | null = null

const running = computed(() => isRunning(job.value?.phase))
const percent = computed(() => (job.value ? fetchPercent(job.value) : 0))
const filteredArtifacts = computed(() => {
  const query = artifactQuery.value.trim().toLowerCase()
  if (!query) return artifacts.value
  return artifacts.value.filter((artifact) => artifact.ticketId.toLowerCase().includes(query))
})
const tagType = computed(() => {
  const phase = job.value?.phase
  if (phase === 'ready') return 'success'
  if (phase === 'failed') return 'danger'
  if (phase === 'cancelled' || phase === 'interrupted') return 'info'
  return 'warning'
})

function stopPolling() {
  if (pollTimer !== null) {
    window.clearInterval(pollTimer)
    pollTimer = null
  }
}

function startPolling() {
  stopPolling()
  pollTimer = window.setInterval(refresh, POLL_INTERVAL_MS)
}

function stopArtifactPolling() {
  if (artifactPollTimer !== null) {
    window.clearInterval(artifactPollTimer)
    artifactPollTimer = null
  }
}

function startArtifactPolling() {
  stopArtifactPolling()
  artifactPollTimer = window.setInterval(refreshArtifacts, POLL_INTERVAL_MS)
}

function artifactTagType(artifact: SyncArtifact) {
  if (artifact.phase === 'ready') return 'success'
  if (artifact.phase === 'failed') return 'danger'
  if (artifact.phase === 'cancelled' || artifact.phase === 'interrupted') return 'info'
  if (artifact.phase === 'cleaned') return 'info'
  return 'warning'
}

function artifactPath(ticketId: string): string {
  return syncRoot.value ? `${syncRoot.value}/${ticketId}` : ticketId
}

function cleanupTime(artifact: SyncArtifact): string {
  if (!artifact.cleanedAt) return ''
  const date = new Date(artifact.cleanedAt)
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString()
}

function gib(bytes: number | null | undefined): string {
  if (!bytes || bytes <= 0) return '—'
  return `${(bytes / 1024 ** 3).toFixed(1)} GiB`
}

function artifactPercent(artifact: SyncArtifact) {
  return artifact.job ? fetchPercent(artifact.job) : 0
}

async function refreshArtifacts() {
  try {
    const latest = await fetchSyncArtifacts()
    artifacts.value = latest.items
    if (latest.root) syncRoot.value = latest.root
    const selected = latest.items.find((item) => item.ticketId === activeTicket.value)
    if (selected?.job && selected.ticketId === activeTicket.value) job.value = selected.job
  } catch {
    // DSH 页面仍可使用；下一次轮询继续恢复。
  }
}

async function refresh() {
  const ticketId = activeTicket.value
  if (!ticketId) return
  try {
    const latest = await fetchSyncJob(ticketId)
    if (activeTicket.value !== ticketId) return
    job.value = latest
    if (latest === null || !isRunning(latest.phase)) stopPolling()
  } catch {
    // 单次查询失败不终止轮询。
  }
}

function handleApiError(error: any) {
  if (error?.response?.status === 409) {
    syncTip.value = '该工单正在同步中，请等待完成'
    return
  }
  const code = error?.response?.data?.error
  syncTip.value = code
    ? errorText(code, error?.response?.data?.message ?? null)
    : '无法连接同步服务，请确认宿主机上的同步服务已启动'
}

async function startSync() {
  const ticketId = ticketInput.value.trim()
  if (!TICKET_ID_PATTERN.test(ticketId)) {
    syncTip.value = '工单号格式错误，应为 Q + 13 位数字'
    return
  }
  syncTip.value = ''
  syncBusy.value = true
  stopPolling()
  activeTicket.value = ticketId
  job.value = null
  try {
    await createSyncJob(ticketId)
    await refresh()
    await refreshArtifacts()
    if (running.value) startPolling()
  } catch (error: any) {
    handleApiError(error)
  } finally {
    syncBusy.value = false
  }
}

async function cancelSync() {
  const ticketId = activeTicket.value
  if (!ticketId) return
  syncBusy.value = true
  try {
    await cancelSyncJob(ticketId)
    await refresh()
    await refreshArtifacts()
  } catch (error: any) {
    handleApiError(error)
  } finally {
    syncBusy.value = false
  }
}

async function cancelArtifact(artifact: SyncArtifact) {
  if (!isRunning(artifact.phase)) return
  syncBusy.value = true
  try {
    await cancelSyncJob(artifact.ticketId)
    await refreshArtifacts()
    if (activeTicket.value === artifact.ticketId) await refresh()
  } catch (error: any) {
    handleApiError(error)
  } finally {
    syncBusy.value = false
  }
}

async function openCleanupEvents() {
  cleanupDialogVisible.value = true
  cleanupLoading.value = true
  try {
    const data = await fetchCleanupEvents(100)
    cleanupEvents.value = data.items
    cleanupThresholds.value = data.thresholds ?? null
  } catch {
    cleanupEvents.value = []
    cleanupThresholds.value = null
  } finally {
    cleanupLoading.value = false
  }
}

onMounted(() => {
  refreshArtifacts()
  startArtifactPolling()
})

onUnmounted(() => {
  stopPolling()
  stopArtifactPolling()
})
</script>

<template>
  <el-card class="sync-card">
    <div class="sync-bar">
      <span class="sync-title">工单日志同步</span>
      <el-input
        v-model="ticketInput"
        class="sync-input"
        placeholder="Q + 13 位数字，例如 Q2026052801304"
        :disabled="running"
        clearable
        @keyup.enter="startSync"
      />
      <el-button type="primary" :loading="syncBusy" :disabled="running" @click="startSync">同步日志</el-button>
      <el-button v-if="running" type="danger" plain @click="cancelSync">取消</el-button>
    </div>

    <div v-if="job" class="sync-status">
      <el-tag :type="tagType" size="small">{{ phaseText(job.phase) }}</el-tag>
      <span class="sync-meta">工单 {{ job.ticketId }}</span>
      <template v-if="job.phase === 'fetching'">
        <el-progress v-if="job.totalBytes > 0" :percentage="percent" :stroke-width="10" class="sync-progress" />
        <span class="sync-meta">
          {{ mib(job.receivedBytes) }}<template v-if="job.totalBytes > 0"> / {{ mib(job.totalBytes) }}</template>
        </span>
      </template>
      <template v-else-if="job.phase === 'extracting'">
        <span class="sync-meta">已解压 {{ mib(job.extractedBytes) }} · {{ job.fileCount }} 个文件</span>
        <span v-if="job.currentArchive" class="sync-archive">{{ job.currentArchive }}</span>
      </template>
      <span v-else-if="job.phase === 'ready'" class="sync-meta">
        共 {{ job.fileCount }} 个文件 · {{ mib(job.extractedBytes) }}，可在下方 DSH 中选择该工单目录
        <em v-if="job.message" class="sync-warning">（{{ job.message }}）</em>
      </span>
      <span v-else-if="job.phase === 'failed'" class="sync-error">{{ errorText(job.errorCode, job.message) }}</span>
    </div>

    <div class="artifact-panel">
      <div class="artifact-panel-header">
        <span class="artifact-panel-title">本地工单目录</span>
        <span class="artifact-panel-tip">自动同步每 30 秒检查，页面每 2 秒刷新状态</span>
        <el-input v-model="artifactQuery" class="artifact-search" size="small" clearable placeholder="搜索工单号" />
        <el-button text type="primary" size="small" @click="openCleanupEvents">清理记录</el-button>
        <el-button text type="primary" size="small" @click="refreshArtifacts">刷新</el-button>
      </div>

      <el-empty v-if="artifacts.length === 0" :image-size="56" description="暂无已同步或进行中的工单" />
      <el-empty v-else-if="filteredArtifacts.length === 0" :image-size="56" description="没有匹配的工单" />
      <div v-else class="artifact-list">
        <div v-for="artifact in filteredArtifacts" :key="artifact.ticketId" class="artifact-item">
          <div class="artifact-item-header">
            <div class="artifact-item-title">
              <el-tag :type="artifactTagType(artifact)" size="small">{{ phaseText(artifact.phase) }}</el-tag>
              <span>工单 {{ artifact.ticketId }}</span>
            </div>
            <el-button
              v-if="isRunning(artifact.phase)"
              type="danger"
              link
              size="small"
              :loading="syncBusy && activeTicket === artifact.ticketId"
              @click="cancelArtifact(artifact)"
            >
              取消
            </el-button>
          </div>
          <div v-if="artifact.directoryReady" class="artifact-path">目录：{{ artifactPath(artifact.ticketId) }}</div>
          <div v-if="artifact.phase === 'queued'" class="artifact-detail">等待同步任务执行</div>
          <div v-else-if="artifact.phase === 'fetching'" class="artifact-detail artifact-progress-row">
            <el-progress
              v-if="artifact.job && artifact.job.totalBytes > 0"
              :percentage="artifactPercent(artifact)"
              :stroke-width="8"
              class="artifact-progress"
            />
            <span v-if="artifact.job" class="artifact-detail">
              同步中 {{ mib(artifact.job.receivedBytes) }}<template v-if="artifact.job.totalBytes > 0"> / {{ mib(artifact.job.totalBytes) }}</template>
            </span>
          </div>
          <div v-else-if="artifact.phase === 'extracting'" class="artifact-detail">
            解压中<template v-if="artifact.job"> · {{ mib(artifact.job.extractedBytes) }} · {{ artifact.job.fileCount }} 个文件<span v-if="artifact.job.currentArchive" class="artifact-archive">{{ artifact.job.currentArchive }}</span></template>
          </div>
          <div v-else-if="artifact.phase === 'ready'" class="artifact-detail artifact-success">
            已同步并解压，可在下方 DSH 文件选择器中使用<span v-if="artifact.job?.message" class="sync-warning">（{{ artifact.job.message }}）</span>
          </div>
          <div v-else-if="artifact.phase === 'failed'" class="artifact-detail artifact-error">
            {{ errorText(artifact.job?.errorCode ?? null, artifact.job?.message ?? null) }}<span v-if="artifact.directoryReady">，本地已有目录仍可使用</span>
          </div>
          <div v-else-if="artifact.phase === 'cleaned'" class="artifact-detail">
            <template v-if="cleanupTime(artifact)">已于 {{ cleanupTime(artifact) }} 清理</template><template v-else>产物已清理</template><span class="artifact-cleaned-hint">；原件仍在日志源服务器上，可重新同步</span>
          </div>
          <div v-else class="artifact-detail">任务已取消</div>
        </div>
      </div>
    </div>

    <el-alert v-if="syncTip" type="warning" :closable="false" class="sync-tip" :description="syncTip" />
    <el-dialog v-model="cleanupDialogVisible" title="自动清理记录" width="760px">
      <div v-loading="cleanupLoading" class="cleanup-dialog-body">
        <div v-if="cleanupThresholds" class="cleanup-thresholds">
          <span>保留期 {{ cleanupThresholds.retentionDays }} 天（判据为同步完成时刻，非工单日期）</span>
          <span>最短保留 {{ cleanupThresholds.minKeepDays }} 天</span>
          <span>余量软阈值 {{ gib(cleanupThresholds.evictFreeBytes) }}</span>
          <span>余量硬阈值 {{ gib(cleanupThresholds.minFreeBytes) }}</span>
          <span>产物总量上限 {{ gib(cleanupThresholds.maxTotalBytes) }}</span>
        </div>
        <el-empty v-if="!cleanupLoading && cleanupEvents.length === 0" :image-size="48" description="暂无清理记录（每 30 秒检查一次，无动作时不写流水）" />
        <el-table v-else :data="cleanupEvents" size="small" max-height="420">
          <el-table-column prop="occurredAt" label="时间" width="170"><template #default="{ row }">{{ new Date(row.occurredAt).toLocaleString() }}</template></el-table-column>
          <el-table-column label="动作" width="130"><template #default="{ row }">{{ cleanupKindText(row.kind) }}</template></el-table-column>
          <el-table-column prop="ticketId" label="工单" width="160"><template #default="{ row }">{{ row.ticketId || '—' }}</template></el-table-column>
          <el-table-column label="释放" width="95"><template #default="{ row }">{{ gib(row.bytes) }}</template></el-table-column>
          <el-table-column label="当时余量" width="105"><template #default="{ row }">{{ gib(row.freeBytes) }}</template></el-table-column>
          <el-table-column prop="detail" label="依据" show-overflow-tooltip />
        </el-table>
      </div>
    </el-dialog>
  </el-card>
</template>

<style scoped>
.sync-card { border-radius: 8px; margin-bottom: 12px; }
.sync-bar { display: flex; align-items: center; gap: 12px; }
.sync-title { font-size: 14px; color: #606266; font-weight: 500; flex: none; }
.sync-input { max-width: 320px; }
.sync-status { display: flex; align-items: center; gap: 12px; margin-top: 12px; }
.sync-meta { font-size: 13px; color: #606266; }
.sync-archive { font-size: 12px; color: #909399; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 420px; }
.sync-error { font-size: 13px; color: #f56c6c; }
.sync-warning { font-size: 13px; font-style: normal; color: #e6a23c; }
.artifact-panel { margin-top: 16px; border-top: 1px solid #ebeef5; padding-top: 12px; }
.artifact-panel-header { display: flex; align-items: center; gap: 10px; }
.artifact-panel-title { font-size: 13px; color: #303133; font-weight: 600; }
.artifact-panel-tip { flex: 1; font-size: 12px; color: #909399; }
.artifact-search { width: 220px; }
.artifact-list { display: grid; gap: 8px; margin-top: 10px; max-height: 260px; overflow: auto; }
.artifact-item { padding: 10px 12px; border: 1px solid #ebeef5; border-radius: 6px; background: #fafafa; }
.artifact-item-header { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.artifact-item-title { display: flex; align-items: center; gap: 8px; font-size: 13px; color: #303133; font-weight: 500; }
.artifact-path, .artifact-detail { margin-top: 6px; font-size: 12px; color: #606266; }
.artifact-path { color: #909399; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.artifact-progress-row { display: flex; align-items: center; gap: 10px; }
.artifact-progress { width: 180px; }
.artifact-archive { display: block; margin-top: 4px; color: #909399; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 680px; }
.artifact-success { color: #67c23a; }
.artifact-error { color: #f56c6c; }
.artifact-cleaned-hint { color: #909399; }
.cleanup-dialog-body { min-height: 120px; }
.cleanup-thresholds { display: flex; flex-wrap: wrap; gap: 8px 18px; margin-bottom: 12px; padding: 8px 12px; background: #f5f7fa; border-radius: 4px; color: #606266; font-size: 12px; line-height: 1.6; }
.sync-progress { width: 220px; }
.sync-tip { margin-top: 12px; }
</style>
