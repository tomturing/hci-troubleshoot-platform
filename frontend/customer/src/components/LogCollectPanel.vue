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

// ── 抽屉行为（与历史工单/SSH 终端一致）────────────────────────────────────
const pinned = ref(false)

/** 拖拽右边缘调整宽度：写入共享宽度，历史工单抽屉同步变化 */
function startResize(e: MouseEvent) {
  e.preventDefault()
  const startX = e.clientX
  const startWidth = chatStore.sidePanelWidth
  const onMove = (ev: MouseEvent) => {
    chatStore.sidePanelWidth = Math.max(
      300,
      Math.min(window.innerWidth * 0.8, startWidth + (ev.clientX - startX)),
    )
  }
  const onUp = () => {
    document.removeEventListener('mousemove', onMove)
    document.removeEventListener('mouseup', onUp)
  }
  document.addEventListener('mousemove', onMove)
  document.addEventListener('mouseup', onUp)
}

/** 关闭抽屉时暂停平台快照轮询，避免面板不可见还在后台请求 */
function handleDrawerClose() {
  if (resumeTimer) {
    clearInterval(resumeTimer)
    resumeTimer = null
  }
}

const RELAY_URL = 'http://acli.sangfor.com.cn:5888'
const POLL_INTERVAL = 5000

interface ModuleOption {
  value: LogCollectModule
  label: string
}

interface HostOption {
  ip: string
  name?: string
  master?: boolean
}

// 内置场景目录：文案与 acli 1.0.1 帮助一致。
// 第一性原理：目录真源在集群（acli help），这里只是「探测失败也要能用」的兜底，
// 正常路径会被 probe 返回的 modules 覆盖。
const BUILTIN_MODULES: ModuleOption[] = [
  { value: 'vm_suspend', label: '虚拟机挂起专项' },
  { value: 'vm_start', label: '虚拟机开机失败专项' },
  { value: 'host_offline', label: '主机离线专项' },
  { value: 'network', label: '网络专项' },
  { value: 'storage', label: '虚拟存储专项' },
  { value: 'slowdisk', label: '卡慢盘专项' },
  { value: 'performance', label: '性能专项' },
  { value: 'core', label: 'core 文件专项' },
  { value: 'system', label: '中台专项' },
  { value: 'other', label: '默认收集专项（不收集上述场景日志）' },
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
  // 以下字段由 bridge 从目标集群读取；缺省表示探测降级，前端必须能继续工作
  modules?: ModuleOption[]
  hosts?: HostOption[]
  catalog_source?: 'acli_help' | 'platform_default'
  catalog_note?: string
}

type PrepareState = 'idle' | 'probing' | 'ready' | 'failed'

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
// 面板视图：form = 新建收集；result = 该工单已完成收集的只读结果
const viewMode = ref<'form' | 'result'>('form')
const resultTask = ref<LogCollectTask | null>(null)
// 该工单历史任务总数：用于告知"已完成是既成事实，可以重新收集但不是必须"
const historyTotal = ref(0)
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
})

/**
 * 发起节点 = 当前 SSH 会话的目标主机，是既成事实，不是用户输入。
 *
 * 第一性原理：bridge 的会话键是 `caseID@nodeIP`，一切 bridge 动作都必须路由回
 * 「pty 正在运行的那台机器」。用户无法通过填一个 IP 去"连上"另一台机器——
 * 手填与真实会话不一致时只会得到 session_missing（LC-ENV-006）。
 * 因此唯一真值只能来自会话本身，UI 只做展示，不提供编辑入口。
 */
const sessionHost = computed(() => {
  const live = chatStore.sshConnectionState === 'connected' ? chatStore.sshCurrentConfig?.host : ''
  return (live || chatStore.lastSshHost || '').trim()
})
// 会话主机优先；props.nodeIp 仅作为调用方显式指定时的兜底（当前 App.vue 不传）
const nodeIp = computed(() => (sessionHost.value || props.nodeIp || '').trim())

// ── 准备阶段（打开面板即探测，避免用户填完表单才发现环境不可用）────────────
const moduleOptions = ref<ModuleOption[]>([...BUILTIN_MODULES])
const hostOptions = ref<HostOption[]>([])
const prepareState = ref<PrepareState>('idle')
const prepareError = ref('')
// 准备令牌：节点 IP 变更会重探，旧探测结果不得覆盖新结果
let prepareToken = 0

const nodeIpReady = computed(() => Boolean(nodeIp.value))

/**
 * 目标主机选择器与 form.ips 的双向桥。
 *
 * 第一性原理：后端契约是字符串（`all` 或逗号分隔），前端要的是多选；
 * 二者只保留一个真值（form.ips），空数组语义固定为 all，避免"空选"被误读成"不采集"。
 */
const selectedHosts = computed<string[]>({
  get() {
    const raw = form.ips.trim()
    if (!raw || raw === 'all') return []
    return raw
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean)
  },
  set(value: string[]) {
    form.ips = value.length ? value.join(',') : 'all'
  },
})

/**
 * 目标主机勾选框的取值。
 *
 * 第一性原理：`all` 与"空数组"都必须表现为「全部勾选」，否则用户看到一屏未勾选的
 * 主机，会以为一台都不采；反向也一致——勾满即回写 `all`，避免把 20 台 IP 拼成长串。
 * 取消到 0 台时回写 `all`：后端契约里"空选择"= 全部，UI 不允许出现空选择歧义。
 */
const hostChecked = computed<string[]>({
  get() {
    if (!hostOptions.value.length) return []
    const picked = selectedHosts.value
    return picked.length ? picked : hostOptions.value.map((host) => host.ip)
  },
  set(value: string[]) {
    if (!value.length) {
      form.ips = 'all'
      return
    }
    form.ips = value.length === hostOptions.value.length ? 'all' : value.join(',')
  },
})

const allHostsChecked = computed(
  () => hostOptions.value.length > 0 && hostChecked.value.length === hostOptions.value.length,
)

const catalogSourceText = computed(() => {
  if (prepareState.value === 'probing') return '正在从集群读取场景目录…'
  if (!probeResult.value) return '尚未读取集群目录，当前展示平台内置项'
  return probeResult.value.catalog_source === 'acli_help'
    ? '目录来自集群 acli'
    : '平台内置目录（集群未返回目录）'
})

// ── 结果态文案（全部由平台任务记录派生，不做本地推断）──────────────────────

/** 专项文案：优先用本次探测到的集群目录，缺失时回落平台内置。 */
function moduleLabelOf(value: string): string {
  const hit =
    moduleOptions.value.find((item) => item.value === value) ??
    BUILTIN_MODULES.find((item) => item.value === value)
  return hit?.label ?? value
}

const resultFinishedText = computed(() => {
  const raw = resultTask.value?.finished_at || resultTask.value?.updated_at
  if (!raw) return '时间未记录'
  const date = new Date(raw)
  if (Number.isNaN(date.getTime())) return '时间未记录'
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
})

const resultModulesText = computed(() => {
  const modules = resultTask.value?.modules ?? []
  return modules.length ? modules.map(moduleLabelOf).join('、') : '未记录'
})

const resultRangeText = computed(() => {
  const task = resultTask.value
  if (!task) return ''
  return task.date_from === task.date_to ? task.date_from : `${task.date_from} ~ ${task.date_to}`
})

const resultHostsText = computed(() => {
  const ips = resultTask.value?.ips ?? []
  if (!ips.length || ips.includes('all')) return '全部在线节点'
  return `${ips.length} 台（${ips.join('、')}）`
})

const resultSizeText = computed(() => {
  const size = resultTask.value?.archive_size
  if (!size) return '未记录'
  const mb = size / 1024 / 1024
  return mb >= 1024 ? `${(mb / 1024).toFixed(2)} GB` : `${mb.toFixed(1)} MB`
})

const resultDeliveryText = computed(() => {
  const mode = resultTask.value?.upload_mode
  if (mode === 'acli_direct') return 'P1 直传日志服务器'
  if (mode === 'bridge_relay') return 'P3 经平台中转'
  return '未记录'
})

const resultArchiveText = computed(() => {
  const task = resultTask.value
  if (!task) return ''
  if (task.upload_mode === 'acli_direct') return '已由插件直传日志服务器'
  if (task.relay_status === 'success') return '已归档到日志服务器'
  return `归档状态：${task.relay_status}`
})

const resultArtifactText = computed(() => {
  const task = resultTask.value
  if (!task) return ''
  if (task.archive_deleted) return 'HCI 上的压缩包已清理'
  return task.archive_path || '未记录'
})

const resultCleanupTip = computed(() => {
  const task = resultTask.value
  if (!task || task.archive_deleted || !task.archive_path) return ''
  if (task.upload_mode === 'acli_direct') {
    return 'P1 由插件直传日志服务器，上传成功后插件会自动删除 HCI 上的压缩包，无需人工清理。'
  }
  return `平台不自动删除 HCI 上的采集包，请在节点上自行清理：rm -f ${task.archive_path}（不要删除 /sf/data/local/acli/plugins 与 tasks 目录）`
})

/** 节点下拉文案：IP 是必选值，名称与主控标记只做辅助识别。 */
function hostLabel(host: HostOption): string {
  const base = host.name ? `${host.ip}（${host.name}）` : host.ip
  return host.master ? `${base} · 主控` : base
}

/** 用探测结果覆盖目录与节点清单；场景目录以集群为准，内置目录只做兜底。 */
function applyCatalog(data: ProbeResult) {
  moduleOptions.value = data.modules?.length ? data.modules : [...BUILTIN_MODULES]
  hostOptions.value = data.hosts ?? []
  // 目录收敛后清掉已不存在的场景，否则提交时会被 create 判为非法枚举
  const valid = new Set(moduleOptions.value.map((item) => item.value))
  form.modules = form.modules.filter((value) => valid.has(value))
  // 收敛后为空会让用户误以为"没有可选项"，回落到目录首项（集群目录首项即最常用专项）
  if (!form.modules.length && moduleOptions.value.length > 0) {
    form.modules = [moduleOptions.value[0].value]
  }
}

/**
 * 准备阶段：读取架构、acli、可用空间、日志服务器连通性，以及场景目录与节点清单。
 *
 * 对抗性审查：探测失败不等于不能收集——主机清单为空时回退手填 IP，
 * 因此这里只记录失败原因，不阻断后续表单编辑。
 */
async function prepare(options: { silent?: boolean } = {}): Promise<boolean> {
  if (prepareState.value === 'probing') return false
  const targetNode = nodeIp.value
  if (!targetNode) {
    prepareState.value = 'failed'
    prepareError.value = '请先在 SSH 终端连接 HCI 节点'
    return false
  }
  const token = ++prepareToken
  prepareState.value = 'probing'
  prepareError.value = ''
  try {
    const ws = await openSocket()
    const probe = await sendAndWait(ws, 'probe', { remoteUrl: RELAY_URL, nodeIp: targetNode }, 90000)
    if (token !== prepareToken) return false
    if (probe.status !== 'ok') {
      throw new Error(probe.message || '环境探测失败，请确认已在 SSH 终端连接 HCI')
    }
    const data = probe.log_data as ProbeResult
    probeResult.value = data
    applyCatalog(data)
    prepareState.value = 'ready'
    return true
  } catch (error: any) {
    if (token !== prepareToken) return false
    prepareState.value = 'failed'
    prepareError.value = String(error?.message || error).slice(0, 200)
    if (!options.silent) ElMessage.error(prepareError.value)
    return false
  }
}

const running = computed(() =>
  ['probing', 'installing', 'creating', 'collecting', 'pulling', 'uploading', 'relaying'].includes(stage.value),
)

const percent = computed(() => {
  if (stage.value === 'done') return 100
  if (resumedTaskId.value) return currentTask.value?.progress ?? 0
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
      // 所有动作一律锚定会话主机：nodeIp 是派生值，不存在"表单里另填一个"的可能，
      // 也就不会再出现写成空串（bridge 落到默认会话）或写成不存在节点（session_missing）。
      ws.send(
        buildLogCollectMessage(props.caseId, action, {
          ...(options as any),
          nodeIp: nodeIp.value,
        }),
      )
    }
  })
}

// ── 已有任务接管（409 后"查看任务状态" / 刷新恢复）─────────────────────────
const resumedTaskId = ref<string | null>(null)
let resumeTimer: ReturnType<typeof setInterval> | null = null

const TASK_STATUS_STAGE: Record<string, Stage> = {
  pending: 'creating',
  probing: 'probing',
  installing: 'installing',
  collecting: 'collecting',
  packaging: 'collecting',
  uploading: 'uploading',
  completed: 'done',
  failed: 'failed',
  cancelled: 'failed',
}

const TERMINAL_TASK_STATUSES = ['completed', 'failed', 'cancelled']

/**
 * 轮询平台快照展示已有任务。
 *
 * 不驱动 bridge：任务可能是其他客户端发起的（同一工单号），这里只读状态；
 * 若是本人任务，平台快照足以展示阶段/进度与失败原因。
 */
async function resumeTask(taskId: string) {
  try {
    const task = await logCollectApi.getTask(taskId)
    currentTask.value = task
    resumedTaskId.value = taskId
    stage.value = TASK_STATUS_STAGE[task.status] ?? 'idle'
    if (task.error_message) errorMessage.value = task.error_message

    if (resumeTimer) clearInterval(resumeTimer)
    resumeTimer = setInterval(async () => {
      try {
        const latest = await logCollectApi.getTask(taskId)
        currentTask.value = latest
        stage.value = TASK_STATUS_STAGE[latest.status] ?? 'idle'
        if (TERMINAL_TASK_STATUSES.includes(latest.status) && resumeTimer) {
          clearInterval(resumeTimer)
          resumeTimer = null
        }
      } catch {
        if (resumeTimer) {
          clearInterval(resumeTimer)
          resumeTimer = null
        }
      }
    }, POLL_INTERVAL)
  } catch (e: any) {
    ElMessage.error(
      e?.response?.status === 404 ? '任务不存在或不属于当前客户端' : '任务状态加载失败',
    )
  }
}

/** 409 后点"查看任务状态"：把面板切到只读接管模式。 */
function resumeActiveTask(): void {
  const active = chatStore.logCollectActiveTask
  if (active) void resumeTask(active.task_id)
}

// ── 手动刷新 + 自动刷新 ────────────────────────────────────────────────────
const refreshingProgress = ref(false)
const lastRefreshAt = ref<Date | null>(null)
// 主流程正在向 bridge 轮询进度时，手动刷新只读平台快照，避免抢占 waiter
let pollingBridgeProgress = false

const lastRefreshText = computed(() => {
  if (!lastRefreshAt.value) return ''
  const hh = String(lastRefreshAt.value.getHours()).padStart(2, '0')
  const mm = String(lastRefreshAt.value.getMinutes()).padStart(2, '0')
  const ss = String(lastRefreshAt.value.getSeconds()).padStart(2, '0')
  return `更新于 ${hh}:${mm}:${ss}`
})

/**
 * 查询任务进度。
 *
 * 两个信息源必须分开看：
 * - 平台快照（GET 任务）：任务状态机、错误原因，任何客户端都能查；
 * - bridge 实时进度：acli 的百分比只有节点侧知道，平台不落库。
 * 主流程正在轮询 bridge 时只刷新平台快照，防止两个 'progress' 请求互相覆盖 waiter。
 */
async function refreshProgress(silent = false) {
  if (refreshingProgress.value) return
  const taskId = currentTask.value?.task_id || resumedTaskId.value
  if (!taskId) {
    if (!silent) ElMessage.info('当前没有可查询的采集任务')
    return
  }
  refreshingProgress.value = true
  try {
    const latest = await logCollectApi.getTask(taskId)
    currentTask.value = latest
    stage.value = TASK_STATUS_STAGE[latest.status] ?? stage.value
    if (latest.error_message) errorMessage.value = latest.error_message

    if (!pollingBridgeProgress && socket?.readyState === WebSocket.OPEN) {
      try {
        const result = await sendAndWait(socket, 'progress', {}, 15000)
        if (result.status === 'error') throw new Error(result.message || 'bridge 进度查询失败')
        const snapshot = result.log_data as ProgressSnapshot | null
        if (snapshot) progress.value = snapshot
      } catch (bridgeError: any) {
        // 平台状态已刷新，bridge 不可用只做提示，不否定整个查询
        if (!silent) ElMessage.warning(`节点侧进度不可用：${bridgeError.message}`)
      }
    }
    lastRefreshAt.value = new Date()
    if (!silent) ElMessage.success('已刷新任务进度')
  } catch (e: any) {
    if (!silent) {
      ElMessage.error(
        e?.response?.status === 404 ? '任务不存在或不属于当前客户端' : '任务进度查询失败',
      )
    }
  } finally {
    refreshingProgress.value = false
  }
}

/**
 * 打开面板时以平台任务为准恢复现场。
 *
 * 第一性原理：任务状态的真值在平台（log_collect_task 表），不在前端内存。
 * 早先只恢复了"进行中"的任务，完成后一关抽屉再打开就退回空白表单，
 * 用户会以为必须再收一次——这既违背事实，也容易造成重复采集。
 * 因此恢复顺序固定为：进行中 → 最近一次已完成 → 空白表单（可重新收集）。
 */
async function restoreTaskState(force = false) {
  if (!force && (currentTask.value || viewMode.value === 'result')) return
  try {
    const list = await logCollectApi.listTasks(props.caseId, 10)
    historyTotal.value = list.total ?? list.items?.length ?? 0
    const items = list.items ?? []
    const running = items.find((item) => !TERMINAL_TASK_STATUSES.includes(item.status))
    if (running) {
      viewMode.value = 'form'
      await resumeTask(running.task_id)
      ElMessage.info('已接管进行中的采集任务，进度每 5 秒自动刷新')
      return
    }
    // 已完成优先展示：这是用户最需要看到的结果（收了什么、什么时候收的、包在哪）
    const completed = items.find((item) => item.status === 'completed')
    if (completed) {
      showResult(completed)
      return
    }
    if (viewMode.value === 'result') viewMode.value = 'form'
  } catch {
    // 恢复失败不影响新任务下发
  }
}

/** 切到结果态：只读展示平台上的完成记录，不参与 bridge 轮询。 */
function showResult(task: LogCollectTask) {
  resultTask.value = task
  currentTask.value = task
  viewMode.value = 'result'
  stage.value = 'done'
  progress.value = null
  relayStatus.value = 'none'
  cleanupTip.value = ''
  errorMessage.value = ''
}

/**
 * 重新收集：回到表单，并用上一次的参数预填。
 *
 * 覆盖是常态（同一故障再发一次），预填省掉重复选择；但场景必须收敛到当前目录，
 * 否则提交时会被 create 判为非法枚举。
 */
function startRecollect() {
  const last = resultTask.value
  viewMode.value = 'form'
  stage.value = 'idle'
  currentTask.value = null
  resumedTaskId.value = null
  progress.value = null
  relayStatus.value = 'none'
  cleanupTip.value = ''
  errorMessage.value = ''
  if (last) {
    form.dateRange = [last.date_from, last.date_to]
    const ips = last.ips ?? []
    form.ips = !ips.length || ips.includes('all') ? 'all' : ips.join(',')
    const valid = new Set<string>(moduleOptions.value.map((item) => item.value))
    const picked = (last.modules ?? []).filter((value) => valid.has(value))
    if (picked.length) form.modules = picked as LogCollectModule[]
  }
  if (prepareState.value !== 'ready') void prepare({ silent: true })
}

// ── 主流程 ──────────────────────────────────────────────────────────────────

async function handleStart() {
  if (!form.dateRange || form.dateRange.length !== 2) {
    ElMessage.warning('请选择时间范围')
    return
  }

  const targetNode = nodeIp.value
  if (!targetNode) {
    ElMessage.warning('请先在 SSH 终端连接 HCI 节点')
    return
  }

  cleanupTip.value = ''
  errorMessage.value = ''
  transfer.sha256 = ''
  currentTask.value = null
  progress.value = null
  relayStatus.value = 'none'
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
    // 复用准备阶段的探测结果：打开面板时已经读过集群，这里再探一次纯属浪费，
    // 且会重复触发 acli help 解析。仅在未准备/准备失败时现场补探。
    if (prepareState.value !== 'ready' || !probeResult.value) {
      const ready = await prepare({ silent: true })
      if (!ready || !probeResult.value) throw new Error(prepareError.value || '环境准备失败')
    }

    if (!probeResult.value.acli_installed) {
      await confirmInstall(ws)
    }

    pathMode.value = probeResult.value.log_server_reachable ? 'P1' : 'P3'

    let task: LogCollectTask
    try {
      task = await logCollectApi.createTask({
        case_id: props.caseId,
        // 工单号只有一个：acli -s、产物名与任务归属都使用 case_id
        origin_node_ip: targetNode,
        modules,
        date_from: form.dateRange[0],
        date_to: form.dateRange[1],
        ips: form.ips.split(',').map((s) => s.trim()).filter(Boolean),
        remote_url: pathMode.value === 'P1' ? RELAY_URL : undefined,
        trace_id: `lc-${props.caseId}-${Date.now()}`,
      })
    } catch (createError: any) {
      if (createError?.response?.status === 409) {
        // 冲突不是失败：不改任务状态，停在 idle 让用户点"查看任务状态"
        const problem = createError.response.data?.detail ?? {}
        chatStore.setLogCollectActiveTask(problem.active_task ?? null)
        stage.value = 'idle'
        errorMessage.value = problem.title || '该工单已有进行中的日志收集任务'
        ElMessage.warning(errorMessage.value)
        return
      }
      throw createError
    }
    currentTask.value = task

    stage.value = 'creating'
    const created = await sendAndWait(
      ws,
      'create',
      {
        nodeIp: targetNode,
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
  pollingBridgeProgress = true
  try {
    for (let attempt = 0; attempt < 720; attempt++) {
      if (token !== runToken) return false
      const result = await sendAndWait(ws, 'progress', {}, 60000)
      // bridge 明确报错（如会话已断开）必须立刻上抛：
      // 早先这里只读 log_data，错误结果会被当成"还没有进度"，面板空转一小时仍显示 0%。
      if (result.status === 'error') {
        throw new Error(result.message || '节点侧进度查询失败')
      }
      const snapshot = result.log_data as ProgressSnapshot | null
      if (snapshot) {
        progress.value = snapshot
        lastRefreshAt.value = new Date()
        if (snapshot.percent >= 80 || snapshot.status.includes('完成')) return true
        if (snapshot.status.includes('失败')) return false
      }
      await sleep(POLL_INTERVAL)
    }
    return false
  } finally {
    pollingBridgeProgress = false
  }
}

async function finishP1(task: LogCollectTask) {
  const snapshot = progress.value
  if (snapshot?.upload_state && !snapshot.upload_state.includes('成功')) {
    throw new Error(`日志服务器上传失败：${snapshot.upload_state}，可改用 P3 中转`)
  }
  await markCompleted(task.task_id, {
    upload_mode: 'acli_direct',
    archive_path: snapshot?.archive_path || undefined,
  })
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
async function markCompleted(taskId: string, extra: Partial<LogCollectTask> = {}) {
  try {
    currentTask.value = await logCollectApi.patchTask(taskId, {
      status: 'completed',
      progress: 100,
      ...extra,
    })
    // 完成即切结果态：同一份展示既服务"刚收完"，也服务"关掉抽屉再打开"，
    // 避免内存态与平台态两套 UI 各说一套。
    if (currentTask.value) showResult(currentTask.value)
  } catch {
    ElMessage.warning('日志已归档，但平台任务状态回填失败，请在任务列表确认')
  }
}

async function runP3(ws: WebSocket, task: LogCollectTask, token: number) {
  const archivePath = progress.value?.archive_path || ''
  if (!archivePath) throw new Error('未获取到压缩包路径，无法中转')

  // 1) 取包大小（init 需要 total_size）
  const statResult = await sendAndWait(ws, 'stat', { sourcePath: archivePath }, 30000)
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
  await markCompleted(task.task_id, {
    upload_mode: 'bridge_relay',
    archive_path: archivePath,
    archive_size: completed.size,
    // sha256 有 64 位小写十六进制格式约束，格式不符时宁可不写，避免整条 PATCH 被 422 拒绝
    archive_sha256: /^[0-9a-f]{64}$/.test(completed.sha256 || '') ? completed.sha256 : undefined,
  })
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
    socket.send(buildLogCollectMessage(props.caseId, 'cancel', {}))
  }
  progress.value = null
  relayStatus.value = 'none'
  stage.value = 'idle'
}

function sleep(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

// immediate 必须为 true：App.vue 用 v-if="logCollectCaseId" 挂载本组件，
// 而 logCollectCaseId 与 logCollectVisible 在同一 tick 赋值，组件"带着 visible=true
// 首次挂载"；非 immediate 的 watcher 看不到初始值，探测请求永远不会发出。
watch(
  visible,
  (open) => {
    if (!open) return
    if (form.dateRange.length === 0) {
      const today = new Date()
      const from = new Date(today.getTime() - 86400000)
      form.dateRange = [formatDate(from), formatDate(today)]
    }
    // 打开即准备：把架构/acli/目录/节点清单先读回来，用户一进来就能看见可选项。
    if (!nodeIpReady.value) {
      prepareState.value = 'idle'
      return
    }
    if (prepareState.value !== 'ready' && prepareState.value !== 'probing') {
      void prepare({ silent: true })
    }
    // 重开面板时接管进行中的任务，避免"关掉再打开就看不到进度"
    void restoreTaskState()
  },
  { immediate: true },
)

// 换工单 = 换上下文：App.vue 以 v-if 挂载本组件，换单只更新 props，不重建实例，
// 因此必须显式作废上一单的完成态、任务与探测结论，否则会把 A 单的完成结果展示给 B 单。
watch(
  () => props.caseId,
  () => {
    viewMode.value = 'form'
    resultTask.value = null
    currentTask.value = null
    resumedTaskId.value = null
    historyTotal.value = 0
    probeResult.value = null
    prepareState.value = 'idle'
    stage.value = 'idle'
    cleanupTip.value = ''
    errorMessage.value = ''
    if (visible.value) void restoreTaskState(true)
  },
)

// 会话主机变了（重新登录到另一台节点），上一轮的探测结论（目录、节点清单、P1/P3 判定）
// 全部失效，必须重探。派生值只会在会话切换时变化，防抖仅用于合并登录过程中的多次写入。
let nodeIpTimer: ReturnType<typeof setTimeout> | null = null
watch(
  nodeIp,
  (value) => {
    if (!visible.value || !value) return
    // 采集中换节点属于异常操作：此时重探只会把展示值改成另一台机器，
    // 而运行中的任务仍锚在旧节点（handleStart 已捕获 targetNode），反而制造错配。
    if (running.value) return
    if (nodeIpTimer) clearTimeout(nodeIpTimer)
    nodeIpTimer = setTimeout(() => {
      probeResult.value = null
      prepareState.value = 'idle'
      void prepare({ silent: true })
    }, 500)
  },
)

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
  // 准备阶段的令牌与定时器同样要失效，否则重探会在卸载后回写状态
  prepareToken++
  if (nodeIpTimer) {
    clearTimeout(nodeIpTimer)
    nodeIpTimer = null
  }
  if (resumeTimer) {
    clearInterval(resumeTimer)
    resumeTimer = null
  }
  waiters.clear()
  socket?.close()
  socket = null
})
</script>

<template>
  <!--
    与「历史工单」「SSH 终端」抽屉完全对齐：左侧滑出、共享宽度、可拖拽调宽、
    支持钉住（钉住后点击外部不再回缩，仅切掉遮罩）、点外部/ESC 关闭。
  -->
  <el-drawer
    v-model="visible"
    direction="ltr"
    :size="`${chatStore.sidePanelWidth}px`"
    :append-to-body="true"
    :modal="true"
    :lock-scroll="!pinned"
    :close-on-click-modal="!pinned"
    :close-on-press-escape="!pinned"
    :with-header="true"
    :modal-class="pinned ? 'drawer-unblocked-overlay' : ''"
    class="workspace-drawer"
    @close="handleDrawerClose"
  >
    <div class="drawer-resize-handle drawer-resize-right" @mousedown="startResize"></div>
    <template #header>
      <div class="drawer-header">
        <span class="drawer-title">日志收集</span>
        <div class="drawer-actions">
          <span v-if="viewMode === 'form' && lastRefreshText" class="lc-refresh-time">{{ lastRefreshText }}</span>
          <el-button
            v-if="viewMode === 'form'"
            text
            size="small"
            :loading="refreshingProgress"
            @click="refreshProgress()"
          >
            刷新进度
          </el-button>
          <el-button text size="small" @click="pinned = !pinned">
            {{ pinned ? '取消钉住' : '钉住' }}
          </el-button>
        </div>
      </div>
    </template>
    <div class="drawer-body-content lc-body">
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

      <!--
        结果态优先：已完成是平台上的既成事实，先把结论摆出来；
        重新收集只是可选项，不是默认动作。
      -->
      <div v-if="viewMode === 'result' && resultTask" class="lc-result">
        <el-alert
          type="success"
          :closable="false"
          show-icon
          :title="`该工单已完成日志收集（${resultFinishedText}）`"
          description="以下是平台记录的上一次收集结果；如需再次采集，点右下角「重新收集」。"
        />
        <el-descriptions :column="2" border size="small">
          <el-descriptions-item label="发起节点">{{ resultTask.origin_node_ip }}</el-descriptions-item>
          <el-descriptions-item label="交付路径">{{ resultDeliveryText }}</el-descriptions-item>
          <el-descriptions-item label="收集场景">{{ resultModulesText }}</el-descriptions-item>
          <el-descriptions-item label="时间范围">{{ resultRangeText }}</el-descriptions-item>
          <el-descriptions-item label="目标主机">{{ resultHostsText }}</el-descriptions-item>
          <el-descriptions-item label="压缩包大小">{{ resultSizeText }}</el-descriptions-item>
          <el-descriptions-item label="归档状态">{{ resultArchiveText }}</el-descriptions-item>
          <el-descriptions-item label="产物位置">{{ resultArtifactText }}</el-descriptions-item>
        </el-descriptions>
        <el-alert
          v-if="resultCleanupTip"
          :title="resultCleanupTip"
          type="warning"
          :closable="false"
          show-icon
          class="lc-cleanup"
        />
        <p class="lc-hint">
          当前客户端在本工单下共 {{ historyTotal }} 条收集记录；已完成的任务不会被覆盖，重新收集会新建一条任务。
        </p>
      </div>

      <template v-else>
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
          <el-descriptions-item label="场景目录">
            <el-tag size="small" :type="probeResult.catalog_source === 'acli_help' ? 'success' : 'info'">
              {{ catalogSourceText }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="可采节点">
            {{ probeResult.hosts?.length ? `${probeResult.hosts.length} 个` : '未获取' }}
          </el-descriptions-item>
        </el-descriptions>

        <!-- 准备状态的唯一出口：用户在填表之前就能看见"能不能收、可选什么" -->
        <el-alert
          v-if="!nodeIpReady"
          class="lc-prepare"
          type="info"
          :closable="false"
          show-icon
          title="请先在 SSH 终端连接 HCI 节点；连接后才能读取场景目录与节点清单"
        />
        <el-alert
          v-else-if="prepareState === 'probing'"
          class="lc-prepare"
          type="info"
          :closable="false"
          show-icon
          title="正在读取集群环境：架构 / acli / 场景目录 / 节点清单…"
        />
        <el-alert
          v-else-if="prepareState === 'failed'"
          class="lc-prepare"
          type="error"
          :closable="false"
          show-icon
          :title="`环境准备失败：${prepareError}`"
        >
          <el-button size="small" text type="primary" :disabled="running" @click="prepare()">
            重新读取
          </el-button>
        </el-alert>

        <!-- 探测期间只锁"提交"，不锁表单：用户可以先看清可填项，不必等探测结束 -->
        <el-form :model="form" label-width="90px" :disabled="running">
          <el-form-item label="故障场景">
            <!-- 全量平铺勾选：目录最多十余项，一次看全比"点开下拉再逐项找"少两次交互 -->
            <el-checkbox-group v-model="form.modules" class="lc-checkbox-group lc-module-group">
              <el-checkbox
                v-for="item in moduleOptions"
                :key="item.value"
                :value="item.value"
                border
                size="small"
              >
                {{ item.label }}
              </el-checkbox>
            </el-checkbox-group>
            <div class="lc-hint">
              {{ catalogSourceText }}
              <span v-if="probeResult?.catalog_note">；{{ probeResult.catalog_note }}</span>
            </div>
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
            <!--
              只读展示：发起节点就是当前 SSH 会话连的那台机器，用户改不动、也不该改。
              acli 进度只在该节点有效，会话键是 caseID@nodeIP，手填不一致只会落到不存在的会话。
            -->
            <div class="lc-node-fact">
              <template v-if="nodeIp">
                <span class="lc-node-fact-value">{{ nodeIp }}</span>
                <el-tag size="small" type="info" effect="plain">当前 SSH 会话</el-tag>
              </template>
              <span v-else class="lc-node-fact-empty">未连接 SSH 会话</span>
            </div>
            <div class="lc-hint">
              取自已登录的 SSH 终端，采集、进度查询与取消都会路由回此节点；换节点请重新登录 SSH
            </div>
          </el-form-item>
          <el-form-item label="目标主机">
            <!-- 有集群清单就必须用清单：手填 IP 是唯一允许的降级，不是默认交互 -->
            <template v-if="hostOptions.length">
              <div class="lc-host-head">
                <span class="lc-host-count">共 {{ hostOptions.length }} 台在线主机</span>
                <span class="lc-host-state">
                  {{ allHostsChecked ? '已全选（等价 all）' : `已选 ${hostChecked.length} 台` }}
                </span>
              </div>
              <el-checkbox-group v-model="hostChecked" class="lc-checkbox-group lc-host-group">
                <el-checkbox
                  v-for="host in hostOptions"
                  :key="host.ip"
                  :value="host.ip"
                  border
                  size="small"
                >
                  {{ hostLabel(host) }}
                </el-checkbox>
              </el-checkbox-group>
            </template>
            <el-input v-else v-model="form.ips" placeholder="all 表示全部在线节点，或逗号分隔 IP" />
            <div class="lc-hint">
              {{
                hostOptions.length
                  ? '不勾选任何主机 = 全部在线节点；勾选全部同样等价于 all'
                  : '未获取到节点清单，请按 acli -i 格式手填 IP'
              }}
            </div>
          </el-form-item>
        </el-form>

        <!-- 409：同一工单已有进行中的任务。冲突不是失败，提供"查看任务状态" -->
        <div v-if="chatStore.logCollectActiveTask" class="lc-conflict">
          <div class="lc-conflict-body">
            <p class="lc-conflict-title">{{ errorMessage || '该工单已有进行中的日志收集任务' }}</p>
            <p class="lc-conflict-meta">
              状态：{{ chatStore.logCollectActiveTask.status }}
              · 进度：{{ chatStore.logCollectActiveTask.progress }}%
              <span v-if="!chatStore.logCollectActiveTask.created_by_me">· 由其他客户端发起</span>
            </p>
          </div>
          <el-button
            v-if="chatStore.logCollectActiveTask.created_by_me"
            size="small"
            type="primary"
            @click="resumeActiveTask()"
          >
            查看任务状态
          </el-button>
        </div>

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
      </template>
    </div>

    <template #footer>
      <el-button @click="visible = false">关闭</el-button>
      <template v-if="viewMode === 'result'">
        <el-button type="primary" @click="startRecollect">重新收集</el-button>
      </template>
      <template v-else>
        <el-button
          v-if="!running"
          type="primary"
          :disabled="prepareState === 'probing'"
          @click="handleStart"
        >
          {{ prepareState === 'probing' ? '正在读取环境…' : '开始收集' }}
        </el-button>
        <el-button v-else type="danger" plain @click="handleCancel">取消</el-button>
      </template>
    </template>
  </el-drawer>
</template>

<style scoped>
.lc-body {
  display: flex;
  flex-direction: column;
  gap: 16px;
  /* 抽屉 body 由全局样式设为 overflow:hidden，内容超高时必须自己滚 */
  overflow-y: auto;
  height: 100%;
  box-sizing: border-box;
}
.lc-refresh-time {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-right: 4px;
  white-space: nowrap;
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
.lc-conflict {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 12px;
  border: 1px solid var(--el-color-warning-light-5);
  border-radius: 6px;
  background: var(--el-color-warning-light-9);
}
.lc-conflict-body {
  flex: 1;
  min-width: 0;
}
.lc-conflict-title {
  margin: 0 0 2px;
  font-size: 13px;
  font-weight: 600;
  color: var(--el-color-warning-dark-2);
}
.lc-conflict-meta {
  margin: 0;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.lc-result {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.lc-hint {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  line-height: 1.5;
  margin-top: 4px;
}
/* 发起节点是事实展示，不是输入框：用等宽字体弱化"可编辑"暗示 */
.lc-node-fact {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 32px;
}
.lc-node-fact-value {
  font-family: var(--el-font-family-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  font-size: 13px;
  color: var(--el-text-color-primary);
}
.lc-node-fact-empty {
  font-size: 13px;
  color: var(--el-text-color-secondary);
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
.lc-prepare {
  margin-bottom: 12px;
}
/* 勾选组：全量平铺、自动换行；长标签（IP+名称+主控）允许折行，不再被下拉宽度截断 */
.lc-checkbox-group {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  width: 100%;
}
.lc-checkbox-group :deep(.el-checkbox) {
  margin-right: 0;
  height: auto;
  min-height: 24px;
  padding: 2px 10px;
}
.lc-checkbox-group :deep(.el-checkbox__label) {
  font-size: 12px;
  line-height: 1.5;
  white-space: normal;
  word-break: break-all;
}
.lc-host-head {
  display: flex;
  justify-content: space-between;
  width: 100%;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  line-height: 1.5;
}
</style>
