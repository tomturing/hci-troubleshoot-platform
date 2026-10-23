<script setup lang="ts">
/**
 * 日志收集面板（acli log_collect + P1/P3 交付编排）。
 *
 * 三条不可协商的约束（决定了下面的流程）：
 *  1. 执行只能发生在 bridge 的 SSH 会话内，且复用终端已建立的连接；
 *  2. 20 GiB 不走浏览器：分片由 bridge 直传 `/api/direct/...`；
 *  3. 平台不做任何自动清理：结束后只提示路径与命令，由用户自行清理。
 */
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  buildAcliSyncMessage,
  buildLogCollectMessage,
  createBridgeSocket,
  resolveBridgeUrl,
} from '@/api/terminal'
import {
  logCollectApi,
  type LogCollectModule,
  type LogCollectTask,
  type RelayStatus,
} from '@/api/logCollect'
import { useChatStore } from '@/stores/chat'

const props = defineProps<{ caseId: string; nodeIp?: string }>()
const visible = defineModel<boolean>({ required: true })
const chatStore = useChatStore()

const RELAY_URL = 'http://acli.sangfor.com.cn:5888'
const POLL_INTERVAL = 5000

const MODULES: Array<{ value: LogCollectModule; label: string }> = [
  { value: 'vm_suspend', label: '虚拟机挂起/关机异常' },
  { value: 'vm_start', label: '虚拟机启动异常' },
  { value: 'host_offline', label: '主机离线' },
  { value: 'slowdisk', label: '存储卡慢盘' },
  { value: 'storage', label: '虚拟存储异常' },
  { value: 'network', label: '网络异常' },
  { value: 'performance', label: '性能问题' },
  { value: 'system', label: '中台/管理面' },
  { value: 'core', label: '宕机 core 文件' },
  { value: 'other', label: '其他（通用采集）' },
]

type Stage =
  | 'idle'
  | 'probing'
  | 'installing'
  | 'creating'
  | 'collecting'
  | 'pulling'
  | 'uploading'
  | 'relaying'
  | 'done'
  | 'failed'

interface ProbeResult {
  architecture: string
  acli_installed: boolean
  acli_version: string
  available_mb: number
  log_server_reachable: boolean
  http_code: number
}

interface ProgressSnapshot {
  status: string
  percent: number
  done: number
  total: number
  archive_path: string
  upload_state: string
  hosts: Array<{ ip: string; status: string; percent: number }>
}

const stage = ref<Stage>('idle')
const probeResult = ref<ProbeResult | null>(null)
const pathMode = ref<'P1' | 'P3' | ''>('')
const currentTask = ref<LogCollectTask | null>(null)
const progress = ref<ProgressSnapshot | null>(null)
const transfer = reactive({ stage: '', percent: 0, pulled: 0, uploadedParts: 0, partCount: 0, sha256: '' })
const relayStatus = ref<RelayStatus>('none')
const cleanupTip = ref('')
const errorMessage = ref('')
// 运行令牌：每次启动 / 取消都自增，后台轮询据此退出，避免取消后仍继续写状态
let runToken = 0

const form = reactive({
  modules: ['vm_suspend'] as LogCollectModule[],
  dateRange: [] as string[],
  ips: 'all',
  // 发起节点：acli progress 只在该节点有效，后续轮询必须复用同一节点
  nodeIp: props.nodeIp || '',
})

const running = computed(() =>
  ['probing', 'installing', 'creating', 'collecting', 'pulling', 'uploading', 'relaying'].includes(stage.value),
)

const percent = computed(() => {
  if (stage.value === 'done') return 100
  if (stage.value === 'relaying') return 95 + Math.min(4, relayStatus.value === 'running' ? 2 : 0)
  if (stage.value === 'uploading' || stage.value === 'pulling') return 60 + Math.round(transfer.percent * 0.3)
  return Math.min(60, progress.value?.percent ?? 0) * (60 / 80)
})

const progressStatus = computed(() =>
  stage.value === 'failed' ? 'exception' : stage.value === 'done' ? 'success' : undefined,
)

const stepActive = computed(() => {
  if (stage.value === 'done') return 4
  if (stage.value === 'relaying') return 3
  if (stage.value === 'uploading' || stage.value === 'pulling') return 2
  if (stage.value === 'collecting' || stage.value === 'creating') return 1
  return 0
})

const statusText = computed(() => {
  if (stage.value === 'failed') return errorMessage.value || currentTask.value?.error_message || '任务失败'
  if (stage.value === 'done') return '日志已归档到日志服务器'
  if (stage.value === 'collecting') {
    const p = progress.value
    return p ? `${p.status}（${p.done}/${p.total}）` : '采集进行中'
  }
  if (stage.value === 'pulling') return `正在从 HCI 拉取：${transfer.pulled} 字节`
  if (stage.value === 'uploading') return `正在上传平台：${transfer.uploadedParts}/${transfer.partCount} 片`
  if (stage.value === 'relaying') return `正在归档到日志服务器：${relayStatus.value}`
  return ''
})

// ── WebSocket ───────────────────────────────────────────────────────────────

let socket: WebSocket | null = null
const waiters = new Map<string, (data: any) => void>()

function openSocket(): Promise<WebSocket> {
  if (socket && socket.readyState === WebSocket.OPEN) return Promise.resolve(socket)
  return resolveBridgeUrl().then(
    () =>
      new Promise<WebSocket>((resolve, reject) => {
        const ws = createBridgeSocket()
        const timer = setTimeout(() => {
          ws.close()
          reject(new Error('连接 terminal_bridge 超时'))
        }, 8000)
        ws.onopen = () => {
          clearTimeout(timer)
          socket = ws
          resolve(ws)
        }
        ws.onerror = () => {
          clearTimeout(timer)
          reject(new Error('无法连接 terminal_bridge，请确认本地服务已启动'))
        }
        ws.onmessage = handleMessage
      }),
  )
}

function handleMessage(event: MessageEvent) {
  let data: any
  try {
    data = JSON.parse(event.data)
  } catch {
    return
  }
  if (data.type === 'acli_sync_result') {
    const waiter = waiters.get('acli_sync')
    if (waiter) {
      waiters.delete('acli_sync')
      waiter(data)
    }
    return
  }
  if (data.type !== 'log_collect_result') return
  const action = data.log_action
  // transfer 是持续事件流（拉取/上传各阶段多次回包），单独处理
  if (action === 'transfer') {
    const payload = data.log_data || {}
    transfer.stage = payload.stage || ''
    transfer.percent = payload.percent || 0
    transfer.pulled = payload.pulled_bytes || 0
    transfer.uploadedParts = payload.uploaded_parts || 0
    transfer.partCount = payload.part_count || 0
    transfer.sha256 = payload.sha256 || transfer.sha256
    if (payload.stage === 'pulling') stage.value = 'pulling'
    if (payload.stage === 'uploading') stage.value = 'uploading'
    if (payload.stage === 'done') {
      const waiter = waiters.get('transfer')
      if (waiter) {
        waiters.delete('transfer')
        waiter(data)
      }
    }
    if (data.status === 'error') {
      const waiter = waiters.get('transfer')
      if (waiter) {
        waiters.delete('transfer')
        waiter(data)
      }
    }
    return
  }
  const waiter = waiters.get(action)
  if (waiter) {
    waiters.delete(action)
    waiter(data)
  }
}

function sendAndWait(
  ws: WebSocket,
  action: 'probe' | 'create' | 'progress' | 'cancel' | 'stat' | 'transfer' | 'acli_sync',
  options: Record<string, unknown> = {},
  timeoutMs = 60000,
): Promise<any> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      waiters.delete(action)
      reject(new Error(`${action} 超时（${Math.round(timeoutMs / 1000)}s）`))
    }, timeoutMs)
    waiters.set(action, (data) => {
      clearTimeout(timer)
      resolve(data)
    })
    if (action === 'acli_sync') {
      ws.send(buildAcliSyncMessage(props.caseId, { force: false, minDiskMb: 100 }))
    } else {
      ws.send(
        buildLogCollectMessage(props.caseId, action, { ...(options as any), nodeIp: props.nodeIp }),
      )
    }
  })
}

// ── 主流程 ──────────────────────────────────────────────────────────────────

async function handleStart() {
  if (!form.dateRange || form.dateRange.length !== 2) {
    ElMessage.warning('请选择时间范围')
    return
  }

  const nodeIp = (form.nodeIp || props.nodeIp || '').trim()
  if (!nodeIp) {
    ElMessage.warning('请填写发起节点地址（需与 SSH 终端连接的节点一致）')
    return
  }

  cleanupTip.value = ''
  errorMessage.value = ''
  transfer.sha256 = ''
  currentTask.value = null
  progress.value = null
  relayStatus.value = 'none'
  // 校验通过后回填：后续所有 bridge 动作都必须锚定同一节点，否则会落到默认会话
  form.nodeIp = nodeIp
  const token = ++runToken

  let ws: WebSocket
  try {
    ws = await openSocket()
  } catch (error: any) {
    ElMessage.error(error.message)
    return
  }

  const modules = form.modules.length ? form.modules : (['other'] as LogCollectModule[])
  stage.value = 'probing'
  try {
    const probe = await sendAndWait(ws, 'probe', { remoteUrl: RELAY_URL, nodeIp }, 60000)
    if (probe.status !== 'ok') throw new Error(probe.message || '环境探测失败，请确认已在 SSH 终端连接 HCI')
    probeResult.value = probe.log_data as ProbeResult

    if (!probeResult.value.acli_installed) {
      await confirmInstall(ws)
    }

    pathMode.value = probeResult.value.log_server_reachable ? 'P1' : 'P3'

    const task = await logCollectApi.createTask({
      case_id: props.caseId,
      // 不传 ticket_no：服务端强制 ticket_no == case_id，避免出现两套工单号
      origin_node_ip: nodeIp,
      modules,
      date_from: form.dateRange[0],
      date_to: form.dateRange[1],
      ips: form.ips.split(',').map((s) => s.trim()).filter(Boolean),
      remote_url: pathMode.value === 'P1' ? RELAY_URL : undefined,
      trace_id: `lc-${props.caseId}-${Date.now()}`,
    })
    currentTask.value = task

    stage.value = 'creating'
    const created = await sendAndWait(
      ws,
      'create',
      {
        nodeIp,
        // acli -s 与任务归属使用同一个平台工单号
        ticketNo: props.caseId,
        modules,
        dateFrom: form.dateRange[0],
        dateTo: form.dateRange[1],
        ips: form.ips.replace(/\s+/g, ''),
        remoteUrl: pathMode.value === 'P1' ? RELAY_URL : undefined,
      },
      180000,
    )
    if (created.status !== 'ok') throw new Error(created.message || '任务下发失败')

    // 平台任务必须随阶段推进：pending 不能直接跳到终态，否则 PATCH 会被状态机拒绝
    stage.value = 'collecting'
    currentTask.value = await logCollectApi.patchTask(task.task_id, { status: 'collecting' })
    const collected = await pollUntilCollected(ws, token)
    if (token !== runToken) return
    if (!collected) throw new Error('采集未完成')

    if (pathMode.value === 'P1') {
      await finishP1(task)
    } else {
      await runP3(ws, task, token)
    }
  } catch (error: any) {
    // 用户已取消：后台流程静默退出，不再覆盖任务状态
    if (token !== runToken) return
    errorMessage.value = String(error?.message || error).slice(0, 500)
    // 轮询超时属于"结果未知"：不替服务端写终态，否则会出现 UI 报失败、后台实际成功
    if (error instanceof RelayPendingError) {
      stage.value = 'failed'
      ElMessage.warning(errorMessage.value)
      return
    }
    stage.value = 'failed'
    if (currentTask.value) {
      await logCollectApi
        .patchTask(currentTask.value.task_id, {
          status: 'failed',
          error_message: errorMessage.value,
        })
        .catch(() => undefined)
    }
    ElMessage.error(errorMessage.value || '日志收集失败')
  }
}

async function confirmInstall(ws: WebSocket) {
  await ElMessageBox.confirm(
    `当前节点未检测到 acli（架构 ${probeResult.value?.architecture || '未知'}）。` +
      '安装会向集群所有节点分发插件，约 10–15 秒/节点，属于变更操作。是否继续？',
    '需要安装 acli',
    { confirmButtonText: '确认安装', cancelButtonText: '取消', type: 'warning' },
  )
  stage.value = 'installing'
  const result = await sendAndWait(ws, 'acli_sync', {}, 300000)
  if (result.status === 'error') throw new Error(result.message || 'acli 安装失败')
  probeResult.value = { ...(probeResult.value as ProbeResult), acli_installed: true }
}

async function pollUntilCollected(ws: WebSocket, token: number): Promise<boolean> {
  // 采集上限 1 小时（720 次 × 5s）：超时明确失败，避免面板无限转圈。
  for (let attempt = 0; attempt < 720; attempt++) {
    if (token !== runToken) return false
    const result = await sendAndWait(ws, 'progress', { nodeIp: form.nodeIp }, 60000)
    const snapshot = result.log_data as ProgressSnapshot | null
    if (snapshot) {
      progress.value = snapshot
      if (snapshot.percent >= 80 || snapshot.status.includes('完成')) return true
      if (snapshot.status.includes('失败')) return false
    }
    await sleep(POLL_INTERVAL)
  }
  return false
}

async function finishP1(task: LogCollectTask) {
  const snapshot = progress.value
  if (snapshot?.upload_state && !snapshot.upload_state.includes('成功')) {
    throw new Error(`日志服务器上传失败：${snapshot.upload_state}，可改用 P3 中转`)
  }
  await markCompleted(task.task_id)
  stage.value = 'done'
  cleanupTip.value = 'P1 由插件直传日志服务器；上传成功后插件会自动删除 HCI 上的压缩包，无需人工清理。'
  ElMessage.success('日志已归档到日志服务器')
}

/**
 * 回填任务终态并同步服务端返回的任务快照。
 *
 * 归档/上传已经由服务端确认，PATCH 只是状态回填：即使回填失败也不能把已成功的归档翻转成失败，
 * 因此这里只提示，不抛出。
 */
async function markCompleted(taskId: string) {
  try {
    currentTask.value = await logCollectApi.patchTask(taskId, { status: 'completed', progress: 100 })
  } catch {
    ElMessage.warning('日志已归档，但平台任务状态回填失败，请在任务列表确认')
  }
}

async function runP3(ws: WebSocket, task: LogCollectTask, token: number) {
  const archivePath = progress.value?.archive_path || ''
  if (!archivePath) throw new Error('未获取到压缩包路径，无法中转')

  // 1) 取包大小（init 需要 total_size）
  const statResult = await sendAndWait(ws, 'stat', { sourcePath: archivePath, nodeIp: form.nodeIp }, 30000)
  if (statResult.status !== 'ok') throw new Error(statResult.message || '读取压缩包大小失败')
  const totalSize = Number(statResult.log_data?.size || 0)
  if (totalSize <= 0) throw new Error('压缩包大小异常')

  // 2) 平台初始化分片会话（幂等，重试会返回已传分片）
  const session = await logCollectApi.initUpload(task.task_id, { total_size: totalSize })

  // 3) bridge 拉取 + 分块上传
  stage.value = 'pulling'
  currentTask.value = await logCollectApi.patchTask(task.task_id, { status: 'uploading' })
  const transferred = await sendAndWait(
    ws,
    'transfer',
    {
      nodeIp: form.nodeIp,
      sourcePath: archivePath,
      platformUrl: window.location.origin,
      uploadToken: session.upload_token,
      chunkSize: session.chunk_size,
      partCount: session.part_count,
      totalSize,
      uploadedParts: session.uploaded_parts,
    },
    2 * 3600 * 1000,
  )
  if (transferred.status !== 'ok') throw new Error(transferred.message || '日志包上传失败')

  // 4) 合并 + 校验
  const completed = await logCollectApi.completeUpload(task.task_id, {
    sha256: transfer.sha256 || undefined,
    size: totalSize,
  })

  // 5) 代传日志服务器（/relay 幂等启动，进度与终态一律以服务端 relay_status 为准）
  stage.value = 'relaying'
  await logCollectApi.startRelay(task.task_id)
  await pollRelay(task.task_id, token)
  if (token !== runToken) return

  // 归档成功才推进任务终态：否则任务会永久停在 uploading，并占住该工单的进行中名额
  await markCompleted(task.task_id)
  stage.value = 'done'
  cleanupTip.value =
    `日志包已归档到日志服务器（${completed.size} 字节）。` +
    '平台保留副本供回看，HCI 上的采集包仍在，请在节点上自行清理：\n' +
    `rm -f ${archivePath}\n` +
    '注意：不要删除 /sf/data/local/acli/plugins 与 tasks 目录。'
  ElMessage.success('日志已上传平台并归档到日志服务器')
}

/** 归档由服务端后台任务驱动，前端轮询超时只代表"结果未知"，不代表任务失败。 */
class RelayPendingError extends Error {}

async function pollRelay(taskId: string, token: number) {
  // 上限 2 小时（1440 次 × 5s）：代传是长任务，但不能无限轮询
  for (let attempt = 0; attempt < 1440; attempt++) {
    if (token !== runToken) return
    const snapshot = await logCollectApi.getRelay(taskId)
    relayStatus.value = snapshot.relay_status
    if (snapshot.relay_status === 'success') return
    if (snapshot.relay_status === 'failed') {
      throw new Error('归档到日志服务器失败，错误码见平台任务记录，可据此排查')
    }
    await sleep(POLL_INTERVAL)
  }
  throw new RelayPendingError('归档超过 2 小时仍在进行，平台任务状态未变更，请稍后确认结果')
}

async function handleCancel() {
  if (!currentTask.value) {
    stage.value = 'idle'
    return
  }
  try {
    await ElMessageBox.confirm(
      '取消只会终止平台侧任务，HCI 上的采集进程需在 SSH 终端执行 cancel。',
      '确认取消',
      { type: 'warning' },
    )
  } catch {
    // 用户点了"返回"，保持现状
    return
  }
  // 先让后台轮询退出，再改状态，避免轮询回写覆盖
  runToken++
  await logCollectApi.cancelTask(currentTask.value.task_id, '用户取消').catch(() => undefined)
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(buildLogCollectMessage(props.caseId, 'cancel', { nodeIp: form.nodeIp || props.nodeIp }))
  }
  progress.value = null
  relayStatus.value = 'none'
  stage.value = 'idle'
}

function sleep(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

watch(visible, (open) => {
  if (!open) return
  if (form.dateRange.length === 0) {
    const today = new Date()
    const from = new Date(today.getTime() - 86400000)
    form.dateRange = [formatDate(from), formatDate(today)]
  }
})

function disableFutureDate(value: Date): boolean {
  return value.getTime() > Date.now()
}

function formatDate(value: Date): string {
  const year = value.getFullYear()
  const month = `${value.getMonth() + 1}`.padStart(2, '0')
  const day = `${value.getDate()}`.padStart(2, '0')
  return `${year}-${month}-${day}`
}

onBeforeUnmount(() => {
  // 让仍在运行的后台轮询退出，避免组件卸载后继续改状态
  runToken++
  waiters.clear()
  socket?.close()
  socket = null
})
</script>

<template>
  <el-drawer v-model="visible" title="日志收集" size="720px" :close-on-click-modal="false">
    <div class="lc-body">
      <div class="lc-case-bar">
        <span class="lc-case-label">归档工单</span>
        <span class="lc-case-value">{{ caseId }}</span>
        <el-button
          size="small"
          text
          type="primary"
          :disabled="running"
          @click="chatStore.openLogCollectCaseDialog()"
        >
          更换
        </el-button>
      </div>

      <el-descriptions v-if="probeResult" :column="2" border size="small" class="lc-probe">
        <el-descriptions-item label="架构">{{ probeResult.architecture }}</el-descriptions-item>
        <el-descriptions-item label="acli">
          {{ probeResult.acli_installed ? probeResult.acli_version : '未安装' }}
        </el-descriptions-item>
        <el-descriptions-item label="交付路径">
          <el-tag :type="probeResult.log_server_reachable ? 'success' : 'warning'" size="small">
            {{ probeResult.log_server_reachable ? 'P1 直传日志服务器' : 'P3 经平台中转' }}
          </el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="可用空间">{{ probeResult.available_mb }} MB</el-descriptions-item>
      </el-descriptions>

      <el-form :model="form" label-width="90px" :disabled="running">
        <el-form-item label="故障场景">
          <el-select v-model="form.modules" multiple collapse-tags placeholder="选择收集专项">
            <el-option v-for="item in MODULES" :key="item.value" :label="item.label" :value="item.value" />
          </el-select>
        </el-form-item>
        <el-form-item label="时间范围">
          <el-date-picker
            v-model="form.dateRange"
            type="daterange"
            value-format="YYYY-MM-DD"
            start-placeholder="开始日期"
            end-placeholder="结束日期"
            :disabled-date="disableFutureDate"
          />
          <div class="lc-hint">日志按天归档，将收集所选日期全天日志（最多 30 天）</div>
        </el-form-item>
        <el-form-item label="发起节点">
          <el-input v-model="form.nodeIp" placeholder="与 SSH 终端连接的 HCI 节点 IP" />
          <div class="lc-hint">acli 进度只在该节点有效，取消与进度查询都会路由回此节点</div>
        </el-form-item>
        <el-form-item label="目标主机">
          <el-input v-model="form.ips" placeholder="all 表示全部在线节点，或逗号分隔 IP" />
        </el-form-item>
      </el-form>

      <div v-if="running || stage === 'done' || stage === 'failed'" class="lc-progress">
        <el-steps :active="stepActive" finish-status="success" simple>
          <el-step title="探测" />
          <el-step title="采集" />
          <el-step title="上传" />
          <el-step title="归档" />
        </el-steps>
        <el-progress :percentage="percent" :status="progressStatus" />
        <div class="lc-status">{{ statusText }}</div>
      </div>

      <el-alert
        v-if="cleanupTip"
        :title="cleanupTip"
        type="warning"
        :closable="false"
        show-icon
        class="lc-cleanup"
      />
    </div>

    <template #footer>
      <el-button @click="visible = false">关闭</el-button>
      <el-button v-if="!running" type="primary" @click="handleStart">开始收集</el-button>
      <el-button v-else type="danger" plain @click="handleCancel">取消</el-button>
    </template>
  </el-drawer>
</template>

<style scoped>
.lc-body {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.lc-case-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border-radius: 6px;
  background: var(--el-fill-color-light);
  font-size: 13px;
}
.lc-case-label {
  color: var(--el-text-color-secondary);
}
.lc-case-value {
  font-weight: 600;
  color: var(--el-color-primary);
}
.lc-hint {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  line-height: 1.5;
  margin-top: 4px;
}
.lc-progress {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 12px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
}
.lc-status {
  font-size: 13px;
  color: var(--el-text-color-regular);
}
.lc-cleanup {
  white-space: pre-wrap;
}
</style>
