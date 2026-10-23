<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { createApiClient } from '@hci/shared'
import { getAccessToken } from '@/utils/auth'
import {
  PHASE_TEXT,
  TICKET_ID_PATTERN,
  cancelSyncJob,
  createSyncJob,
  errorText,
  fetchPercent,
  fetchSyncJob,
  isRunning,
  mib,
  type SyncJob,
} from '@/api/ticketSync'

// 管理端 axios 客户端注入登录 JWT（axios 不经 window.fetch 全局拦截器，须显式传入）
const apiClient = createApiClient('/api', undefined, getAccessToken)

// ==========================================
// DSH（DeepSeek Harness）诊断工作台
// ==========================================
// 与 Grafana / Langfuse 的关键差异：DSH 没有匿名访问模式，进入其 Web UI 必须
// 携带一次性的 launch token 完成会话交换，而该 token 由 DSH 进程随机生成、
// 每次重建都会变化，因此不能像 Grafana 那样在前端按 hostname 推导，
// 必须向后端（经管理员 JWT 鉴权）实时索取。
const dshUrl = ref('')
const dshReady = ref(false)
const dshLoading = ref(true)
const dshError = ref('')

async function loadDshUrl() {
  dshLoading.value = true
  dshError.value = ''
  try {
    const res = await apiClient.get('/dsh/launch')
    dshUrl.value = res.data.url
    dshReady.value = true
  } catch (error: any) {
    const status = error?.response?.status
    if (status === 503) {
      // DSH 进程刚重建时尚未打印就绪行、token 还没落盘，属于可恢复的中间态
      dshError.value = 'DSH 服务正在启动，尚未生成会话凭据，请稍后点击「重新连接」。'
    } else if (status === 403) {
      dshError.value = '当前账号不具备管理员权限，无法取得 DSH 会话凭据。'
    } else {
      dshError.value = '未能取得 DSH 会话入口，请稍后重试。'
    }
    // 保留旧地址不清空：让 iframe 继续展示上一次的会话，避免页面整体塌陷
    dshReady.value = Boolean(dshUrl.value)
  } finally {
    dshLoading.value = false
  }
}

onMounted(loadDshUrl)

// ==========================================
// 工单日志同步
// ==========================================
// 服务端状态全在内存，一次同步大约 6 秒（50 MiB 工单），因此用 2s 轮询即可，
// 不必上 SSE。轮询只在任务推进期间开，结束后立刻停，不留空转定时器。
const POLL_INTERVAL_MS = 2000

const ticketInput = ref('')
const activeTicket = ref('')
const job = ref<SyncJob | null>(null)
const syncBusy = ref(false)
const syncTip = ref('')
let pollTimer: number | null = null

const running = computed(() => isRunning(job.value?.phase))
const percent = computed(() => (job.value ? fetchPercent(job.value) : 0))
const tagType = computed(() => {
  const phase = job.value?.phase
  if (phase === 'ready') return 'success'
  if (phase === 'failed') return 'danger'
  if (phase === 'cancelled') return 'info'
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

async function refresh() {
  const ticketId = activeTicket.value
  if (!ticketId) return
  try {
    const latest = await fetchSyncJob(ticketId)
    // 用户已切换工单时丢弃过期响应，否则旧任务会覆盖新任务的进度
    if (activeTicket.value !== ticketId) return
    job.value = latest
    // 服务重启后任务表为空（404 → null），此时没有可跟进的对象
    if (latest === null || !isRunning(latest.phase)) stopPolling()
  } catch {
    // 单次查询失败不终止轮询：宿主机短暂不可达之后仍可能恢复
  }
}

function handleApiError(error: any) {
  const status = error?.response?.status
  if (status === 409) {
    syncTip.value = '该工单正在同步中，请等待完成'
    return
  }
  const code = error?.response?.data?.error
  if (code) {
    syncTip.value = errorText(code, error?.response?.data?.message ?? null)
    return
  }
  syncTip.value = '无法连接同步服务，请确认宿主机上的同步服务已启动'
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
    // 受理后立即取一次状态，不必先等满一个轮询周期
    await refresh()
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
  } catch (error: any) {
    handleApiError(error)
  } finally {
    syncBusy.value = false
  }
}

onUnmounted(stopPolling)
</script>

<template>
  <div class="dsh-container">
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
        <el-button type="primary" :loading="syncBusy" :disabled="running" @click="startSync">
          同步日志
        </el-button>
        <el-button v-if="running" type="danger" plain @click="cancelSync">取消</el-button>
      </div>

      <div v-if="job" class="sync-status">
        <el-tag :type="tagType" size="small">{{ PHASE_TEXT[job.phase] }}</el-tag>
        <span class="sync-meta">工单 {{ job.ticketId }}</span>

        <template v-if="job.phase === 'fetching'">
          <el-progress
            v-if="job.totalBytes > 0"
            :percentage="percent"
            :stroke-width="10"
            class="sync-progress"
          />
          <span class="sync-meta">
            {{ mib(job.receivedBytes) }}
            <template v-if="job.totalBytes > 0"> / {{ mib(job.totalBytes) }}</template>
          </span>
        </template>

        <template v-else-if="job.phase === 'extracting'">
          <span class="sync-meta">已解压 {{ mib(job.extractedBytes) }} · {{ job.fileCount }} 个文件</span>
          <span v-if="job.currentArchive" class="sync-archive">{{ job.currentArchive }}</span>
        </template>

        <span v-else-if="job.phase === 'ready'" class="sync-meta">
          共 {{ job.fileCount }} 个文件 · {{ mib(job.extractedBytes) }}，可在下方 DSH 中选择该工单目录
        </span>

        <span v-else-if="job.phase === 'failed'" class="sync-error">
          {{ errorText(job.errorCode, job.message) }}
        </span>
      </div>

      <el-alert
        v-if="syncTip"
        type="warning"
        :closable="false"
        class="sync-tip"
        :description="syncTip"
      />
    </el-card>

    <el-card class="dsh-card">
      <div v-loading="dshLoading" class="dsh-content">
        <div class="dsh-actions-bar">
          <span class="dsh-info-text">嵌入式 DSH 诊断工作台（DeepSeek Harness）</span>
          <el-button type="primary" size="small" @click="loadDshUrl">
            <el-icon style="margin-right: 4px"><Refresh /></el-icon>
            重新连接
          </el-button>
        </div>
        <div class="iframe-wrapper">
          <!--
            referrerpolicy=no-referrer：会话入口地址内含 DSH 的 launch token，
            该凭据等价于 Agent 的完整控制权，必须阻止浏览器经 Referer 头
            把它带给任何第三方。
          -->
          <iframe
            v-if="dshReady"
            :src="dshUrl"
            referrerpolicy="no-referrer"
            width="100%"
            height="100%"
            frameborder="0"
            allowfullscreen
          />
        </div>
        <el-alert
          v-if="dshError"
          type="warning"
          :closable="false"
          class="dsh-alert"
          :description="dshError"
        />
        <el-alert
          v-else
          type="info"
          :closable="false"
          class="dsh-alert"
          description="DSH 以只读方式挂载宿主机 /sf/log，用于分析客户 HCI 故障日志压缩包。会话凭据由管理端下发且具备完整操作权限，请勿向外部分享页面地址。"
        />
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.dsh-container {
  padding: 20px;
}

.dsh-card {
  border-radius: 8px;
}

/* 同步卡片与 DSH 卡片同宽，只多一条操作条，不抢占 iframe 的竖向空间 */
.sync-card {
  border-radius: 8px;
  margin-bottom: 12px;
}

.sync-bar {
  display: flex;
  align-items: center;
  gap: 12px;
}

.sync-title {
  font-size: 14px;
  color: #606266;
  font-weight: 500;
  flex: none;
}

.sync-input {
  max-width: 320px;
}

.sync-status {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 12px;
}

.sync-meta {
  font-size: 13px;
  color: #606266;
}

.sync-archive {
  font-size: 12px;
  color: #909399;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 420px;
}

.sync-error {
  font-size: 13px;
  color: #f56c6c;
}

.sync-progress {
  width: 220px;
}

.sync-tip {
  margin-top: 12px;
}

.dsh-content {
  min-height: 620px;
}

/* 操作条样式与 ObservabilityView 保持一致，保证视觉连贯 */
.dsh-actions-bar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 16px;
  background-color: #f5f7fa;
  border-radius: 6px;
  border-left: 4px solid #409eff;
}

.dsh-info-text {
  font-size: 14px;
  color: #606266;
  font-weight: 500;
}

.iframe-wrapper {
  width: 100%;
  height: calc(100vh - 375px);
  min-height: 480px;
  margin-top: 12px;
  border: 1px solid #dcdfe6;
  border-radius: 6px;
  overflow: hidden;
  box-shadow: 0 2px 12px 0 rgba(0, 0, 0, 0.05);
}

.iframe-wrapper iframe {
  display: block;
}

.dsh-alert {
  margin-top: 12px;
}
</style>
