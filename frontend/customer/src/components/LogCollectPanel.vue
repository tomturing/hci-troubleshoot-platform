<script setup lang="ts">
/**
 * 日志收集面板（acli log_collect + P1/P2 交付编排）。
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
  buildDeliveryMessage,
  buildLogCollectMessage,
  checkBridgeBeforeOpen,
  createBridgeSocket,
  parseAcliSyncFacts,
  resolveBridgeUrl,
  type AcliRetryStage,
  type AcliSyncFacts,
} from '@/api/terminal'
import {
  logCollectApi,
  type DeliveryStatus,
  type LogCollectCustomScenarioItem,
  type LogCollectDefaultManifest,
  type LogCollectModule,
  type LogCollectScenarioItem,
  type LogCollectTask,
  type LogCollectTaskPatch,
  type ProbeHistoryItem,
  type RelaySnapshot,
  type RelayStatus,
  type UploadInitResult,
} from '@/api/logCollect'
import {
  buildDeliverySubmitMessage,
  buildDeliverySubmitPayload,
  describeRelayProgress,
  describeUploadProgress,
  formatFailureReason,
  isCertificateUntrusted,
  isTerminalDeliveryFailure,
  platformCertTrusted,
  rememberPlatformCertTrust,
  buildResultHeadline,
  canResumeDeliveryAfterReject,
  describeArchiveCheck,
  hasDeliveryEvidence,
  isStatusTransitionRejected,
  p1ArchiveCheckOf,
  type DeliverySubmitPayload,
  type UploadProgressView,
  type RelayProgressView,
} from '@/utils/logCollectDelivery'
import { logCollectErrorHint } from '@/services/logCollectErrorHints'
import { customerFaultText, showCustomerFault } from '@/utils/logCollectFault'
import { UI_TEXT, MISSING_DATA_TEXT, RENDER_POLICY } from '@hci/shared'
import LogCollectFaultCard from './LogCollectFaultCard.vue'
import LogCollectDiagnosis from '@/components/LogCollectDiagnosis.vue'
import {
  EXTRA_PATH_MAX_COUNT,
  extraPathsLimitHint,
  normalizeExtraPaths,
  parseExtraPathInput,
  validateExtraPath,
} from '@/utils/logCollectExtraPaths'
import { checkDateRange } from '@/utils/logCollectDateRange'
import { CLAIM_BLOCKED_REASON_TEXT } from '@/utils/logCollectOwnership'
import { buildCancelNotice, cancelLogCollectTask } from '@/services/logCollectCancel'
import { ACLI_SYNC_TIMEOUT_MS, acliRetrySuggestions } from '@/services/acliManager'
import { pickDisplayTask } from '@/services/logCollectHistory'
import { computeOverallPercent, formatByteCount } from '@/services/logCollectProgress'
import { buildNodeCleanupTip } from '@/services/logCollectCleanup'
import {
  BRIDGE_CHANNEL_LOST_MESSAGE,
  isOutcomeUnknownError,
  isSessionDiverged,
  isArchiveSizeSettled,
  isTransientPlatformError,
  shouldRecoverRelayLease,
} from '@/services/logCollectRelay'
import {
  blocksCreate as blocksCreateAction,
  buildSyncNotice,
  decideNodeSync,
  classifyUploadState,
  DELIVERY_EVIDENCE_WAIT_MS,
  fallbackNodeState,
  hashReport,
  interpretArchiveProbe,
  isP1UploadDelivered,
  parseAcliTotalTime,
  type ArchiveVerification,
  type NodeState,
  type SyncAction,
} from '@/services/logCollectSync'
import { useChatStore } from '@/stores/chat'

const props = defineProps<{ caseId: string; nodeIp?: string }>()
const visible = defineModel<boolean>({ required: true })
const chatStore = useChatStore()

// ── 面板内容（外壳由 PanelShell 提供）──────────────────────────────────────

/**
 * 描述表列数随抽屉宽度切换。
 *
 * el-descriptions 的列数是 JS 属性，CSS 容器查询改不动；而抽屉宽度本身就是
 * 唯一真值，直接用它判断比再挂一个 ResizeObserver 更可靠。
 * 600px 是「96px 字段名 + ≥140px 值」两列都不折行的下限。低于它硬上两列，
 * 「P1 直传日志服务器」这种 9 字文案就会折成两行，反而比单列更碎。
 */
const descColumns = computed(() => (chatStore.leftPanelWidth >= 600 ? 2 : 1))

/** 表单标签：窄容器下置顶，避免标签列继续吃掉本就不多的内容宽度 */
const formLabelPosition = computed(() => (chatStore.leftPanelWidth < 480 ? 'top' : 'right'))

/** 日志服务器探测目标：与 bridge 的默认值一致，仅用于展示与兜底文案。 */
const RELAY_URL = 'http://acli.sangfor.com.cn:5888/'

/**
 * 探测回包归一化：把 bridge 的 `log_server_http_code` 与平台存档的 `http_code` 收成同一个值。
 *
 * 第一性原理：HTTP 码是「日志服务器到底回没回话」的唯一机器判据。两个来源并存时若
 * 「取到哪个算哪个」，同一份事实就会在不同入口给出不同结论（面板说 0、历史记录说 500）。
 * 所以入口只留一个：合并后的值写回 `http_code`，业务代码不得再读第二个字段。
 */
function normalizeProbe(raw: Partial<ProbeResult> | null | undefined): ProbeResult | null {
  if (!raw) return null
  const code = raw.log_server_http_code ?? raw.http_code
  return {
    ...(raw as ProbeResult),
    http_code: typeof code === 'number' && Number.isFinite(code) ? code : 0,
  }
}
const POLL_INTERVAL = 5000
// 终态任务的低频对账间隔：failed 的节点侧任务可能仍在跑（需要「接管」），
// 完全停轮询会让接管入口再也不出现；但每 5s 打一次 SSH 也没有必要。
const TERMINAL_POLL_INTERVAL = 30000

/**
 * 交付证据等待起点（单调时钟，不持久化）。
 *
 * 关页面重开 / 换端接管时内存起点会丢，此时以任务的 `updated_at` 为锚点：
 * `status=completed` 写入平台那一刻就是"采集完成"时刻，与内存起点同义。
 * 不做这个回退，窗口会被每轮对账重置成 0，等待就会变成永久等待。
 */
let deliveryWaitStartedAt = 0

function deliveryWaitElapsed(task: LogCollectTask | null): number {
  if (deliveryWaitStartedAt) return Math.max(0, Date.now() - deliveryWaitStartedAt)
  const anchor = task?.updated_at || task?.finished_at
  const ts = anchor ? Date.parse(String(anchor)) : NaN
  return Number.isFinite(ts) ? Math.max(0, Date.now() - ts) : 0
}

/** 唯一调用链：状态转移带上 trace/case/task、证据来源与等待时长，便于端到端串起前后端日志。 */
function logDeliveryDecision(
  event: string,
  detail: Record<string, unknown>,
  task: LogCollectTask | null = currentTask.value,
) {
  console.info('[log-collect][delivery]', JSON.stringify({
    event,
    trace_id: `lc-${props.caseId}-${task?.task_id ?? 'none'}`,
    case_id: props.caseId,
    task_id: task?.task_id ?? null,
    ...detail,
  }))
}

// 最近一次桥侧失败的规范错误码（LC-<LAYER>-<NNN>）。
//
// 桥侧 log_collect_result 已经带 error_code（layer/owner/retryable 由 sendMsg 补全），
// 但失败是以 throw 抛出的，码会在异常链里丢失。落库前用它把码补回平台任务，
// 否则 error_code 恒为 NULL（2026-10-27 实测 35/38），错误码目录无法统计与分流。
let lastNodeErrorCode = ''

/**
 * 本次 create 的完整（脱敏后）节点输出。
 *
 * bridge 已按头尾保留，Python Traceback 的尾部异常类型不会再被 512 字节预览切掉
 * （2026-11-09 Q2026110901673：exit_code=0 但节点从未建任务，现场查不到真因）。
 * 只在任务失败时写进 detail.create_output：正常成功不产生额外写库。
 * detail 在服务端按字典合并，不会冲掉 baseline_report_hash / probe。
 */
let createOutputEvidence = ''

interface ModuleOption {
  value: LogCollectModule
  label: string
  /** 以下字段来自平台场景字典；缺失表示该枚举尚未维护文案 */
  summary?: string
  collects?: string
  typicalSymptoms?: string
  durationHint?: string
  /** 与其他场景互斥（字典能力，当前无项启用）：选中它时其余选项置灰不可勾 */
  exclusive?: boolean
  /** 字典里的展示顺序；未维护时排在已维护项之后 */
  sortOrder?: number
  /** 是否命中字典（决定是否显示"平台未维护说明"提醒） */
  documented?: boolean
}

interface HostOption {
  ip: string
  name?: string
  master?: boolean
}

// 内置场景目录：只在**探测失败时**兜底使用。
// 第一性原理：能力边界在集群（acli help 的 -m 枚举），展示文案在平台字典（log_collect_scenario）。
// 这里的短名是两层都拿不到时的最后一道兜底，不得再当作选项的权威来源。
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
  /** acli 编译时间，bridge 一直有回传，早期前端未声明导致只显示版本号 */
  acli_build_time?: string
  /** 本次探测真正命中的 acli 路径：同机存在多份 acli 时，它解释「为何与手敲版本不同」 */
  acli_binary?: string
  available_mb: number
  log_server_reachable: boolean
  /**
   * 日志服务器 HTTP 码（0 = 一个响应字节都没收到）。
   *
   * 收敛后的字段：bridge 回传的是 `log_server_http_code`，平台存档与旧单测用 `http_code`，
   * 两者都由 normalizeProbe 归一到这里。早期前端只声明了 `http_code`，而 bridge 从不发它，
   * 真值永远落空——随任务落库的探测快照因此长期缺 `http_code`，事后无法判断
   * 「日志服务器当时是没回话，还是回了 5xx」。
   */
  http_code: number
  /** bridge 原始字段（见上）：保留声明只为归一化时有据可依，业务代码一律读 http_code */
  log_server_http_code?: number
  /** 探测耗时：失败时是「耗到超时」的秒数，随快照落库，供事后区分丢包与拒绝 */
  log_server_latency_ms?: number
  /** 本次探测的目标地址（随快照落库，便于事后核对探的是哪个地址） */
  log_server_url?: string
  /**
   * 节点 acli 是否支持注入上传请求头（`-L|--url-header`）。
   *
   * 这是 P1 能否带凭据上传的节点侧前提：5888 启用鉴权后，acli 必须能把
   * 平台签发的票据注入成请求头才过得去；老版本没有该参数，直传必然 401。
   * `undefined` 表示探测端未升级，按"未知"处理，不禁用 P1。
   */
  acli_supports_upload_header?: boolean
  // 以下字段由 bridge 从目标集群读取；缺省表示探测降级，前端必须能继续工作
  modules?: ModuleOption[]
  hosts?: HostOption[]
  catalog_source?: 'acli_help' | 'platform_default'
  catalog_note?: string
  /** 日志服务器探测失败原因（bridge 原话，只做排障佐证，不参与结论） */
  note?: string
}

type PrepareState = 'idle' | 'probing' | 'ready' | 'failed'

interface ProgressSnapshot {
  status: string
  percent: number
  done: number
  total: number
  archive_path: string
  /**
   * 压缩包字节数，由 bridge 在上传窗口内 stat 得到。
   * 插件 progress 本身不打印大小，P1 成功后包会被自删，所以经常为 0（= 未量到）。
   */
  archive_size?: number
  /**
   * 压缩包此刻是否真的在节点上（bridge 的 stat 结论，三态）。
   *
   * 与 `archive_path` 的区别是决定性的：路径只说明"曾经生成过"，
   * P1 发送成功后插件会立刻删除文件，报告里却仍带路径。
   * 缺省（旧版 bridge / 探测命令不可用）时必须当作"未确认"，不能当作"不存在"。
   */
  archive_exists?: boolean
  upload_state: string
  /**
   * acli 自报的总耗时（`00:03:38.9` / `58:11`），bridge 已按 `总耗时`/`Total time` 归一。
   *
   * 它是**独立于文本指纹**的归属证据：任务只存在 3 秒、报告却自称跑了 3 分 38 秒，
   * 这份报告必然属于更早的任务。解析失败（缺省）时不做否决。
   */
  total_time?: string
  hosts: Array<{ ip: string; status: string; percent: number }>
  /** 节点态归一化结果：none / running / completed / abnormal / unknown */
  node_state?: NodeState
  /**
   * 报告原文：与 create 前的基线比较，判断报告是否已刷成本次任务。
   *
   * 这是唯一可靠的归属证据——不依赖文件是否存在（P1 成功后压缩包会被插件自删）。
   */
  raw?: string
}

const stage = ref<Stage>('idle')
// 失败发生在第几步：stage 变成 failed 之后，阶段条必须停在真实位置，
// 否则无论在哪一步挂掉都只亮「探测」，看起来像根本没开始。
const failedStep = ref(0)

/**
 * acli 安装这一跳的实时事实（下载 → 上传 → 校验 → 安装）。
 *
 * 与 stage 的分工：stage 回答“日志收集流程走到哪”，这里回答“装工具这一步
 * 到底错在哪、包对不对”。上传失败时只抛一句 error 的话，用户无法回答
 * “是包被传坏了，还是安装器失败”，也就不知道该点哪个兜底按钮。
 */
const acliLive = reactive({
  status: '',
  message: '',
  percent: 0,
  packageSize: 0,
  sha256: '',
  md5: '',
  integrity: '',
  attempts: 0,
  installOutput: '',
})

/** acli 安装失败态：非空即渲染兜底卡片（含校验摘要与重试按钮）。 */
const acliFailure = ref<null | {
  message: string
  stage: string
  suggestions: AcliRetryStage[]
  facts: AcliSyncFacts
}>(null)
/** 重试进行中：按钮置灰，避免用户连点造成两个上传叠在同一个 destPath 上。 */
const acliRetrying = ref(false)
const probeResult = ref<ProbeResult | null>(null)
const pathMode = ref<'P1' | 'P2' | ''>('')
/**
 * 用户是否手动选过交付路径。
 *
 * 探测结果只提供**默认值**：可达性判据可能因探测时机（5888 临时抖动、ACL 未放通）
 * 而误判，用户若已明确选择就必须尊重，否则每次点「开始收集」都被静默改回。
 */
const pathModeTouched = ref(false)

/**
 * P1 是否可选。
 *
 * 第一性原理：两条路径不是偏好，而是由可达性决定的物理选择——HCI 连不上 5888 时，
 * P1 不是「差一点的选择」，而是注定失败的那一个。把一个必然失败的选项摆给用户，
 * 等于要求他先用一次失败去验证一个平台已经知道的事实，所以直接置灰。
 */
const p1Disabled = computed(() => {
  const probe = probeResult.value
  if (!probe) return false
  if (!probe.log_server_reachable) return true
  // 可达只说明网络通，不代表能上传：5888 启用鉴权后，acli 必须能把票据
  // 注入成请求头才过得去。老版本 acli 没有 -L，直传必然 401。
  //
  // 只在明确 false 时禁用：老 bridge 不返回该字段（undefined），
  // 此时按"未知"处理，保留 P1 可选，避免升级顺序把用户卡在 P2 上。
  return probe.acli_supports_upload_header === false
})

/** 探测给出的 P1 不可用原因（供置灰时的提示文案使用）。 */
const p1UnavailableReason = computed(() => {
  const probe = probeResult.value
  if (!probe) return ''
  if (!probe.log_server_reachable) return '节点连不上日志服务器'
  if (probe.acli_supports_upload_header === false) return '节点 acli 版本不支持上传鉴权头，需升级 acli'
  return ''
})
const currentTask = ref<LogCollectTask | null>(null)
// spc 场景：平台清单是否仍覆写在节点的 core 槽位上（true = 需要还原）。
// 只当"要不要发 restore_spc"的开关用，不参与采集状态判定——采集可能早已 completed，
// 而槽位还原是独立于采集的生命周期事实。
const spcPendingRestore = ref(false)
// 逐台写入证据：还原只能靠 backup 名（<file>.platform.<task_id>.bak）重放，
// 因此这几项必须随任务落库并展示，否则换端后无法判断哪台残留平台清单。
const spcHosts = ref<Array<{ ip: string; backup?: string; error?: string }>>([])
// 面板视图：form = 新建收集；result = 该工单已完成收集的只读结果
const viewMode = ref<'form' | 'result'>('form')
const resultTask = ref<LogCollectTask | null>(null)
// 该工单历史任务总数：用于告知"已完成是既成事实，可以重新收集但不是必须"
const historyTotal = ref(0)
/**
 * 本工单最近的若干条记录（最多 5 条）。
 *
 * 只展示"最近一条"是不够的：同一工单经常有多条（本次现场 4 条），用户需要能确认
 * 面板说的到底是哪一次，并主动切回去看。缺少这个入口时，"展示错了任务"会被
 * 直接读成"系统判错了交付路径"（Q2026102701193）。
 */
const historyTasks = ref<LogCollectTask[]>([])
const progress = ref<ProgressSnapshot | null>(null)
/**
 * 本次任务过程中观测到的最大压缩包字节数（0 = 还没量到过）。
 *
 * 压缩包大小是「文件存在期间」的常量：打包完成后不再变化，之后每次采样只会因为
 * 「P1 上传成功、插件自删包」而变成 0。所以正确聚合方式是取**峰值**。
 *
 * 为什么必须单独记住：完成判据是「上传成功」，而这一刻包恰好已被删除——
 * 只取"完成那一份快照"的话 archive_size 必然为空（现场 Q2026101401683 的 40 条
 * progress 事件全空）。过程中的观测值被时间冲掉了，这正是「未记录」的真正成因。
 */
const observedArchiveSize = ref(0)
/**
 * 本次下发的节点报告基线（报告指纹）。
 *
 * 第一性原理：acli 的 progress 是 **ticket 维度**的，同一工单重复 create 时
 * 头几次读到的仍是上一次任务的报告。唯一能区分"这份报告属于哪次下发"的信号，
 * 是"它和下发前那一份是否不同"。
 *
 * 基线必须跟着任务走（存 `log_collect_task.detail`），不能只活在页面内存里：
 * 关面板、换浏览器、重启后接管任务时，内存里的基线早就没了。
 */
const baselineReportHash = ref('')
/**
 * L1 正向证据：本次下发是否**亲眼见过节点 running（采集起点）**。
 *
 * acli 是节点级单任务，`create` 不回句柄、`progress` 也不带任务 ID，所以"插件已经
 * 接单并开始跑我的任务"这件事，只能由一次 running 观测来证明。没见到它之前，
 * 任何 completed 报告都可能是上一次任务的残留（2026-11-06 Q2026110666667：
 * 22:08:42 建任务，22:08:45 就拿到上一轮 100%，而节点 22:09:31 才从 0% 开始）。
 *
 * 内存态只覆盖"本次会话"；跨页面刷新由 `task.progress > 0` 兜底（只有 running 才写进度）。
 */
const sessionAdopted = ref(false)
/**
 * L1 门开关：本次会话**亲自 create** 的任务才要求先看到采集起点。
 *
 * 接管历史任务时必须关掉——用户关页面、过一阵重开去接管一个早已跑完的任务时，
 * 永远不可能再看到 running，开着门就会把真实完成永久挡住（Q2026101401683）。
 */
const requireSessionAdopt = ref(false)
/**
 * 本次下发过程中是否观测到过上传阶段（`upload_state` 为 sending/uploading）。
 *
 * 用于识别「采集完成瞬间的假 Success」：acli 实测会先报 Success、再回落 Sending 跑很久。
 * 从未见过上传过程时，那句 Success 不足以证明包已经到了日志服务器。
 */
const uploadPhaseSeen = ref(false)
// 对账提示：用于告诉用户"节点态与平台态不一致、下一步该做什么"
const nodeSyncHint = ref('')
// 对账时读到的节点压缩包路径：补交付必须用「节点实际报告的那一份」，
// 不能用 task.archive_path（P2 补交付场景里平台侧可能压根没记过）。
const nodeArchivePath = ref('')
// 僵尸任务/游离任务的一键动作（取消并重试 / 接管），空串表示无需处理
const nodeAction = ref<'' | 'cancel_retry' | 'adopt' | 'correct'>('')
// 接管按钮语义：resume=节点还在跑，接上进度；deliver=节点已采完，接着交付
const nodeAdoptMode = ref<'resume' | 'deliver'>('resume')
const transfer = reactive({
  // bridge 交付作业的原始阶段（waiting_collect / pulling / uploading / …）：
  // 文案要区分「还没开始传输」与「正在拉取 0 字节」，只有它能分辨。
  stage: '',
  percent: 0,
  pulled: 0,
  // 源包大小（job 的 source_size）：没有分母的「已拉 N 字节」无法判断还要多久。
  totalSize: 0,
  uploadedParts: 0,
  partCount: 0,
  sha256: '',
})
/** 交付进度整组归零：新任务不得继承上一轮的分母、阶段与摘要。 */
function resetTransfer() {
  transfer.stage = ''
  transfer.percent = 0
  transfer.pulled = 0
  transfer.totalSize = 0
  transfer.uploadedParts = 0
  transfer.partCount = 0
  transfer.sha256 = ''
}
const relayStatus = ref<RelayStatus>('none')
// 代传进度明细（第几片 / 第几轮 / 上次失败原因）。relayStatus 只有五态，
// 单片超时 300s 时它一直停在 running，看不出"在传"还是"卡死"，所以另存一份快照。
const relayProgress = ref<RelayProgressView | null>(null)
const cleanupTip = ref('')
const errorMessage = ref('')
// 失败诊断：只按规范码给「检查/处理」，不用文案猜。
// 任务刚失败时 patch 会回填 currentTask，所以这里能立刻拿到最新码。
// 结果态看的是 resultTask：用户隔天回来查看时，走的不是表单流程，
// 之前只读到 currentTask 会让诊断卡在结果态整块消失。
const failureTask = computed(() => (viewMode.value === 'result' ? resultTask.value : currentTask.value))
const failureCode = computed(() => String(failureTask.value?.error_code || '').trim())
const failureHint = computed(() => logCollectErrorHint(failureCode.value))
const failureReasonText = computed(() => {
  const local = viewMode.value === 'result' ? '' : errorMessage.value
  return local || failureTask.value?.error_message || ''
})
/**
 * 诊断卡的显示条件：本轮跑失败了（stage=failed），或展示的任务本身就是失败/取消终态。
 * 取消也要给原因：用户点了取消却看到"任务失败"，会以为是自己误操作导致的数据丢失。
 */
const showFailureDiagnosis = computed(
  () =>
    stage.value === 'failed' ||
    failureTask.value?.status === 'failed' ||
    failureTask.value?.status === 'cancelled',
)
// 运行令牌：每次启动 / 取消都自增，后台轮询据此退出，避免取消后仍继续写状态
let runToken = 0

/**
 * 主流程是否正在跑（一次 handleStart 从下发到交付收尾）。
 *
 * 为什么需要这把锁：主流程（pollUntilCollected → runP2）与 5 秒自动对账是两条
 * **都会写平台状态**的链路。主流程在 resumeTaskFlow 入口停掉了定时器，但用户
 * 中途关掉再打开抽屉时，restoreTaskState 会把定时器重新拉起来（那是为了接管
 * 别人的任务），于是同一实例里两条链路并行 —— 对账先把 status 写成 completed，
 * 主流程随后的 PATCH uploading 被状态机以 409 拒绝，交付在 0 片时被抛断
 * （2026-11-09 Q2026110900356：平台已开会话、桥一个分片都没收到）。
 *
 * 约束：主流程在跑时，自动/手动对账一律不写库（只读展示由主流程自己负责）。
 */
const mainFlowActive = ref(false)
/** 主流程代际：只有最新一代才能关闭 mainFlowActive，旧流程的 finally 不得抢新流程的锁。 */
let mainFlowSeq = 0

const form = reactive({
  modules: ['vm_suspend'] as LogCollectModule[],
  dateRange: [] as string[],
  ips: 'all',
  /**
   * 额外收集路径（acli `-p`）。
   *
   * 第一性原理：`-p` 是本次采集范围的组成部分，不是"高级玩法"——
   * 客户想收业务自定义目录时，没有它就只能手工上节点执行 acli，
   * 收集结果会脱离平台的交付链（分片、摘要、P1/P2 都不覆盖）。
   *
   * 为什么必须在这里校验而不是交给后端：插件把路径直接拼进 `cp -r`（未加引号），
   * 非法字符不是"收不到"，而是能在客户节点执行命令；提前拒绝才是安全边界。
   */
  extraPaths: [] as string[],
  /**
   * 场景模式：default = 现有专项多选；custom = 平台自定义场景（acli -c）。
   *
   * 视觉上是互斥的两块，但底层**叠加**：自定义模式固定下发 `-m other`，
   * 配置只追加路径（config.modules 永不生效）。界面不展示专项，
   * 是为了不误导用户以为"选了自定义就没有基础日志"。
   */
  scenarioMode: 'default' as 'default' | 'custom',
  customScenarioId: '',
})

const isCustomMode = computed(() => form.scenarioMode === 'custom' && Boolean(form.customScenarioId))
const selectedCustomScenario = computed(() =>
  customScenarios.value.find((item) => item.scenario_id === form.customScenarioId) ?? null,
)

/** 高级选项默认收起：额外路径是补充手段，不是每次采集都要经过的决策点。 */
const advancedOpen = ref<string[]>([])
/** 额外路径输入框的暂存值：只有回车/失焦才真正入列，避免半截路径被提交。 */
const extraPathInput = ref('')
const extraPathsLimitReached = computed(() => form.extraPaths.length >= EXTRA_PATH_MAX_COUNT)
const extraPathsHint = computed(() => extraPathsLimitHint(form.extraPaths.length))
/** 最近一次添加的失败原因（一次展示多条，不逐条弹窗）。 */
const extraPathError = ref('')

function addExtraPaths() {
  const raw = extraPathInput.value.trim()
  if (!raw) return
  extraPathError.value = ''
  const { accepted, rejected, overflow } = parseExtraPathInput(raw, form.extraPaths)
  if (accepted.length) form.extraPaths.push(...accepted)
  extraPathInput.value = ''
  if (rejected.length) {
    extraPathError.value = rejected
      .slice(0, 3)
      .map((item) => `${item.value}：${item.reason}`)
      .join('；')
    if (rejected.length > 3) extraPathError.value += `；另有 ${rejected.length - 3} 条未通过`
  }
  if (overflow > 0) {
    extraPathError.value = [extraPathError.value, `超出上限 ${EXTRA_PATH_MAX_COUNT} 条，已忽略 ${overflow} 条`]
      .filter(Boolean)
      .join('；')
  }
}

function removeExtraPath(path: string) {
  form.extraPaths = form.extraPaths.filter((item) => item !== path)
}

/**
 * 发起节点 = 当前 SSH 会话的目标主机，是既成事实，不是用户输入。
 *
 * 第一性原理：bridge 的会话键是 `caseID@nodeIP`，一切 bridge 动作都必须路由回
 * 「pty 正在运行的那台机器」。用户无法通过填一个 IP 去"连上"另一台机器——
 * 手填与真实会话不一致时只会得到 session_missing（LC-ENV-006）。
 * 因此唯一真值只能来自会话本身，UI 只做展示，不提供编辑入口。
 *
 * 对抗性审查：`lastSshHost` 是 localStorage 里的「上一次连过的主机」，跨刷新、跨重登都保留。
 * 它只能用于预填登录表单，绝不能当作「当前会话存在」——早先 sessionHost 在断线时回退到它，
 * 界面于是继续显示「172.x 当前 SSH 会话」，用户点开始收集必然收到 session_missing。
 */
const sshConnected = computed(() => chatStore.sshConnectionState === 'connected')

const sessionHost = computed(() => {
  if (!sshConnected.value) return ''
  return (chatStore.sshCurrentConfig?.host || '').trim()
})
// 会话主机优先；props.nodeIp 仅作为调用方显式指定时的兜底（当前 App.vue 不传）
const nodeIp = computed(() => (sessionHost.value || props.nodeIp || '').trim())

// ── 准备阶段（打开面板即探测，避免用户填完表单才发现环境不可用）────────────
// 集群返回的能力边界（bridge probe 的 -m 枚举）；字典只叠加文案，不参与“能不能选”。
const probeModules = ref<ModuleOption[]>([...BUILTIN_MODULES])
/**
 * 平台场景字典快照。
 *
 * 空数组有三种含义（未拉到 / 未维护 / 已停用），对界面而言是同一种降级：
 * 回落到 bridge 短名 + 标记“平台未维护说明”。字典失败绝不能拦住探测与采集。
 */
const scenarioDict = ref<LogCollectScenarioItem[]>([])
/**
 * 平台自定义场景（acli -c），只含元数据。
 *
 * 与字典同一次探测拉取、同样失败降级：一个后台配置服务不可用，
 * 不该让整个日志收集面板变成"不可用"。
 */
const customScenarios = ref<LogCollectCustomScenarioItem[]>([])
const scenarioDictLoaded = ref(false)
/** 字典不可用时的说明（区分“未维护”与“读取失败”，不拼成一句含混提示） */
const scenarioDictNote = ref('')
/** 默认场景清单只读预览：失败降级为空，不挡原生采集。 */
const defaultManifests = ref<LogCollectDefaultManifest[]>([])
const selectedManifestPreviews = computed(() => {
  const selected = form.modules as string[]
  return defaultManifests.value.filter((item) => selected.includes(item.module))
})
const moduleOptions = computed<ModuleOption[]>(() =>
  mergeModuleOptions(probeModules.value, scenarioDict.value),
)
const hostOptions = ref<HostOption[]>([])
const prepareState = ref<PrepareState>('idle')
const prepareError = ref('')
// 本次探测完成的时刻：探测结论是快照（acli 可能被自动升级），界面必须显式说明"何时读的"。
const probedAt = ref<number | null>(null)
// 准备令牌：节点 IP 变更会重探，旧探测结果不得覆盖新结果
let prepareToken = 0

// ── 探测重试与事实复查 ───────────────────────────────────────────────────
/**
 * 瞬态失败的重试节奏：2s / 4s / 8s（指数退避 + ±20% 抖动）。
 *
 * 第一性原理：探测失败只有两类——「这次没成」与「这里就是这样」。
 * 超时、通道被踢、bridge 无回包属于前者，重试有意义；acli 未安装、节点清单为空、
 * 日志服务器不可达属于后者，重试一万次也不会变，只能等外部事实改变。
 * 把两类塞进同一个重试循环，就会在「acli 没装」上空转到上限，把真因刷成噪音。
 */
const PROBE_RETRY_DELAYS = [2000, 4000, 8000]
/** 事实复查节奏：acli 可能被后台安装流程装上、节点可能上下线，低频看一眼足够。 */
const PROBE_RECHECK_INTERVAL_MS = 30000
const PROBE_RECHECK_MAX = 3
/**
 * 探测卡死自愈阈值。
 *
 * 对抗性审查：旧实现用 `prepareState === 'probing'` 挡住重入，且早退时静默 return。
 * 一旦某次探测的等待器丢失（回包被同 action 的后到请求覆盖、组件重挂载丢 waiter），
 * 状态会永远停在 probing——用户点「重新探测」毫无反应，只有 F5 重建组件才能恢复
 * （2026-10-30 现场：装完 acli 后必须刷新页面面板才更新）。
 * 因此重入判据必须是「这次探测已经跑了多久」，而不是「状态是不是 probing」。
 */
const PROBE_STALL_MS = 120000

/** 本次探测开始的时刻：只用于判定「是否已卡死」，不参与展示。 */
const probingStartedAt = ref(0)
/** 当前这一轮自动重试的进度（null = 没有在重试）。 */
const probeRetry = ref<{ attempt: number; total: number; delayMs: number } | null>(null)
/** 事实复查已执行次数：手动重探、换工单、换节点都会归零。 */
const probeRecheckCount = ref(0)
let probeRecheckTimer: ReturnType<typeof setTimeout> | null = null

/** 重试间隔加 ±20% 抖动：多客户端同时探测时避免同相位冲击同一个节点。 */
function probeRetryDelay(index: number): number {
  const base =
    PROBE_RETRY_DELAYS[Math.min(index, PROBE_RETRY_DELAYS.length - 1)] ?? PROBE_RETRY_DELAYS[0]
  return Math.round(base + base * (Math.random() * 0.4 - 0.2))
}

/**
 * 「没有可用会话」的唯一文案。
 *
 * 措辞必须指向**本面板里就能完成的动作**（底部「连接 SSH」），而不是让用户自己去
 * 右侧终端找入口——这正是 2026-11-02 现场反馈的症结：按钮禁用、文案却只说"请连接"。
 */
const NODE_MISSING_MESSAGE = 'SSH 会话未连接：请点击面板底部「连接 SSH」登录后再读取环境'

/**
 * 该失败是否值得重试。
 *
 * 只有「重连 SSH 才能解决」的失败被排除：session_missing / LC-ENV-006 / 未连接节点。
 * 其余（超时、通道断开、未知桥侧异常）一律按瞬态处理——未知错误重试一次的代价，
 * 远小于让用户自己发现「其实再点一次就好了」。
 */
function isRetryableProbeError(error: unknown): boolean {
  const text = String((error as any)?.message || error || '')
  if (text.includes('SSH 会话未连接')) return false
  if (text.includes('LC-ENV-006') || text.includes('session_missing')) return false
  if (text.includes('会话不存在')) return false
  return true
}

function probeErrorText(error: unknown): string {
  return String((error as any)?.message || error || '').slice(0, 200)
}

/** 重试提示文案：必须同时说明「失败原因」「等多久」「第几次」，否则与卡死无法区分。 */
const probeRetryText = computed(() => {
  const retry = probeRetry.value
  if (!retry) return ''
  const seconds = Math.max(1, Math.round(retry.delayMs / 1000))
  const reason = prepareError.value || '原因未记录'
  return `环境探测失败（${reason}），${seconds} 秒后自动重试（第 ${retry.attempt}/${retry.total} 次）`
})

/**
 * 探测结论里「会被外部改变、但不算探测故障」的项。
 *
 * 成功标准（2026-10-30 与现场确认）：通道通 + acli 已装 + 节点清单非空。
 * 日志服务器可达性不参与成功判定（它是网络现状，不是探测故障），但仍进复查范围——
 * 权限放开后同一份快照就该被刷新。
 */
function needsFactRecheck(data: ProbeResult | null | undefined): boolean {
  if (!data) return false
  if (!data.acli_installed) return true
  if (!data.hosts?.length) return true
  if (!data.log_server_reachable) return true
  return false
}

function stopFactRecheck() {
  if (probeRecheckTimer) {
    clearTimeout(probeRecheckTimer)
    probeRecheckTimer = null
  }
}

/**
 * 安排一次事实复查。
 *
 * 与重试的关系：复查不复用重试（`retries: false`），否则一次瞬态失败会被放大成
 * 3×3 次 SSH 调用；复查次数也不因成功而归零，只由用户动作（手动重探/换单/换节点）复位。
 */
function scheduleFactRecheck() {
  stopFactRecheck()
  // 采集中不复查：探测要连发多条 SSH 命令，会和采集抢同一个会话
  if (!visible.value || running.value) return
  if (!needsFactRecheck(probeResult.value)) return
  if (probeRecheckCount.value >= PROBE_RECHECK_MAX) return
  probeRecheckTimer = setTimeout(() => {
    probeRecheckTimer = null
    if (!visible.value || running.value) return
    probeRecheckCount.value += 1
    void prepare({ silent: true, retries: false, resetRecheck: false })
  }, PROBE_RECHECK_INTERVAL_MS)
}

const nodeIpReady = computed(() => Boolean(nodeIp.value))

/**
 * 「开始收集」此刻是否真的能下发：会话活着 **且** 能确定发起节点。
 *
 * 两个条件缺一不可：会话断了谈不上采集；会话在但读不到主机时，
 * 采集消息没有 `node_ip`，bridge 的 `caseID@nodeIP` 一定查不到会话（LC-ENV-006）。
 * 这个派生值同时决定主按钮的语义（见 handlePrimaryAction）。
 */
const canCollect = computed(() => sshConnected.value && nodeIpReady.value)

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

/** IPv4 字面量校验：与 bridge/平台两侧规则同口径（4 段、每段 0-255）。 */
function isIPv4Literal(value: string): boolean {
  const parts = value.split('.')
  if (parts.length !== 4) return false
  return parts.every((part) => /^\d{1,3}$/.test(part) && Number(part) <= 255)
}

/**
 * 把表单里的目标主机规整成 bridge 唯一认得的形态（`all` 或逗号分隔 IPv4）。
 *
 * 第一性原理：`acli -i` 只有两种合法输入——`all`，或逗号分隔的 IPv4；
 * 它们互斥（"全部"已包含"其中某一台"），因此 all+IP 必须当场拒绝，
 * 而不是让某一层替用户做取舍。
 *
 * 对抗性审查：必须在前端拦，因为两侧判罚并不对称——平台 `ips` 校验看到
 * `all` 后按"全部"放行，bridge 却按原串逐项校验判非法；不拦的话用户拿到的是
 * 「平台已建任务、节点下发失败」这种半成品状态。
 *
 * 返回 null 表示非法（调用方负责提示），'all' 表示全部在线节点。
 */
function normalizeTargetHosts(): string | null {
  const hosts = form.ips
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean)
  if (!hosts.length) return 'all'
  if (hosts.includes('all')) return hosts.length === 1 ? 'all' : null
  return hosts.every(isIPv4Literal) ? hosts.join(',') : null
}

/**
 * 手填兜底提示：必须同时回答"为什么没有清单"和"该填什么"。
 *
 * 第一性原理：用户能填的值只有三种——all、单个 IP、英文逗号分隔的 IP，
 * 因此提示直接给格式与示例，而不是让用户去猜 acli 的参数名（-i 是内部契约，不该出现在界面）。
 * 对抗性审查：节点清单的唯一来源是 `acli platform node list`，acli 未安装时清单必然为空；
 * 不说明这一点，用户会把"平台读不到节点"误判成平台故障。
 */
const manualHostHint = computed(() => {
  const format = 'all（全部在线节点）或英文逗号分隔的 IP，如 172.28.25.2,172.28.25.3'
  if (probeResult.value && !probeResult.value.acli_installed) {
    return `acli 未安装，无法自动读取节点清单；可手填 ${format}，开始收集时会先安装 acli`
  }
  return `未读取到节点清单，可手填 ${format}`
})

/**
 * 展示用 acli 版本文案：版本号 + 编译时间。
 *
 * 第一性原理：`acli --version` 的两行输出才是完整事实（1.0.1 / build 2026-09-15 21:00:04），
 * 只显示主版本会让"同版本不同构建"无法区分——排障时恰恰要按 build 比对。
 * 对抗性审查：旧后端不回传 build（字段缺省）时只显示版本号，不能显示 "build undefined"。
 */
const acliVersionText = computed(() => {
  const probe = probeResult.value
  if (!probe) return ''
  if (!probe.acli_installed) return '未安装'
  const build = (probe.acli_build_time || '').trim()
  return build ? `${probe.acli_version} (build ${build})` : probe.acli_version
})

/** 探测命中的 acli 路径（悬停可见）：与手敲结果不一致时，第一眼要能看出是哪份二进制。 */
const acliTitle = computed(() => {
  const binary = probeResult.value?.acli_binary
  return binary ? `实际探测的二进制：${binary}` : ''
})

/** 探测时间：面板关闭再打开会重探，用户需要知道眼前这组值是何时读的。 */
const probedAtText = computed(() =>
  probedAt.value ? new Date(probedAt.value).toLocaleTimeString('zh-CN', { hour12: false }) : '',
)

/**
 * 环境探测区只保留可观测的**能力出处**：集群是否返回过枚举。
 * 文案出处属于平台内部实现细节，不再展示给客户。
 */
const catalogSourceText = computed(() => {
  if (prepareState.value === 'probing') return '正在从集群读取可选场景…'
  if (!probeResult.value) return '尚未读取集群能力，当前展示平台内置项'
  return probeResult.value.catalog_source === 'acli_help'
    ? '可选场景由集群 acli 提供'
    : '平台内置场景列表（集群未返回枚举）'
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

/**
 * 交付路径的判定口径。
 *
 * `upload_mode` 是后补字段：2026-10-23 之前完成的任务没有它（DB 为 NULL），
 * 直接按枚举展示会出现「交付路径：未记录 / 归档状态：none」这种内部值泄漏。
 * P1 创建时一定带 `remote_url`，用它兜底即可区分 P1 与「未知」，且只用于展示、不回写。
 */
const resultMode = computed<'acli_direct' | 'bridge_relay' | ''>(() => {
  const task = resultTask.value
  if (!task) return ''
  if (task.upload_mode === 'acli_direct' || task.upload_mode === 'bridge_relay') return task.upload_mode
  return task.remote_url ? 'acli_direct' : ''
})

/**
 * 结果态标题与描述：必须说清"这是哪一次、结果如何"。
 *
 * 写死"已完成"会让失败任务的卡片撒谎——而用户正是靠这一行判断"这次到底成没成"。
 * 同理，"采集完成"也不等于"日志已交付"：判定统一交给 buildResultHeadline，
 * 由**交付证据**决定横幅颜色，避免绿色 success 与"平台零产物"同屏（Q2026110900356）。
 */
const resultHeadline = computed<{ type: 'success' | 'warning' | 'error' | 'info'; title: string; description: string }>(
  () =>
    buildResultHeadline({
      status: resultTask.value?.status,
      delivered: resultDelivered.value,
      when: resultFinishedText.value,
      // 「还没核验到」和「日志服务器上确实没有」是两件事：横幅必须带上核验时刻与观测结果，
      // 否则用户只能理解成"平台说我没传"（Q2026111000243 的现场反应）。
      archiveCheck: p1ArchiveCheckOf(resultTask.value),
      canUpload: canUploadFromNode.value,
      // 平台派生结论优先：有证据时不让本地 status/delivery 再拼一个「已完成」出来。
      display: resultTask.value?.display ?? null,
      artifact: resultTask.value?.artifact ?? null,
      // 结论不能吞掉诊断信息：错误码与结论仍需同时上第一屏，
      // 让用户第一眼就知道该查哪一层，而不是把"重新收集"当诊断手段。
      errorCode: resultTask.value?.error_code,
      hintTitle: logCollectErrorHint(resultTask.value?.error_code)?.title ?? null,
    }),
)

/** capabilities 探测结论：旧桥不认识 log_collect_evidence 时显式提示，不静默降级。 */
const bridgeEvidenceCapable = ref(true)
const platformTransfer = computed(() => currentTask.value?.transfer ?? resultTask.value?.transfer ?? null)
const platformNetwork = computed(() => currentTask.value?.network ?? resultTask.value?.network ?? null)
const platformDisplay = computed(() => currentTask.value?.display ?? resultTask.value?.display ?? null)

/**
 * 「没有数据」同页只说一次，且必须可归因：
 * 未进入传输/探测环节 → 如实说未进入，不能赖到桥接端版本上；
 * 明确旧桥 → 说版本旧；其余 → 说尚无上报记录。文案全部来自契约。
 */
const evidenceHint = computed(() => {
  if (!platformDisplay.value) return ''
  const failedBeforeTransfer =
    (stage.value === 'failed' || platformDisplay.value.conclusion === 'failed') &&
    !platformTransfer.value && !platformNetwork.value
  if (failedBeforeTransfer || platformDisplay.value.phase === 'queued') {
    return MISSING_DATA_TEXT.not_reached
  }
  const outdated = platformDisplay.value.conditions?.some(item => item.key === 'bridge_outdated')
  if (!bridgeEvidenceCapable.value || outdated) return MISSING_DATA_TEXT.bridge_outdated
  if (!platformTransfer.value && !platformNetwork.value) return MISSING_DATA_TEXT.genuinely_empty
  return ''
})

/** 失败且未进入采集时，「未取到有效日志」是必然结果，不作为独立观察事实重复展示。 */
const visibleConditions = computed(() => {
  const list = platformDisplay.value?.conditions ?? []
  if (platformDisplay.value?.conclusion !== 'failed') return list
  const skip = RENDER_POLICY.conditions_skip_when_not_collected as readonly string[]
  return list.filter(item => !skip.includes(item.key))
})

function formatEtaText(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return '—'
  if (seconds < 60) return `${Math.round(seconds)} 秒`
  const minutes = Math.floor(seconds / 60)
  return minutes < 60 ? `${minutes} 分 ${Math.round(seconds % 60)} 秒` : `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`
}

const RESULT_STATUS_TEXT: Record<string, string> = {
  completed: '完成',
  failed: '失败',
  cancelled: '已取消',
}

/**
 * 历史记录的短标签：时间 + 结论 + 交付路径。
 *
 * 三项缺一不可——用户判断"是不是我看的那次"靠时间，判断"该不该重收"靠结论，
 * 而"我明明选了 P2"这类质疑只能靠路径自证（Q2026102701193）。
 */
function historyLabel(task: LogCollectTask): string {
  const raw = task.finished_at || task.created_at
  const date = raw ? new Date(raw) : null
  const time =
    date && !Number.isNaN(date.getTime())
      ? `${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')} ` +
        `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`
      : '时间未记录'
  const path =
    task.upload_mode === 'acli_direct'
      ? 'P1'
      : task.upload_mode === 'bridge_relay'
        ? 'P2'
        : '路径未记录'
  return `${time} · ${RESULT_STATUS_TEXT[task.status] ?? task.status} · ${path}`
}

/** 切换展示记录：归档状态与上传入口都基于"当前展示的那条"，切换后必须重新对账 */
function switchDisplayTask(taskId: string) {
  if (taskId === resultTask.value?.task_id) return
  const task = historyTasks.value.find((item) => item.task_id === taskId)
  if (!task) return
  showResult(task)
  // 主流程在跑时它自己就是对账者：这里再发一次会与它抢写同一行（见 mainFlowActive）。
  if (sshConnected.value && !mainFlowActive.value) {
    void syncNodeState({ applyPatches: true, silent: true, source: 'manual' })
  }
}

const resultDeliveryText = computed(() => {
  if (resultMode.value === 'acli_direct') return 'P1 直传日志服务器'
  if (resultMode.value === 'bridge_relay') return 'P2 经平台中转'
  return '未记录'
})

/**
 * 交付路径的默认值与可选性。
 *
 * 第一性原理：这项选择的正确值只取决于一个平台已经知道的事实（HCI 能不能直连 5888），
 * 所以平台直接给结论，不让用户做选择题：
 *   - 可达 → 默认 P1（少一跳中转），用户仍可手动改 P2；
 *   - 不可达（含 5xx / 超时 / 无响应等一切异常）→ 强制 P2，P1 置灰不可选。
 * 用户从未手选时才套默认值；但不可达是硬约束，即使他之前选过 P1，也要收回——
 * 给一个必然失败的选项不是尊重，而是挖坑。
 */
function applyDefaultPathMode() {
  const probe = probeResult.value
  if (!probe) return
  if (!probe.log_server_reachable) {
    // 不可达（含 5xx）是硬约束：即使之前手选过 P1，也要收回。
    pathMode.value = 'P2'
    return
  }
  // 可达：用户没手选过就回到 P1。这里不能写成 `!pathMode.value`——否则
  // 「先判不可达（程序置 P2）→ 重探恢复可达」会永久停在 P2，比实际能力差一跳。
  if (!pathModeTouched.value) pathMode.value = 'P1'
}

/**
 * 交付路径的选择说明。
 *
 * 只说「这条路的代价」，不复述探测结论：探测结果已经由上方「建议路径」的取值表达，
 * 在这里再说一遍「连不上日志服务器」就是对同一件事的第二次播报。
 */
const pathModeHint = computed(() => {
  if (pathMode.value === 'P1') {
    return 'acli 插件从 HCI 直接上传到日志服务器：省一跳中转，但平台不留副本，事后无法回看/下载'
  }
  if (pathMode.value === 'P2') {
    return 'HCI 只生成压缩包，由客户机经平台中转到日志服务器：平台留副本，可回看/下载，但需要客户机在线'
  }
  return '探测完成后自动选择：HCI 能直连日志服务器时用 P1，否则用 P2'
})

/**
 * 「建议路径」这一格的取值。
 *
 * 契约（Q2026102801062）：只有两个字符串——`P1 直传日志服务器` / `P2 经平台中转`，
 * 不加「（建议）」「待探测」之类修饰。探测结论已经由「取值是哪一个」说清楚，
 * 每多一个字，用户就要多判断一次。
 *
 * 有任务时只认 `upload_mode`：它是创建那一刻落库的决策，能回答「这单实际走哪条路」；
 * 没有任务时才是探测快照。两种来源共用同一套取值，不让界面出现第二套措辞。
 */
const deliveryModeFact = computed(() => {
  const mode = currentTask.value?.upload_mode
  if (mode === 'acli_direct') return { label: 'P1 直传日志服务器', type: 'success' as const }
  if (mode === 'bridge_relay') return { label: 'P2 经平台中转', type: 'warning' as const }
  const probe = probeResult.value
  if (!probe) return null
  return probe.log_server_reachable
    ? { label: 'P1 直传日志服务器', type: 'success' as const }
    : { label: 'P2 经平台中转', type: 'warning' as const }
})

/**
 * 探测降级项的独立清单。
 *
 * 只有「读不到节点清单」进这里：它改变的是用户下一步要不要手填 IP。
 * 日志服务器可达性**不进告警**：连不上只是选 P2 的理由，由上方「建议路径」的取值
 * 与下方交付路径的默认值/可选性同一件事表达；再叠一条需要判读的文案，用户只会更迷茫。
 */
const probeIssues = computed(() => {
  const probe = probeResult.value
  if (!probe || probe.hosts?.length) return [] as { key: string; title: string; detail: string }[]
  return [
    {
      key: 'hosts',
      title: '节点清单获取失败（仍可手填 all 或节点 IP）',
      detail: probe.catalog_note || 'acli 未返回任何节点，且没有给出原因',
    },
  ]
})

/**
 * 压缩包大小。
 *
 * P1 的字节数只有两种来源：插件 progress 输出（不含大小）或节点上的文件本身（上传成功后
 * 插件立刻自删）。bridge 会在上传窗口内 stat 一次回填，但轮询间隔内完成的小包常常错过，
 * 因此 P1 空值属预期，文案要说清原因，别让用户以为平台漏采。
 */
const resultSizeText = computed(() => {
  const task = resultTask.value
  const size = task?.archive_size
  // 平台已给出统一口径的体积文案时优先用它（1024 进制、与后端逐字一致）。
  if (task?.artifact?.size_text && task.artifact.size_text !== '—') return task.artifact.size_text
  if (!size) {
    // 零产物的失败结论比「未记录」诚实：1130 事故里后者让用户以为只是没记上。
    if (task?.display?.conclusion === 'failed') return '未取到有效日志'
    // 失败任务从未走到上传：P1 的「直传后已删除」叙事对它是谎言，必须按终态说。
    if (task?.status === 'failed') return '本次采集失败，未产出压缩包'
    return resultMode.value === 'acli_direct' ? '未记录（直传后压缩包已删除）' : '未记录'
  }
  const mb = size / 1024 / 1024
  return mb >= 1024 ? `${(mb / 1024).toFixed(2)} GB` : `${mb.toFixed(1)} MB`
})

/**
 * 结果态是否该出现「上传到平台」。
 *
 * 入口可见性不能只依赖节点实时对账：对账要先建立 SSH/bridge 会话再 stat，面板刚
 * 打开时那一步通常还没回来（现场截图里 SSH 还在"正在连接"），于是出现"包明明还在
 * 节点上、平台也记了路径，却连按钮都不给"，用户只能干等或去重新收集。
 *
 * 因此分两级：
 *   1. 对账明确 `adopt + deliver` → 立即给入口；
 *   2. 对账尚未回来 → 只要平台记录能证明「P2 有包、尚未交付」，先给入口，
 *      点击时再由 stat 校验；包真的没了会得到明确报错，比不给按钮诚实。
 */
const canUploadFromNode = computed(() => {
  if (nodeAction.value === 'adopt' && nodeAdoptMode.value === 'deliver') return true
  // 对账已给出别的结论（直传/被阻塞）时以对账为准
  if (nodeAction.value) return false
  const task = resultTask.value
  if (!task || resultDelivered.value) return false
  if (resultMode.value === 'acli_direct') return false
  return Boolean(task.archive_path) && (task.archive_size ?? 0) > 0
})

/**
 * 结果态是否仍在等节点对账。
 *
 * 用于把"按钮为什么还没出现"变成一句可见的进度，而不是让用户对着没有按钮的
 * 卡片猜平台是不是又坏了。
 */
const nodeVerifyPending = computed(
  () => viewMode.value === 'result' && !nodeAction.value && !resultDelivered.value,
)

/** 认领被拒的原因文案（不吞掉原因，也不把内部原因码直接丢给用户）。 */
const claimBlockedText = computed(() => {
  const reason = chatStore.logCollectCaseClaimBlockedReason
  return reason ? (CLAIM_BLOCKED_REASON_TEXT[reason] ?? '该工单当前不能认领') : ''
})

/**
 * 认领壳工单：把归属与名下采集任务转到当前身份，然后原地刷新。
 *
 * 为什么要用户点确认：把工单从"别人的会话"变成"我的"，是可审计的所有权变更；
 * 自动做等于给任意单号开了后门。认领成功后不重新采集——节点上那份包还在。
 */
async function claimOwnership() {
  try {
    await chatStore.claimLogCollectCase()
    ElMessage.success(
      chatStore.logCollectClaimedTasks > 0
        ? `已认领工单，并接管 ${chatStore.logCollectClaimedTasks} 条采集记录`
        : '已认领工单，可以继续采集与上传',
    )
    await restoreTaskState(true)
    if (sshConnected.value) void syncNodeState({ applyPatches: true, silent: true, source: 'manual' })
  } catch (error: any) {
    ElMessage.warning(error?.message || '认领失败，请稍后重试')
  }
}

/**
 * 平台是否已有交付证据。
 *
 * 判据只能是交付证据，不能是采集状态：`status=completed` 只说明 acli 报采集结束，
 * 平台可能一个字节都没收到（2026-10-27 现场）。证据有三类：
 *   - P2：`relay_status=success` 或 `delivery_status=delivered`；
 *   - P1：`delivery_status=delivered`（由节点报告的「发送成功」经 patch_delivered 回写）；
 *   - 老记录（upload_mode 是后补字段）：没有交付状态可比，按 P1 的口径兜底。
 */
const resultDelivered = computed(() => hasDeliveryEvidence(resultTask.value))

/**
 * 结果态是否提供「重新核验归档」。
 *
 * 为什么必须有这个入口：P1 平台无独立回执，唯一的交付证据是"去日志服务器查一次"，
 * 而那次查询很可能发生在上传还没结束的时候。没有重跑入口时，一条误判的记录只能靠
 * 重新采集来"洗掉"，可包其实早就到了（2026-11-10 Q2026111000243）。
 */
const canReverifyArchive = computed(
  () =>
    resultMode.value === 'acli_direct' &&
    resultTask.value?.status === 'completed' &&
    !resultDelivered.value,
)
const reverifyingArchive = ref(false)

/**
 * 重跑落盘侧核验。
 *
 * 交付终态一律由服务端写（single-writer），这里只做两件事：触发一次权威回查、
 * 把最新任务快照拉回来重绘。前端绝不在本地拼"已交付"。
 */
async function reverifyArchive() {
  const task = resultTask.value
  if (!task || reverifyingArchive.value) return
  reverifyingArchive.value = true
  try {
    const verdict = await logCollectApi.verifyP1Archive(task.task_id, task.archive_size ?? null)
    const latest = await logCollectApi.getTask(task.task_id)
    const index = historyTasks.value.findIndex((item) => item.task_id === latest.task_id)
    if (index >= 0) historyTasks.value.splice(index, 1, latest)
    showResult(latest)
    if (verdict?.verified === true) {
      ElMessage.success('日志服务器上已查到该工单归档件，平台已登记为已交付')
    } else {
      ElMessage.warning(describeArchiveCheck(p1ArchiveCheckOf(latest)) || '本次仍未核验到归档件')
    }
  } catch (error: any) {
    ElMessage.warning(`核验未完成：${error?.message || '平台或日志服务器暂时不可用，请稍后再试'}`)
  } finally {
    reverifyingArchive.value = false
  }
}

const resultArchiveText = computed(() => {
  const task = resultTask.value
  if (!task) return ''
  // 不写「详见平台任务记录」：结果态本身就展示 error_code/error_message，
  // 再把用户指去别处查同一份数据，只会让"记录是空"的现场更困惑（Q2026102801062）。
  if (task.relay_status === 'failed') {
    return `归档失败：${formatFailureReason(task, '平台未记录原因（可在桥日志中按工单号检索）')}`
  }
  if (task.delivery_status === 'failed') {
    // P1 的交付失败**只可能**来自落盘侧核验（acli_direct 没有别的写入者）。
    // 它回答的是"回查时日志服务器上有没有这个包"，不是"上传动作失败了"：
    // 采集刚结束就去回查时上传往往才刚开始，一次"查不到"会被永久记成失败
    // （2026-11-10 Q2026111000243：包已落 /sf/log，界面红着写「交付失败」）。
    // 因此这里如实转述核验快照，并且不再承诺一个可能根本不存在的按钮。
    if (resultMode.value === 'acli_direct') {
      const observed = describeArchiveCheck(p1ArchiveCheckOf(task))
      if (observed) return observed
      return '未核验到归档件（平台无独立回执，可点「重新核验归档」再查一次）'
    }
    // 磁盘满与并发容量不足都能给出可操作原因：原因由 bridge 生成
    // （含 need/free/path），前端只转述，不自己编数字。
    // 历史文案只有一句"交付失败"，用户照着去清盘却不知道清哪里（2026-11-03 Q2026110300988）。
    if (task.error_code === 'LC-TRANSFER-008') {
      return `交付失败：${formatFailureReason(task, '客户机磁盘空间不足，请清理临时目录后重试，无需重新采集')}`
    }
    if (task.error_code === 'LC-TRANSFER-009') {
      // 与 008 的处置动作不同：让用户等其它交付任务结束，而不是去清一个清不出来的盘。
      return `交付失败：${formatFailureReason(task, '客户机还有其它交付任务在占用本地空间，请等待其完成后再重试，无需重新采集')}`
    }
    if (task.error_code === 'LC-TRANSFER-010') {
      // 源包仍在写入：处置动作是"等包稳定后重开一次会话"。
      // 必须阻止连续重点——每次重点都会按当时的中间大小再建一个注定作废的会话。
      return `交付失败：${formatFailureReason(task, '节点压缩包仍在写入（采集已到 100% 但 zip 未收尾），请等文件大小不再变化后重试；要交付最新内容请先重新采集')}`
    }
    return '交付失败（可点「上传到平台」重传，无需重新采集）'
  }
  // queued = 平台已登记一次交付尝试但没走到终态（提交阶段中断/失败）。
  // 不显式化的话，这里只会显示"采集完成，尚未交付"，用户无从知道已经试过一次、
  // 试到哪一步了（Q2026102701193 现场库里就有一条 queued 卡了两天）。
  // 这一条排在「失败兜底」之前：采集失败但交付曾登记为 queued 时，仍以 queued 为准。
  if (task.delivery_status === 'queued') {
    // 有分片进度就必须带上：19/233 与 231/233 的处置完全相反。
    // 前者说明链路撑不住当前节奏（别傻等），后者只差最后几分钟（别重开会话）。
    const progress = describeUploadProgress(
      (task.detail?.['upload_progress'] ?? null) as UploadProgressView | null,
    )
    if (progress) {
      return `交付已登记但未完成：${progress}（链路恢复后自动续传，无需重新采集）`
    }
    return '交付已登记但未完成（可点「上传到平台」重试）'
  }
  // 失败任务没有"归档/交付"可言：兜底显示未归档，避免落到「未记录」让人以为还在路上。
  if (task.status === 'failed') return '采集失败，未归档'
  if (resultDelivered.value) {
    // P1 平台拿不到回执：只有回查过归档件才能说"已核验"，否则必须说清是节点自报。
    if (resultMode.value === 'acli_direct') {
      return p1ArchiveCheckOf(task)?.verified === true
        ? '已核验：日志服务器上存在该工单归档件'
        : '已由插件直传日志服务器（节点自报，平台无独立回执）'
    }
    return '已归档到日志服务器'
  }
  if (task.status === 'completed') return '采集完成，尚未交付'
  return '未记录'
})

/**
 * 完整性结论。
 *
 * 对抗性审查：这里最容易犯的错是把"没校验"渲染成一个看起来像成功的默认值。
 * 只有 integrity_state === 'verified' 才允许说"已校验"；其余一律显性说明
 * 没有源包基线，避免用户把"大小对上了"误当成"字节一致"。
 */
const resultIntegrityText = computed(() => {
  const task = resultTask.value
  if (!task) return ''
  if (task.upload_mode === 'acli_direct') return '直传模式无摘要比对（平台不留副本）'
  if (task.integrity_state === 'verified') return '已校验：源包摘要与平台副本一致'
  return '未校验源包（仅传输层校验；节点未提供摘要或为历史任务）'
})

const resultArtifactText = computed(() => {
  const task = resultTask.value
  if (!task) return ''
  // 失败即无产物：不能套用 P1 成功后的「自动清理」话术。
  if (task.status === 'failed') return '采集未成功，无产物'
  if (task.archive_deleted) return 'HCI 上的压缩包已清理'
  if (task.archive_path) return task.archive_path
  return resultMode.value === 'acli_direct' ? '由插件上传后自动清理，路径未记录' : '未记录'
})

/**
 * 结果态的失败原因。
 *
 * 顶部提示条一直写着"失败原因见下方错误信息"，但结果态从未渲染过 error_message：
 * Q2026102701403 现场（采集 455 MiB 后分片上传报 x509 证书不受信）只能靠查库才知道，
 * 界面上一个字都没有。错误码与原因分开拼，方便用户直接把这一行报给支持。
 *
 * 空值时不能编造原因：平台确实可能没记录（比如取消、或历史任务），只能如实说明。
 */
const resultFailureText = computed(() => {
  const task = resultTask.value
  if (!task) return ''
  const code = task.error_code ? `[${task.error_code}] ` : ''
  return `${code}${task.error_message || '平台未记录失败原因（可在桥日志中按工单号检索）'}`
})

/**
 * spc 场景的槽位还原卡片。
 *
 * 这条信息与采集结论正交：任务可能早已 completed，节点上的 core.json 却仍是平台清单。
 * 只在真的动过节点清单时出现（restore_status=none 且无逐台证据 → 不渲染），
 * 避免给普通 -c 场景平白多出一行"正在还原"。
 */
const spcRestoreCard = computed(() => {
  const task = resultTask.value
  if (!task) return null
  const status = task.restore_status ?? 'none'
  const hosts = task.spc_hosts ?? []
  if (status === 'none' && hosts.length === 0) return null
  const written = hosts.filter((host) => !host.error)
  const rejected = hosts.filter((host) => host.error)
  const label =
    status === 'success'
      ? '已还原为节点原清单'
      : status === 'failed'
        ? '还原未完成'
        : '待还原'
  const tone = status === 'success' ? 'success' : status === 'failed' ? 'error' : 'warning'
  // 备份名是唯一的恢复依据（<file>.platform.<task_id>.bak），必须让用户看到可追溯，
  // 但它不是删除目标：界面只解释"改过哪几台"，不作为清理指引。
  const tip = [
    written.length ? `${written.length} 台节点使用过平台清单` : '',
    rejected.length ? `${rejected.length} 台写入失败` : '',
    status === 'success'
      ? '节点已回到采集前状态'
      : '备份仍在节点上，可重试还原',
  ]
    .filter(Boolean)
    .join('；')
  return { label, tone, tip, canRetry: status !== 'success' }
})

const spcRetrying = ref(false)

/**
 * 重试还原：bridge 后台协程与主流程收尾都失效（进程重启 + 页面已关）时的人工兜底。
 * 每次新建连接——此刻不在采集中，复用长连接只会拿到可能已被关闭的 socket。
 */
async function retrySpcRestore(): Promise<void> {
  const task = resultTask.value ?? currentTask.value
  if (!task || spcRetrying.value) return
  spcRetrying.value = true
  let ws: WebSocket | null = null
  try {
    ws = await openSocket()
    spcPendingRestore.value = true
    await restoreSpcSlot(ws, task.task_id)
    // 回读平台记录：还原结论要由服务端确认，不能用本地 ref 自证。
    const fresh = await logCollectApi.getTask(task.task_id).catch(() => null)
    if (fresh) {
      resultTask.value = fresh
      spcHosts.value = fresh.spc_hosts ?? spcHosts.value
    }
  } catch (error: any) {
    ElMessage.warning(error?.message || '还原请求未能发出，请确认桥连接正常')
  } finally {
    spcRetrying.value = false
    try {
      ws?.close()
    } catch {
      /* 关闭失败不影响结论 */
    }
  }
}

const resultCleanupTip = computed(() => {
  const task = resultTask.value
  if (!task || task.archive_deleted) return ''
  // 失败任务不该讲 P1 成功后的清理路径：那是另一种结局，混淆只会让用户以为"其实传成功了"。
  if (task.status === 'failed') return ''
  // 顶部提示条已经在说"包仍在节点、可点上传到平台"：再说一遍只会在固定区占两行，
  // 而且两条的措辞还会互相冲突（一条说仍在、一条说已删）。
  if (canUploadFromNode.value) return ''
  if (resultMode.value === 'acli_direct') {
    if (!resultDelivered.value) {
      // 未交付时只说**清理**这一件事，不再顺带复述交付结论：横幅与归档状态已经在说
      // "未核验到归档件"，第三条"平台尚未收到交付结论"只会叠加同一件事、
      // 却让人误读出"平台完全没收到任何东西"（Q2026111000243）。
      // 有产物位置就是有 stat 证据；没有证据时不能断言"包仍在节点"。
      return task.archive_path
        ? '压缩包仍在节点上（见「产物位置」）；P1 发送成功后插件会自动删除，无需人工清理。'
        : '无需人工清理：P1 发送成功后插件会自动删除节点上的压缩包。'
    }
    return 'P1 由插件直传日志服务器，上传成功后插件会自动删除 HCI 上的压缩包，无需人工清理。'
  }
  if (resultMode.value === 'bridge_relay' && task.archive_path) {
    // 绝不能把 task.archive_path 当成节点路径展示：P2 下它是平台对象 key。
    // 平台只提供准确路径，不提供任何删除命令，由用户自行确认处理。
    // 只认节点侧证据：先本次对账拿到的实时路径，再服务端从桥日志核对过的路径；
    // 两者都不可信时 buildNodeCleanupTip 会明确「不提供路径」。
    return buildNodeCleanupTip(task.case_id || props.caseId, [
      nodeArchivePath.value,
      task.node_archive_path,
    ])
  }
  return ''
})

/**
 * 固定提示区是否要占一行。
 *
 * 只有"不处置就没法继续"或"必须被用户读到的结论"才进固定区；
 * 引导类 info 提示（请先连 SSH、正在读取环境）仍留在内容流里，跟着上下文走。
 * 判断放在 computed 而不是模板里拼 v-if，是为了让固定区整块出现/消失，
 * 不会出现"挂着一个空 gap 的白条"。
 */
const hasPinnedAlerts = computed(
  () =>
    chatStore.logCollectCaseOwned === false ||
    prepareState.value === 'failed' ||
    Boolean(acliFailure.value) ||
    Boolean(nodeSyncHint.value) ||
    Boolean(cleanupTip.value) ||
    Boolean(resultCleanupTip.value),
)

/** 节点下拉文案：IP 是必选值，名称与主控标记只做辅助识别。 */
function hostLabel(host: HostOption): string {
  const base = host.name ? `${host.ip}（${host.name}）` : host.ip
  return host.master ? `${base} · 主控` : base
}

/**
 * 合并「集群能力」与「平台字典」。
 *
 * 三条规则，顺序不能换：
 *  1. 能力边界只认 probe：字典有、集群没返回的枚举**不展示**——展示就等于承诺了一个
 *     create 必然失败的选项；
 *  2. 文案以字典为准，字典没维护的保留 bridge 短名并标记 documented=false（界面会提示“平台未维护”）；
 *  3. 顺序以字典 sort_order 为准，未维护项按 bridge 原顺序追加在尾部，顺序变化才能解释。
 */
function mergeModuleOptions(
  capability: ModuleOption[],
  dict: LogCollectScenarioItem[],
): ModuleOption[] {
  if (!dict.length) return capability.map((item) => ({ ...item, documented: false }))

  const byValue = new Map(dict.map((item) => [item.value, item]))
  const documented: ModuleOption[] = []
  const undocumented: ModuleOption[] = []
  for (const item of capability) {
    const hit = byValue.get(item.value)
    if (!hit) {
      undocumented.push({ ...item, documented: false })
      continue
    }
    documented.push({
      value: item.value,
      label: hit.label?.trim() || item.label,
      summary: hit.summary?.trim() || undefined,
      collects: hit.collects?.trim() || undefined,
      typicalSymptoms: hit.typical_symptoms?.trim() || undefined,
      durationHint: hit.duration_hint?.trim() || undefined,
      exclusive: hit.exclusive,
      sortOrder: hit.sort_order,
      documented: true,
    })
  }
  documented.sort((a, b) => (a.sortOrder ?? 100) - (b.sortOrder ?? 100))
  return [...documented, ...undocumented]
}

/**
 * 拉取场景字典（失败不抛）。
 *
 * 字典是**展示增强**，不是采集前置条件：拿不到时只影响“这一项收什么”能不能显示，
 * 不影响探测、下发与进度。把失败向外抛会让一个后台配置问题变成“日志收集不可用”。
 */
async function loadScenarioDict(): Promise<void> {
  // 两个字典互相独立：任一失败都只降级自己，不能连带把另一个清空。
  const [dictResult, customResult, manifestResult] = await Promise.allSettled([
    logCollectApi.listScenarios(),
    logCollectApi.listCustomScenarios(),
    logCollectApi.listDefaultManifests(),
  ])
  if (dictResult.status === 'fulfilled') {
    scenarioDict.value = dictResult.value
    scenarioDictNote.value = scenarioDict.value.length
      ? ''
      : '平台场景字典暂未维护，当前只显示场景名称'
  } else {
    scenarioDict.value = []
    scenarioDictNote.value = '场景说明暂不可用（平台字典读取失败）'
    console.warn('[log-collect] 场景字典读取失败', dictResult.reason)
  }
  if (customResult.status === 'fulfilled') {
    // 旧后端/桩返回 undefined 时也要有确定的空列表：模板里直接读 .length。
    customScenarios.value = Array.isArray(customResult.value) ? customResult.value : []
  } else {
    customScenarios.value = []
    console.warn('[log-collect] 自定义场景读取失败', customResult.reason)
  }
  if (manifestResult.status === 'fulfilled') {
    defaultManifests.value = Array.isArray(manifestResult.value) ? manifestResult.value : []
  } else {
    defaultManifests.value = []
    console.warn('[log-collect] 默认场景清单预览读取失败', manifestResult.reason)
  }
  scenarioDictLoaded.value = true
}

/** 用探测结果覆盖目录与节点清单；场景目录以集群为准，内置目录只做兜底。 */
function applyCatalog(data: ProbeResult) {
  probeModules.value = data.modules?.length ? data.modules : [...BUILTIN_MODULES]
  hostOptions.value = data.hosts ?? []
  // 目录收敛后清掉已不存在的场景，否则提交时会被 create 判为非法枚举
  const valid = new Set(moduleOptions.value.map((item) => item.value))
  form.modules = form.modules.filter((value) => valid.has(value))
  // 收敛后为空会让用户误以为"没有可选项"，回落到目录首项。
  // 跳过互斥项：默认勾一个互斥项会让其余选项全被禁用（当前字典无互斥项，纯防御）。
  if (!form.modules.length && moduleOptions.value.length > 0) {
    const preferred = moduleOptions.value.find((item) => !item.exclusive) ?? moduleOptions.value[0]
    form.modules = [preferred.value]
  }
}

// ── 场景互斥（字典 exclusive）────────────────────────────────────────────

/**
 * 互斥项（按字典 `exclusive` 数据驱动，**当前一项都没有**）。
 *
 * 为什么保留这段：`exclusive` 是字典能力而非硬编码策略——将来真出现“选了它就不能
 * 选别人”的场景，管理台打开开关即生效。注意 `other` **不属此类**：支持工程师 2026-10-29
 * 实机确认 `-m other,vm_suspend` 是叠加语义（两类日志一起收），故 `other.exclusive = false`。
 *
 * 为什么不靠“后选清前者”的 diff 实现：直接**禁用**冲突项比“选完再默默清掉一个”更可解释，
 * 也不会在回填历史任务时把用户已选的东西改掉。
 */
const exclusiveModules = computed(
  () => new Set(moduleOptions.value.filter((item) => item.exclusive).map((item) => item.value)),
)
const exclusiveSelected = computed(() => form.modules.some((value) => exclusiveModules.value.has(value)))
/** 历史任务可能存过互斥项与具体专项同时存在的组合：只提示，不改写历史事实。 */
const hasModuleConflict = computed(
  () => exclusiveSelected.value && form.modules.some((value) => !exclusiveModules.value.has(value)),
)

function moduleDisabled(value: LogCollectModule): boolean {
  if (running.value) return true
  if (exclusiveSelected.value) return !exclusiveModules.value.has(value)
  if (form.modules.length > 0) return exclusiveModules.value.has(value)
  return false
}

/** 已选场景的详细说明：直接回答“这次到底会收什么、典型症状对不对得上”。 */
const selectedScenarioDocs = computed(() =>
  moduleOptions.value.filter(
    (item) =>
      form.modules.includes(item.value) &&
      Boolean(item.collects || item.typicalSymptoms || item.durationHint),
  ),
)
const emptyModulesHint = computed(() =>
  form.modules.length ? '' : '未选择场景时按「默认收集专项」下发',
)

/**
 * 单次探测：读取架构、acli、可用空间、日志服务器连通性，以及场景目录与节点清单。
 *
 * 返回 null 表示这次探测已被新的一次取代（换工单/换节点/手动重探），
 * 此时**不得**写任何状态，否则旧结论会覆盖新结论。
 */
async function probeOnce(targetNode: string, token: number): Promise<ProbeResult | null> {
  // 字典与探测并行：字典是 HTTP、探测走 bridge WS，串行会让面板多等一个往返。
  // 字典失败已在 loadScenarioDict 内部吞掉，不会影响这里的探测结果。
  const dictPromise = loadScenarioDict()
  const ws = await openSocket()
  const probe = await sendAndWait(
    ws,
    'probe',
    { remoteUrl: RELAY_URL, nodeIp: targetNode, ticketNo: props.caseId },
    90000,
  )
  if (probe.status !== 'ok') {
    throw new Error(probe.message || '环境探测失败，请确认已在 SSH 终端连接 HCI')
  }
  // 必须先归一化再落状态：下游（告警、落库快照）只认 http_code 一个字段。
  const data = normalizeProbe(probe.log_data as ProbeResult)
  if (!data) throw new Error('探测回包缺少环境数据，请重新探测')
  // 字典先落地再合并目录：否则用户会先看到一批无说明的短名，再整排换一次文案（闪变）。
  await dictPromise
  if (token !== prepareToken) return null
  probeResult.value = data
  applyCatalog(data)
  return data
}

/**
 * 准备阶段：探测集群环境，瞬态失败自动重试，成功后再安排一次低频事实复查。
 *
 * 对抗性审查：探测失败不等于不能收集——主机清单为空时回退手填 IP，
 * 因此这里只记录失败原因，不阻断后续表单编辑。
 *
 * 参数：
 * - `silent`：不弹 ElMessage（打开面板/复查时用），失败仍会在表单上方留告警；
 * - `retries`：false 表示只探一次（复查链路用，避免「重试×复查」指数放大）；
 * - `resetRecheck`：false 表示不归零复查计数（复查自己触发的探测必须保留计数）。
 */
async function prepare(
  options: { silent?: boolean; retries?: boolean; resetRecheck?: boolean } = {},
): Promise<boolean> {
  const targetNode = nodeIp.value
  if (!targetNode) {
    prepareState.value = 'failed'
    prepareError.value = NODE_MISSING_MESSAGE
    return false
  }
  // 注意：不能用 running 挡在这里。handleStart 在 stage='probing' 之后会调用本函数
  // （未准备/准备失败时现场补探），而 probing 本身就在 running 集合里，
  // 一挡就会把「开始收集」的补探静默吞掉。采集期间不允许探测只由两个入口负责：
  // 按钮 disabled（手动）+ scheduleFactRecheck（自动复查）。
  const stalled =
    prepareState.value === 'probing' && Date.now() - probingStartedAt.value > PROBE_STALL_MS
  if (prepareState.value === 'probing' && !stalled) {
    // 复用进行中的那次：并发探测会让两个请求抢同一个 waiter（后到者覆盖前者，
    // 前者只能空等到 90s 超时）。但静默返回会让用户以为按钮失灵——
    // 这正是 2026-10-30「点了没反应、只能 F5」的直接成因，必须给出可见反馈。
    if (!options.silent) ElMessage.info('正在探测集群环境，请稍候（也可等本次结果返回）')
    return false
  }

  const token = ++prepareToken
  prepareState.value = 'probing'
  probingStartedAt.value = Date.now()
  prepareError.value = ''
  probeRetry.value = null
  if (options.resetRecheck !== false) probeRecheckCount.value = 0

  const maxRetries = options.retries === false ? 0 : PROBE_RETRY_DELAYS.length
  for (let attempt = 0; attempt <= maxRetries; attempt += 1) {
    if (attempt > 0) {
      const delayMs = probeRetryDelay(attempt - 1)
      probeRetry.value = { attempt, total: maxRetries, delayMs }
      await sleep(delayMs)
      // 等待期间面板关闭 / 换工单 / 被新探测取代：整轮作废，不留下「正在重试」的假象
      if (token !== prepareToken || !visible.value) {
        if (token === prepareToken) {
          probeRetry.value = null
          prepareState.value = 'idle'
        }
        return false
      }
    }
    try {
      const data = await probeOnce(targetNode, token)
      if (!data || token !== prepareToken) return false
      probeRetry.value = null
      // 交付路径由探测结论直接决定：可达 P1、不可达 P2（P1 同时置灰）。
      applyDefaultPathMode()
      probedAt.value = Date.now()
      prepareState.value = 'ready'
      // 历史探测读的是 bridge 事件，失败不影响本次结果，因此不 await。
      void loadProbeHistory()
      // 探测成功 ≠ 环境就绪：acli 可能刚被别的流程装上，节点可能刚上线
      scheduleFactRecheck()
      return true
    } catch (error: any) {
      if (token !== prepareToken) return false
      prepareError.value = probeErrorText(error)
      if (isRetryableProbeError(error) && attempt < maxRetries) continue
      probeRetry.value = null
      prepareState.value = 'failed'
      if (!options.silent) showCustomerFault(prepareError.value)
      return false
    }
  }
  return false
}

const running = computed(() =>
  ['probing', 'installing', 'creating', 'collecting', 'pulling', 'uploading', 'relaying'].includes(stage.value),
)

const progressStatus = computed(() =>
  stage.value === 'failed' ? 'exception' : stage.value === 'done' ? 'success' : undefined,
)

const stepActive = computed(() => {
  if (stage.value === 'done') return 4
  // 失败时回到失败发生的那一步：早先这里落到 return 0，所有失败都显示「卡在探测」。
  if (stage.value === 'failed') return failedStep.value
  if (stage.value === 'relaying') return 3
  if (stage.value === 'uploading' || stage.value === 'pulling') return 2
  if (stage.value === 'collecting' || stage.value === 'creating') return 1
  return 0
})

/**
 * P1 直传：包由节点上的插件直接发给日志服务器，平台拿不到分片进度。
 *
 * 它决定了「上传」这一步该用什么文案与进度来源：直传看节点的 upload_state，
 * P2 中转才看平台的 transfer 分片。
 */
const isDirectDelivery = computed(
  () => currentTask.value?.upload_mode === 'acli_direct' || Boolean(currentTask.value?.remote_url),
)

/**
 * 进度条 = 整条链路的进度，不是节点采集的进度。
 *
 * 旧实现读节点 `percent` 再与交付百分比取 `Math.max`，而节点在采集结束时就报 100，
 * 于是从「开始拉包」到「归档完成」全程都显示 100%（2026-10-30 现场 Q2026102901049：
 * 进度条 100% 与「正在从 HCI 拉取：0 字节」同屏）。分段规则见 services/logCollectProgress.ts。
 */
const percent = computed(() =>
  computeOverallPercent({
    stage: stage.value,
    nodePercent: progress.value?.percent ?? 0,
    deliveryPercent: transfer.percent,
    // 拿到分片进度就一定是平台中转：P1 直传由节点插件发送，平台侧根本没有分片。
    // 这条判据同时覆盖「P1 失败自动降级 P2」——那时 upload_mode 已被改写、remote_url 仍在。
    direct: isDirectDelivery.value && transfer.partCount === 0,
  }),
)

const statusText = computed(() => {
  // 失败原文由下方诊断卡完整展示一次；状态行只给结论，避免同页重复同一长句。
  if (stage.value === 'failed') return UI_TEXT.labels.task_failed
  // 「完成」这一步有两种结局，必须分开说：真的归档了，还是只采完没交付。
  // 一律写"已归档"等于替平台撒谎（Q2026110900356：平台零产物、5888 found=false）。
  if (stage.value === 'done') {
    return hasDeliveryEvidence(currentTask.value) ? '日志已归档到日志服务器' : '采集完成，日志尚未交付'
  }
  if (stage.value === 'collecting') {
    const p = progress.value
    return p ? `${p.status}（${p.done}/${p.total}）` : '采集进行中'
  }
  if (stage.value === 'pulling') {
    // 作业刚提交时 bridge 还在等节点把包写完（waiting_collect），此刻一个字节都还没拉。
    // 直接说「正在拉取 0 字节」会把「还没开始」说成「传输卡住了」。
    if (transfer.stage === 'waiting_collect') return '节点采集已收尾，等待压缩包写入完成（尚未开始传输）'
    const total = transfer.totalSize > 0 ? ` / ${formatByteCount(transfer.totalSize)}` : ''
    return `正在从 HCI 拉取：${formatByteCount(transfer.pulled)}${total}`
  }
  if (stage.value === 'uploading') {
    if (isDirectDelivery.value) {
      const p = progress.value
      return `正在发送到日志服务器：${p?.upload_state || '发送中'}（${p?.percent ?? 0}%）`
    }
    return `正在上传平台：${transfer.uploadedParts}/${transfer.partCount} 片`
  }
  if (stage.value === 'relaying') {
    // 必须带上片段/轮次/上次失败：只写 relayStatus 时，单片 300s 超时期间界面
    // 与"已卡死"完全同形，用户只能得出"挂了"这一个结论。
    return `正在归档到日志服务器：${describeRelayProgress(relayProgress.value)}`
  }
  return ''
})

// ── WebSocket ───────────────────────────────────────────────────────────────

let socket: WebSocket | null = null
const waiters = new Map<string, (data: any) => void>()
// 等待器的失败通道：会话断开 / WS 通道关闭时必须能**立刻**结束等待。
//
// 只有 resolve 通道时，断开后 sendAndWait 只能等满 60s 超时，
// 报成"progress 超时（60s）"——真正的原因（SSH 会话已被 TMOUT=600 踢掉）
// 被这个文案彻底掩盖，用户据此只会去"重新采集"，而插件单任务互斥会直接拒绝
// （2026-10-28 现场：正确处置是重连后接管）。
const waiterRejectors = new Map<string, (error: Error) => void>()

// 会话已断开的准确文案：必须点名真因，不能退化成"超时"。
const SSH_LOST_MESSAGE =
  'SSH 会话已断开（远端 shell 空闲 10 分钟自动退出，HCI TMOUT=600）。请先在 SSH 终端重新连接节点，再用「查看任务状态 / 接管」继续；不要重新下发采集（插件单任务互斥会拒绝）。'
const BRIDGE_CHANNEL_LOST = BRIDGE_CHANNEL_LOST_MESSAGE

// 本面板是否已知会话断开。它不能只看 chatStore：会话可能是面板这条连接
// autoConnect 建的，ssh_disconnected 只会推给建会话的那条 WS。
const sshSessionLost = ref(false)
watch(sshConnected, (ok) => {
  // 重连成功后立即解除告警态，后续对账才能重新驱动节点。
  if (ok) sshSessionLost.value = false
})

function rejectWaiter(action: string, error: Error) {
  const reject = waiterRejectors.get(action)
  if (!reject) return
  waiterRejectors.delete(action)
  waiters.delete(action)
  reject(error)
}

function rejectAllWaiters(error: Error) {
  for (const action of Array.from(waiterRejectors.keys())) rejectWaiter(action, error)
}

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
        ws.onclose = () => {
          // 通道已关：此时发出去的请求永远等不到回包（ws.send 静默丢弃），
          // 必须立刻失败，而不是让调用方空等 60s。
          rejectAllWaiters(new Error(BRIDGE_CHANNEL_LOST))
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
  // 桥的结构化日志也会推给本连接：面板这条 WS 没有绑定工单（sub.caseID 为空），
  // bridge 按「全量订阅者」把日志推过来。历史实现在这里直接 return，
  // 于是面板通道上的桥日志从未进入回采链路——2026-10-27 现场
  // 云端查不到 log_collect.session_missing 的直接原因之一就是这个分支缺失。
  if (data.type === 'bridge_log') {
    chatStore.forwardBridgeLog(data as Record<string, unknown>)
    return
  }
  if (data.type === 'ssh_disconnected') {
    // 远端 shell 退出（HCI TMOUT=600）**不等于** SSH 连接失效：bridge 侧会保留会话
    // （session.shell_eof_detached），采集/进度走独立 exec 通道，照旧可用。
    // 因此这里只标记 + 提示，绝不打断正在进行的轮询：一断就判失败，
    // 等于把本来能跑完的采集判死（2026-10-28 现场正是这样丢掉一次已完成采集的）。
    sshSessionLost.value = true
    if (pollingBridgeProgress) {
      nodeSyncHint.value =
        '终端会话已被节点空闲超时断开（TMOUT=600），采集仍在节点上运行；进度查询继续走现有连接，无需重新采集。'
    }
    return
  }
  if (data.type === 'delivery_result') {
    // 作业化后 submit/status/resume/cancel 共用一种回包，按 log_action 映射回各自的等待器
    const key = `delivery_${data.log_action}`
    const waiter = waiters.get(key)
    if (waiter) {
      waiters.delete(key)
      waiter(data)
    }
    return
  }
  if (data.type === 'acli_sync_progress') {
    // 装工具这一跳的中间态：下载/上传/校验/安装各自有独立文案与百分比。
    // 只更新展示，不结束等待——真正的结论只出现在 acli_sync_result。
    const facts = parseAcliSyncFacts(data)
    acliLive.status = facts.status
    acliLive.message = data.message || ''
    acliLive.percent = facts.percent
    if (facts.packageSize) acliLive.packageSize = facts.packageSize
    if (facts.packageSha256) acliLive.sha256 = facts.packageSha256
    if (facts.packageMd5) acliLive.md5 = facts.packageMd5
    if (facts.integrityState) acliLive.integrity = facts.integrityState
    if (facts.attempts) acliLive.attempts = facts.attempts
    if (facts.installOutput) acliLive.installOutput = facts.installOutput
    return
  }
  if (data.type === 'acli_sync_result') {
    const waiter = waiters.get('acli_sync')
    if (waiter) {
      waiters.delete('acli_sync')
      waiter(data)
      return
    }
    // 无等待者的回包（前端已超时、或重试期间的迟到结果）：仍然要把失败落成
    // 可重试的界面状态，否则 bridge 已经在节点上校验过的结论就彻底丢了；
    // 用户会误以为“什么也没发生”而重头再点一次。
    if (data.status && data.status !== 'installed' && data.status !== 'up_to_date') {
      recordAcliFailure(data, 'acli 安装失败')
    }
    return
  }
  if (data.type !== 'log_collect_result') return
  const action = data.log_action
  // 桥已确认 SSH 会话不存在（LC-ENV-006）：前端 SSH 状态立刻置否。
  // 否则 UI 仍显示「已连接」、按钮仍可点，用户会反复误发请求并收到同样的 session_missing——
  // 这是 8×/天 LC-ENV-006 未被前端拦截的最后一环：面板这条 WS 未必是会话的属主连接，
  // 可能没收到 ssh_disconnected，于是状态陈旧。此处用桥的权威判定自愈，避免反复误发。
  if (data.error_code === 'LC-ENV-006' && chatStore.sshConnectionState === 'connected') {
    chatStore.sshConnectionState = 'disconnected'
  }
  // transfer 是持续事件流（拉取/上传各阶段多次回包），单独处理
  if (action === 'transfer') {
    const payload = data.log_data || {}
    transfer.stage = payload.stage || ''
    transfer.percent = payload.percent || 0
    transfer.pulled = payload.pulled_bytes || 0
    if (payload.total_size > 0) transfer.totalSize = payload.total_size
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
  action:
    | 'probe'
    | 'create'
    | 'progress'
    | 'cancel'
    | 'stat'
    | 'transfer'
    | 'acli_sync'
    // 平台兜底还原 spc 槽位：bridge 后台协程会做一次，这里用于重放（幂等）
    | 'restore_spc'
    // 能力探测：旧桥不认识 spc_files，必须先确认再启用平台清单
    | 'capabilities'
    | 'delivery_submit'
    | 'delivery_status'
    | 'delivery_resume'
    | 'delivery_cancel',
  options: Record<string, unknown> = {},
  timeoutMs = 60000,
): Promise<any> {
  return new Promise((resolve, reject) => {
    // 通道不可用时不发请求：ws.send 在非 OPEN 状态下静默丢弃，
    // 发出去只会换来一次 60s 空等，且报出来的"超时"与真因无关。
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      reject(new Error(BRIDGE_CHANNEL_LOST))
      return
    }
    const timer = setTimeout(() => {
      waiters.delete(action)
      waiterRejectors.delete(action)
      reject(new Error(`${action} 超时（${Math.round(timeoutMs / 1000)}s）`))
    }, timeoutMs)
    waiters.set(action, (data) => {
      clearTimeout(timer)
      waiterRejectors.delete(action)
      resolve(data)
    })
    waiterRejectors.set(action, (error) => {
      clearTimeout(timer)
      reject(error)
    })
    if (action === 'acli_sync') {
      // 与 log_collect 同一锚点：不带 node_ip 时 bridge 会去找键为 caseID 的会话，
      // 而真实会话键是 caseID@nodeIP → 必然 session_missing（安装链路根本不会启动）。
      //
      // retry_stage 由兜底按钮带入：upload=复用已校验缓存重传，install=复用节点上的包重装。
      // 不带它就等于“只能从头再来”，用户为了重试一次要重新拉 20~30MB。
      ws.send(
        buildAcliSyncMessage(props.caseId, {
          force: options.force === true,
          minDiskMb: 100,
          nodeIp: nodeIp.value,
          retryStage: (options.retryStage as AcliRetryStage) || 'all',
        }),
      )
    } else if (action === 'delivery_submit') {
      // submit 携带的是完整协议载荷（snake_case）。**不能**走下面的通用组装：
      // 那一层按 camelCase 取字段，会把 source_path / upload_token / total_size /
      // delivery_mode / tls_insecure 全部丢成 undefined，bridge 只收到
      // {type, case_id, node_ip}，直接报 "source_path 不能为空"（2026-10-27 现场）。
      ws.send(
        buildDeliverySubmitMessage(
          options as unknown as DeliverySubmitPayload,
          props.caseId,
          nodeIp.value,
        ),
      )
    } else if (action.startsWith('delivery_')) {
      // status / resume / cancel：字段少且都是单值，仍走通用消息构造
      ws.send(
        buildDeliveryMessage(
          action.replace('delivery_', '') as 'submit' | 'status' | 'resume' | 'cancel',
          props.caseId,
          { ...(options as any), nodeIp: nodeIp.value },
        ),
      )
    } else {
      // 所有动作一律锚定会话主机：nodeIp 是派生值，不存在"表单里另填一个"的可能，
      // 也就不会再出现写成空串（bridge 落到默认会话）或写成不存在节点（session_missing）。
      ws.send(
        buildLogCollectMessage(
          props.caseId,
          // 上面的分支已排除 acli_sync / delivery_*，走到这里只剩真正的 log_collect 动作，
          // 显式收窄，避免带前缀的联合类型被 TS 拒绝（TS2345）。
          action as
            | 'probe'
            | 'create'
            | 'progress'
            | 'cancel'
            | 'stat'
            | 'transfer'
            | 'restore_spc',
          {
            ...(options as any),
            nodeIp: nodeIp.value,
          },
        ),
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
    // 基线跟着任务走：关面板 / 换浏览器 / 重启后接管，仍能判断报告归属
    baselineReportHash.value = String((task.detail ?? {})['baseline_report_hash'] ?? '')
    // spc 生命周期事实必须随任务继承：上次可能没走完还原（关页面 / bridge 重启），
    // 若这里默认 false，界面会显示"已还原"而节点上还留着平台清单。
    spcPendingRestore.value = task.restore_status === 'pending'
    spcHosts.value = task.spc_hosts ?? []
    // 关键：把"这次下发到底收了什么"回填到表单。
    // 平台记录里一直有 modules / 时间范围 / 目标主机，但旧实现接管时只更新进度，
    // 表单仍是空白默认值 → 用户只能看到"采集 0%"，看不到收集内容。
    applyTaskToForm(task)
    stage.value = TASK_STATUS_STAGE[task.status] ?? 'idle'
    if (task.error_message) errorMessage.value = task.error_message
    // 进面板即对账：平台快照可能停在旧投影上（节点仍在跑、或早已异常结束）
    if (sshConnected.value) void syncNodeState({ applyPatches: true, silent: true, source: 'open' })

    // 终态任务仍然对账，但降频：节点可能还在跑（需要「接管」），5s 空转没有意义。
    startAutoSync(TERMINAL_TASK_STATUSES.includes(task.status) ? TERMINAL_POLL_INTERVAL : POLL_INTERVAL)
  } catch (e: any) {
    ElMessage.error(
      e?.response?.status === 404
        ? '任务不存在或不属于当前客户端'
        : customerFaultText('任务状态加载失败', 'LC-HTP-003'),
    )
  }
}

/**
 * 自动刷新：与手动「刷新进度」走**同一个** syncNodeState。
 *
 * 旧实现的自动轮询只 `GET` 平台快照，而平台快照本身不会自己变——
 * 于是节点早已 100%，面板还停在 0%，"自动刷新"看着像没生效。
 * 统一入口后，自动与手动在任何时刻都得到同一个结论。
 */
function startAutoSync(interval = POLL_INTERVAL) {
  stopAutoSync()
  // 主流程独占轮询权：它自己会推进状态机，再叠一条自动对账只会两条链路抢写
  // 同一行，输的那条拿 409 并中断交付（Q2026110900356）。
  if (mainFlowActive.value) return
  resumeTimer = setInterval(() => {
    if (!visible.value) return
    void triggerAutoSync()
  }, interval)
}

function stopAutoSync() {
  if (resumeTimer) {
    clearInterval(resumeTimer)
    resumeTimer = null
  }
}

async function triggerAutoSync() {
  // 定时器可能在主流程启动前就排好了这一跳：进函数再判一次，防止迟到的对账抢先写库。
  if (mainFlowActive.value) return
  const action = await syncNodeState({ applyPatches: true, silent: true, source: 'auto' })
  // 落终态后降频而不是停摆：平台虽已终态，节点可能仍在跑（需要「接管」）或刚异常，
  // 完全停轮询会让接管入口再也不出现；30s 一次足以发现节点态翻转。
  if (action && (action.action === 'patch_completed' || action.action === 'patch_failed')) {
    startAutoSync(TERMINAL_POLL_INTERVAL)
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
// 对账互斥：自动轮询、手动刷新、打开面板可能同时触发，progress 请求不能并发
let syncingNode = false
// 自动写库熔断：一旦平台以 4xx 明确拒绝，重试同一请求永远不会成功（现场是
// failed→completed 的 409 被每 30 秒重发一次）。用户发起新动作时再解除。
let syncPatchBlocked = false

const lastRefreshText = computed(() => {
  if (!lastRefreshAt.value) return ''
  const hh = String(lastRefreshAt.value.getHours()).padStart(2, '0')
  const mm = String(lastRefreshAt.value.getMinutes()).padStart(2, '0')
  const ss = String(lastRefreshAt.value.getSeconds()).padStart(2, '0')
  return `更新于 ${hh}:${mm}:${ss}`
})

/**
 * 「刷新进度」= 把节点态同步到平台态（不是把平台旧投影再读一遍）。
 *
 * 节点是唯一真相源：先读节点快照，再由 decideNodeSync 决定是否修正平台状态；
 * 只有在 SSH 断开拿不到节点时才退回"只读平台快照"，并明确告知用户。
 */
async function refreshProgress(silent = false) {
  if (refreshingProgress.value) return
  const taskId = currentTask.value?.task_id || resumedTaskId.value
  if (!taskId) {
    if (!silent && !sshConnected.value) ElMessage.info('当前没有可查询的采集任务')
    // 平台上没有任务记录时，对账仍能发现"节点有游离任务"
    if (sshConnected.value) await syncNodeState({ applyPatches: false, silent })
    return
  }
  refreshingProgress.value = true
  try {
    const latest = await logCollectApi.getTask(taskId)
    currentTask.value = latest
    // 基线跟着任务走：本地为空时（换端接管）从任务记录恢复
    if (!baselineReportHash.value) baselineReportHash.value = baselineFromTask()
    // completed → done 只在"真的交付了"时成立：stage=done 的文案是「日志已归档」，
    // 采完但平台零产物时落到 done 就是假成功（Q2026110900356）。
    stage.value =
      latest.status === 'completed' && !hasDeliveryEvidence(latest)
        ? 'idle'
        : (TASK_STATUS_STAGE[latest.status] ?? stage.value)
    if (latest.error_message) errorMessage.value = latest.error_message

    if (!sshConnected.value) {
      if (!silent) ElMessage.warning('SSH 会话已断开，节点侧进度不可用；已刷新平台任务状态')
      lastRefreshAt.value = new Date()
      return
    }
    // 主流程在跑时它自己就是对账者：这里再发一次会与它抢写同一行（见 mainFlowActive）。
    if (mainFlowActive.value) {
      lastRefreshAt.value = new Date()
      return
    }
    // 与自动刷新同一个入口：任何时刻两者的结论必须一致
    const action = await syncNodeState({ applyPatches: true, silent, source: 'manual' })
    if (action && !silent && !['patch_completed', 'patch_failed'].includes(action.action)) {
      ElMessage.info(buildSyncNotice(action))
    }
    if (!silent && !nodeSyncHint.value) ElMessage.success('已刷新任务进度')
  } catch (e: any) {
    if (!silent) {
      ElMessage.error(
        e?.response?.status === 404
          ? '任务不存在或不属于当前客户端'
          : customerFaultText('任务进度查询失败', 'LC-HTP-003'),
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
 * 因此恢复顺序固定为：进行中 → 最近一次已完成 → 最近一次失败 → 空白表单（可重新收集）。
 * 失败记录必须恢复：节点侧任务可能仍在跑，接管只能从这条记录发起；退回空白表单
 * 会让用户以为"没有任务"，只能重新下发并撞插件互斥（2026-10-27 修正）。
 */
async function restoreTaskState(force = false) {
  // 主流程在跑时重开抽屉：任务与进度都在内存里，直接展示即可。
  // 这里若再对账一次，两条链路会同时写状态机——对账抢先写 completed，
  // 主流程的 PATCH uploading 就会吃 409 并中断交付（Q2026110900356）。
  if (mainFlowActive.value) return
  if (!force && (currentTask.value || viewMode.value === 'result')) {
    // 同一会话内重开面板：任务与基线都还在，必须把自动刷新接回来。
    // 旧实现这里直接 return，于是「关抽屉 → 再打开」后自动刷新永久失效，
    // 只有手动点「刷新进度」才会动。
    if (currentTask.value) {
      // 终态任务同样要恢复对账：failed 的节点任务可能仍在跑，
      // 「接管」入口依赖这一次对账（同会话重开面板也走这里）。
      if (sshConnected.value) void syncNodeState({ applyPatches: true, silent: true, source: 'open' })
      startAutoSync(
        TERMINAL_TASK_STATUSES.includes(currentTask.value.status) ? TERMINAL_POLL_INTERVAL : POLL_INTERVAL,
      )
    }
    return
  }
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
    // 展示"最近一条终态任务"，而不是"任意一条 completed"：
    // 用户刚发起的那次尝试（哪怕失败）才是他要看的结论。取任意 completed 会让
    // 几小时前的旧任务顶掉刚失败的新任务，连交付路径看起来都"变了"
    // （Q2026102701193：用户选 P2 建的任务 failed，面板却展示早上那条 P1 的
    //  "已由插件直传"，于是用户质疑"我明明选的 P2"）。
    const latest = pickDisplayTask(items)
    if (latest) {
      historyTasks.value = items.slice(0, 5)
      showResult(latest)
      // 完成 ≠ 已交付：结果态也必须对账一次，否则"包还在节点上、平台没产物"
      // 的任务永远拿不到「上传到平台」，用户只剩"重新采集"这一条错路。
      if (sshConnected.value) void syncNodeState({ applyPatches: true, silent: true, source: 'open' })
      startAutoSync(TERMINAL_POLL_INTERVAL)
      return
    }
    // 失败记录最后恢复：节点侧任务可能仍在运行，此时唯一正确的下一步是「接管」，
    // 而不是重新下发（插件单任务互斥会直接拒绝 create）。
    const failed = items.find((item) => item.status === 'failed')
    if (failed) {
      viewMode.value = 'form'
      await resumeTask(failed.task_id)
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
  // stage=done 只表示"整条链路走完"，而它的文案是「日志已归档到日志服务器」。
  // 采集完成但平台零产物时置 done，会让进度条变绿、步骤条 4/4、文案宣称已归档——
  // 三个假成功信号同时出现（Q2026110900356）。没有交付证据就停在 idle。
  stage.value = hasDeliveryEvidence(task) ? 'done' : 'idle'
  progress.value = null
  // 结果态的数据全部来自平台任务记录，本次会话的峰值到此为止
  observedArchiveSize.value = 0
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
/**
 * 把「平台任务记录」回填到表单。
 *
 * 平台在 create 时已经存下 modules / date_from / date_to / ips，
 * 但这只是数据库里的事实，用户接管任务时必须能在界面上看见——
 * 「只显示进度、看不到这次到底收了什么」是 2026-10-26 现场的直接反馈。
 *
 * 这里只做赋值、不做裁剪：模块清单要等探测回来才知道哪些合法，
 * applyCatalog 拿到目录后会过滤非法值，避免在探测前把合法模块误删。
 */
function applyTaskToForm(task: LogCollectTask) {
  if (task.date_from && task.date_to) form.dateRange = [task.date_from, task.date_to]
  const ips = task.ips ?? []
  // 空数组与 all 同义：都表示"集群全部在线节点"
  form.ips = !ips.length || ips.includes('all') ? 'all' : ips.join(',')
  if (task.modules?.length) form.modules = [...task.modules] as LogCollectModule[]
  // 额外路径必须一起回填：重新收集时静默丢掉，用户会以为"这次范围和上次一样"，
  // 而实际上少收了他上次特意指定的目录。
  form.extraPaths = normalizeExtraPaths(task.extra_paths)
  // 模式必须一起回填：重新收集时静默退回默认模式，等于少收上次特意指定的路径。
  if (task.custom_scenario_id) {
    form.scenarioMode = 'custom'
    form.customScenarioId = String(task.custom_scenario_id)
  } else {
    form.scenarioMode = 'default'
    form.customScenarioId = ''
  }
}

function startRecollect() {
  const last = resultTask.value
  viewMode.value = 'form'
  stage.value = 'idle'
  currentTask.value = null
  resumedTaskId.value = null
  progress.value = null
  // 上一轮的压缩包大小不能带进新任务，否则新包还没生成就显示旧包的大小
  observedArchiveSize.value = 0
  // 重新收集 = 新一次下发：上一轮的基线 / 提示 / 时间戳全部作废。
  // 否则新任务头几秒会拿旧基线比对，或者继续显示上一轮的"已完成"提示。
  baselineReportHash.value = ''
  // L1/L2 证据属于**某一次下发**，不能跨任务继承：上一轮见过 running/上传，
  // 不代表新任务也见过，否则新任务的第一份残留报告就能直接过关。
  sessionAdopted.value = false
  uploadPhaseSeen.value = false
  requireSessionAdopt.value = false
  nodeSyncHint.value = ''
  nodeAction.value = ''
  // 新一次下发 = 解除上一轮的写库熔断
  syncPatchBlocked = false
  lastRefreshAt.value = null
  stopAutoSync()
  relayStatus.value = 'none'
  resetTransfer()
  cleanupTip.value = ''
  errorMessage.value = ''
  if (last) applyTaskToForm(last)
  if (prepareState.value !== 'ready') void prepare({ silent: true })
}

// ── 主流程 ──────────────────────────────────────────────────────────────────

/**
 * 未连接时的主按钮 = 「连接 SSH」，直接弹与右侧终端同一个登录框。
 *
 * 第一性原理：按钮上的动作必须可执行。旧实现把「请先连接 SSH」写成一个 disabled 按钮，
 * 等于把唯一的前置条件变成一个死胡同：用户被要求做一件事，却不给出做这件事的入口，
 * 只能自己发现右侧终端里还有一个「连接 SSH」（2026-11-02 现场反馈）。
 *
 * 对抗性审查：
 * - 会话键是 `caseID@nodeIP`，因此登录必须带**本面板绑定的工单号**。带错单号登录后，
 *   probe 照样会拿到 session_missing，表现为"连上了却读不到环境"。
 * - 登录成功后本面板不必手动刷新：`nodeIp`（会话主机）从空变有，watch 会重探，
 *   按钮随之从「连接 SSH」变回「开始收集」。
 * - bridge 未运行时不能直接把用户丢进表单：先探一次，未运行则由弹框给出下载引导。
 */
const sshConnectOpening = ref(false)

async function requestSshConnect(): Promise<void> {
  if (sshConnectOpening.value) return
  sshConnectOpening.value = true
  try {
    const status = await checkBridgeBeforeOpen()
    chatStore.sshConnectDialogBridgeStatus = status
    chatStore.openSshFlowDialog(props.caseId || null, 'log-collect')
  } finally {
    sshConnectOpening.value = false
  }
}

/**
 * 底部主按钮的唯一入口：能采集就采集，不能采集就把"缺的那一步"变成可点的动作。
 *
 * 保留 handleStart 内的守卫作为兜底：弹框开着期间会话可能又断开，
 * 或用户用键盘触发，都不能把注定失败的请求发出去。
 */
async function handlePrimaryAction(): Promise<void> {
  if (!canCollect.value) {
    await requestSshConnect()
    return
  }
  await handleStart()
}

async function handleStart() {
  if (!form.dateRange || form.dateRange.length !== 2) {
    ElMessage.warning('请选择时间范围')
    return
  }

  // 跨度必须在提交前收敛：后端只接受 30 天内，超了回 422 且不回显原因。
  // 放在最前面是因为它不需要任何前置条件（会话/探测/路径都不影响这个判断），
  // 注定失败的请求连 bridge 通道都不该打开。
  const dateRangeCheck = checkDateRange(form.dateRange[0], form.dateRange[1])
  if (!dateRangeCheck.ok) {
    ElMessage.warning(dateRangeCheck.reason)
    return
  }

  // 新一轮收集先清空上一轮的 spc 生命周期事实：残留的 pending 会在 finally 里
  // 对着新任务发 restore_spc，把"根本没动过清单"的任务标成 pending/success。
  spcPendingRestore.value = false
  spcHosts.value = []

  // 自定义模式没选场景时不能静默回落到"只跑 other"：那会让用户以为
  // 自己指定的路径被收了，实际一条都没下发。
  if (form.scenarioMode === 'custom' && !form.customScenarioId) {
    ElMessage.warning('请选择一个自定义场景，或切回默认场景')
    return
  }

  // 会话必须先活着。openLogCollect 只在「打开面板那一刻」校验过，
  // 面板开着期间 SSH 可能已经断开，此时任何 bridge 动作都只会拿到 session_missing——
  // 与其把注定失败的请求发出去，不如当场告诉用户去重连。
  if (!sshConnected.value) {
    ElMessage.warning('SSH 会话已断开，请点击面板底部「连接 SSH」重新登录后再开始收集')
    return
  }

  const targetNode = nodeIp.value
  if (!targetNode) {
    ElMessage.warning(NODE_MISSING_MESSAGE)
    return
  }

  // 目标主机必须在下发前收敛成唯一合法形态：手填是自由文本，而 bridge 只认
  // `all` 或逗号分隔 IPv4。等建完任务再被节点拒绝，等于白留一条 failed 记录。
  const targetHosts = normalizeTargetHosts()
  if (!targetHosts) {
    ElMessage.warning('目标主机只能填 all 或英文逗号分隔的 IP，且 all 不能与具体 IP 混写')
    return
  }

  // 额外路径在提交前再收敛一次：历史任务回填或浏览器自动填充可能带来脏值，
  // 而 bridge 侧一旦拒绝，用户看到的只是"任务下发失败"，无法定位到具体路径。
  const extraPaths = normalizeExtraPaths(form.extraPaths)
  for (const path of extraPaths) {
    const check = validateExtraPath(path)
    if (!check.ok) {
      ElMessage.warning(`额外路径不合法（${path}）：${check.reason}`)
      return
    }
  }
  form.extraPaths = extraPaths

  cleanupTip.value = ''
  errorMessage.value = ''
  failedStep.value = 0
  // 交付进度必须整组归零：上一轮的 totalSize 会成为新任务的假分母，
  // 上一轮的 phase 会让新任务刚下发就显示「等待压缩包写入」。
  resetTransfer()
  currentTask.value = null
  progress.value = null
  // 新任务从 0 开始记：不继承上一次的峰值
  observedArchiveSize.value = 0
  relayStatus.value = 'none'
  // 新任务重新开始：上一次失败的规范码不能带进来，否则会把旧码写到新任务上。
  lastNodeErrorCode = ''
  // create 输出证据同样属于某一次下发，不能跨任务继承。
  createOutputEvidence = ''
  const token = ++runToken

  let ws: WebSocket
  try {
    ws = await openSocket()
  } catch (error: any) {
    showCustomerFault(error.message)
    return
  }

  // 从这里到 finally 之间，状态机只允许主流程写；自动对账/重开面板都不得插手。
  // 开跑前先把残留的自动对账停掉：它可能在上一轮终态后仍以 30s 节奏在跑。
  const myFlow = ++mainFlowSeq
  mainFlowActive.value = true
  stopAutoSync()

  // 自定义模式固定 -m other：config 的 modules 永不生效，显式传 other 让
  // "专项日志照常收"成为可预期事实，而不是依赖 acli 的默认值。
  const modules = isCustomMode.value
    ? (['other'] as LogCollectModule[])
    : (form.modules.length ? form.modules : (['other'] as LogCollectModule[]))
  stage.value = 'probing'
  try {
    // 复用准备阶段的探测结果：打开面板时已经读过集群，这里再探一次纯属浪费，
    // 且会重复触发 acli help 解析。仅在未准备/准备失败时现场补探。
    // 补探不重试（retries: false）：用户已经点了「开始收集」，等待时间必须可预期——
    // 最坏情况下的退避叠加会把一次下发拖上几分钟，反而不知道卡在哪。
    if (prepareState.value !== 'ready' || !probeResult.value) {
      const ready = await prepare({ silent: true, retries: false })
      if (!ready || !probeResult.value) throw new Error(prepareError.value || '环境准备失败')
    }

    if (!probeResult.value.acli_installed) {
      await confirmInstall(ws)
    }

    // 只把结论落到默认值，不问用户：可达 P1、不可达 P2（P1 已置灰）。
    // 旧实现「不可达时不静默选 P2」的初衷是不替用户决定交付路径，但那样会把
    // 一个必然失败的选项留在台面上；现在改成默认值 + 禁选，用户仍可把 P1 改回 P2。
    applyDefaultPathMode()
    if (!pathMode.value) {
      stage.value = 'idle'
      ElMessage.warning('请选择交付路径（尚未获取探测结论）')
      return
    }

    // 守卫前把「最近一条任务」接回面板：failed 记录若未恢复，平台态为空会把
    // adopt 误判成 warn_node_busy（文案说"非本平台下发"），用户被引导去取消重试，
    // 接管入口永远不出现。autoSync=false：对账交给下面的守卫统一做，
    // 避免并发发两次 progress（syncingNode 互斥会让守卫直接返回 null）。
    if (!currentTask.value) {
      try {
        const latestList = await logCollectApi.listTasks(props.caseId, 1)
        const latest = latestList.items?.[0]
        // 只回填任务态，不调 applyTaskToForm：用户可能刚改过场景/时间范围，
        // 这里再把旧任务参数写回表单，等于静默覆盖用户刚做的选择。
        if (latest) currentTask.value = await logCollectApi.getTask(latest.task_id)
      } catch {
        // 恢复失败不阻断下发：守卫退化为"无平台态"，仍可继续探测节点
      }
    }

    // 前置守卫：节点上有未结束/游离任务时，create 只会被插件互斥拒绝，
    // 平台还会白留一条 failed 记录。先对账，能自动修正的直接修正，需要用户决策的当场拦住。
    const preSync = await syncNodeState({ applyPatches: true, silent: true, source: 'preCreate' })
    // 是否该拦见 blocksCreate：只拦"节点上有活会抢同一会话"，补交付待办不拦。
    if (blocksCreateAction(preSync) && preSync) {
      stage.value = 'idle'
      ElMessage.warning(buildSyncNotice(preSync))
      return
    }

    // 下发前锚定基线：create 不会让报告立刻刷新，下发后的头几次 progress
    // 仍可能是上一次任务的完成报告。指纹随任务入库（detail.baseline_report_hash），
    // 换浏览器/重启后接管任务时仍能把"这一份报告属于哪次下发"判清楚。
    //
    // 基线必须来自**刚刚实读的节点报告**，不能直接用 progress.value：
    // 判为 wait_refresh 时刻意不更新 progress（避免把旧报告的 100% 显示出来），
    // 此时 progress.value 比节点落后一个版本。拿它当锚点，下发后的第一份报告
    // 一定"看起来变了"，归属门就形同虚设 —— Q2026110666667 正是这样复发的
    // （基线 2698 字节，节点当时已是 2776 字节，3 秒的新任务被上一轮报告判完成）。
    // 显式标注类型：上面刚把 progress 置过 null，TS 会把 .value 收窄成 null
    let baselineSnapshot: ProgressSnapshot | null = progress.value
    if (!preSync || preSync.action === 'wait_refresh') {
      baselineSnapshot = (await readNodeSnapshot(20000, true)) ?? progress.value
    }
    const baseline = hashReport(baselineSnapshot?.raw ?? '')
    baselineReportHash.value = baseline
    // L1/L2 证据属于某一次下发，新任务必须从零开始积累
    sessionAdopted.value = false
    uploadPhaseSeen.value = false

    // 唯一调用链：REST 建任务与 WS 下发必须共用同一个 trace_id。
    // 缺了它，桥侧只能回退到 lc-unknown，日志无法把「建任务 → 下发 → 进度」串起来。
    const traceId = `lc-${props.caseId}-${Date.now()}`

    let task: LogCollectTask
    try {
      task = await logCollectApi.createTask({
        case_id: props.caseId,
        // 工单号只有一个：acli -s、产物名与任务归属都使用 case_id
        origin_node_ip: targetNode,
        modules,
        date_from: form.dateRange[0],
        date_to: form.dateRange[1],
        ips: targetHosts.split(','),
        // 额外路径随任务落库：失败任务事后要能回答"这次到底多收了什么"，
        // 只留在前端内存里，换端或关面板后就永久丢失。
        extra_paths: form.extraPaths.length ? [...form.extraPaths] : undefined,
        // 只传 ID：配置内容由服务端按最新 revision 生成（含 {date} 展开、合并 -p）。
        custom_scenario_id: isCustomMode.value ? form.customScenarioId : undefined,
        remote_url: pathMode.value === 'P1' ? RELAY_URL : undefined,
        // 交付路径必须随任务落库：只放在前端内存里的话，采集中的任务、以及
        // P1 失败降级后的任务都无法回答"当初选的是哪条路"。
        upload_mode: pathMode.value === 'P1' ? 'acli_direct' : 'bridge_relay',
        // 探测事实随任务落库：失败任务必须能回答"哪版 acli / 什么架构"，
        // 只留在前端内存时，关面板或换端后这条证据就没了。
        arch: probeResult.value?.architecture || undefined,
        acli_version: probeResult.value?.acli_version || undefined,
        baseline_report_hash: baseline || undefined,
        // 探测快照随任务落库：失败任务事后要能回答"当时那台机器是什么状态"。
        // 只留在前端内存里，关面板或换端就永久丢失（2026-10-28 现场 detail 仅一个指纹）。
        probe: probeResult.value
          ? {
              acli_version: probeResult.value.acli_version,
              acli_build_time: probeResult.value.acli_build_time,
              acli_binary: probeResult.value.acli_binary,
              architecture: probeResult.value.architecture,
              acli_installed: probeResult.value.acli_installed,
              hosts_count: probeResult.value.hosts?.length ?? 0,
              modules_count: probeResult.value.modules?.length ?? 0,
              catalog_source: probeResult.value.catalog_source,
              catalog_note: probeResult.value.catalog_note,
              log_server_reachable: probeResult.value.log_server_reachable,
              http_code: probeResult.value.http_code,
              note: probeResult.value.note,
            }
          : undefined,
        trace_id: traceId,
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
    // 一次性载荷只出现在创建响应里（平台不留副本）：后续任何 patch 都可能把它
    // 从 task 上抹掉。先固化成局部事实，能力探测与下发都认它。
    const issuedSpcFiles = task.spc_files ?? undefined
    const issuedSpcSlots = task.spc_slots ?? undefined
    // P1 凭据只在建任务响应里出现一次：拿不到就说明平台没配票据密钥，
    // 此时发 P1 只会换来一次 401，并在界面上留下一个"直传失败"的假故障。
    //
    // 这里直接把本次下发改写成 P2，同时把落库路径改回 bridge_relay：
    // 让"用户看到的路径"与"实际走的路径"保持一致，而不是靠事后降级记录去解释。
    let p1Ticket = ''
    if (pathMode.value === 'P1') {
      p1Ticket = String(task.p1_ticket || '')
      if (!p1Ticket) {
        ElMessage.warning('平台未配置日志服务器直传凭据，本次已自动改走平台中转')
        pathMode.value = 'P2'
        // 合并而不是整体替换：创建响应里的 config_json / spc_json / spc_files
        // 只在内存里出现一次，被 patch 结果整体覆盖就等于"平台没下发过"。
        const patched = await logCollectApi.patchTask(task.task_id, {
          upload_mode: 'bridge_relay',
          detail: {
            upload_mode_switched_from: 'acli_direct',
            upload_mode_switched_reason: '平台未配置 P1 直传票据，创建时改走 P2',
          },
        })
        task = { ...task, ...patched }
      }
    }
    currentTask.value = task
    // 本次会话亲自下发的任务：必须等节点出现 running（采集起点）才认 completed。
    // 这是唯一发生"读到上一轮报告"的场景——acli 单任务、无句柄，下发瞬间读到的
    // 仍是上一个任务的状态（Q2026110666667：3 秒被判完成）。
    requireSessionAdopt.value = true

    stage.value = 'creating'
    // 旧桥不认识 spc_files 会直接忽略并继续按节点原清单收集，这是最危险的
    // 「看起来成功了」：必须先探测能力，不支持就明确报错，绝不静默降级。
    let capabilities: any = null
    // 有证据票据就顺便做一次能力协商：旧桥不上报速率/网络/包内容/进程日志时，
    // 界面必须说「桥接端版本较旧」，而不是把缺失数据显示成 0 或绿色成功。
    if (issuedSpcFiles?.length || task.evidence_ticket) {
      try {
        capabilities = await sendAndWait(
          ws,
          'capabilities',
          { ticketNo: props.caseId, execId: task.task_id, traceId },
          8000,
        )
      } catch {
        capabilities = null
      }
      bridgeEvidenceCapable.value = capabilities?.status === 'ok'
        && capabilities.log_data?.log_collect_evidence === true
    }
    if (issuedSpcFiles?.length
      && (!capabilities || capabilities.status !== 'ok' || !capabilities.log_data?.multi_slot_spc)) {
      throw new Error('当前终端桥不支持按场景清单收集，请先升级桌面桥再重试（不会按原清单静默收集）')
    }

    const created = await sendAndWait(
      ws,
      'create',
      {
        nodeIp: targetNode,
        // acli -s 与任务归属使用同一个平台工单号
        ticketNo: props.caseId,
        // spc 覆写按任务隔离备份（core.json.platform.<task_id>.bak）：
        // 缺 exec_id 会被桥以 LC-PLUGIN-014 拒绝，且终态无法还原清单。
        execId: task.task_id,
        // 证据票据只在创建响应里出现一次：bridge 用它上报探测/速率/包内容/进程日志。
        // 前端纯透传，不落本地存储、不写进界面。
        evidenceTicket: task.evidence_ticket || undefined,
        // 证据数据面与页面同源：bridge 不猜地址，也避免把证据上报到错误环境。
        platformUrl: window.location.origin,
        evidenceTLSInsecure: platformCertTrusted(window.location.origin),
        traceId,
        modules,
        dateFrom: form.dateRange[0],
        dateTo: form.dateRange[1],
        ips: targetHosts,
        // 自定义模式下 extraPaths 已在服务端合并进 config.paths：
        // 同时传 -c 与 -p 会让 CLI 覆盖 config.paths，bridge 也会直接拒绝。
        extraPaths: isCustomMode.value
          ? undefined
          : (form.extraPaths.length ? [...form.extraPaths] : undefined),
        // 平台签发的配置原文与摘要只在创建响应里出现一次，前端纯透传。
        configJson: task.config_json || undefined,
        configSha256: task.config_sha256 || undefined,
        // spc 场景：原文与摘要直接透传，bridge 负责扇出写入与终态恢复。
        spcJson: task.spc_json || undefined,
        spcSha256: task.spc_sha256 || undefined,
        // 默认场景逐槽位清单：单场景时就是「替换 vm_start.json + -m vm_start」。
        spcFiles: issuedSpcFiles,
        spcSlots: issuedSpcSlots,
        remoteUrl: pathMode.value === 'P1' ? RELAY_URL : undefined,
        // bridge 会把它注入成 `-L 0:Authorization:Bearer <ticket>`
        uploadTicket: p1Ticket || undefined,
      },
      180000,
    )
    // 无论 status 是否 ok 都要留证：exit_code=0 但节点没接单时，
    // 这份输出是唯一能定位 acli 侧真因的物证。兼容旧版 bridge：
    // 旧桥的 raw 未脱敏，落库前必须再抹一次 Bearer，且限长避免写放大。
    createOutputEvidence = String(created.raw || '')
      .replace(/Bearer\s+[A-Za-z0-9._~+/=-]+/g, 'Bearer [REDACTED]')
      .slice(0, 16 * 1024)
    if (created.status !== 'ok') {
      // 下发失败的规范码必须留住：它是后面 patch failed 写库的唯一来源。
      lastNodeErrorCode = String(created.error_code || '')
      throw new Error(created.message || '任务下发失败')
    }

    // 配置文件落点 + 预检结论随任务落库：只留在桥日志里，换端/关页面后
    // 就无法回答"这次到底要收什么、哪些路径没找到"。
    const configReceipt = (created.log_data ?? {}) as {
      config_path?: string
      preflight_missing?: string[]
    }
    if (configReceipt.config_path) {
      try {
        await logCollectApi.patchTask(task.task_id, {
          config_path: configReceipt.config_path,
          preflight_missing: configReceipt.preflight_missing ?? [],
        })
      } catch (patchError: any) {
        // 证据丢失不改变采集事实，但必须可见：不能静默当成"没发生"。
        console.warn('[log-collect] 配置回执落库失败', patchError?.message || patchError)
      }
      if (configReceipt.preflight_missing?.length) {
        ElMessage.warning(
          `有 ${configReceipt.preflight_missing.length} 条路径在发起节点上不存在，可能不会被收集`,
        )
      }
    }

    // spc 回执：此刻平台清单正覆写在每台节点的 core 槽位上，备份已就位。
    // 逐台证据必须落库——换端/关页面后，只看桥日志无法回答"改了哪几台、
    // 备份叫什么"，而恢复只能靠备份名（<file>.platform.<task_id>.bak）重放。
    const spcReceipt = (created.log_data ?? {}) as {
      spc_slot?: string
      spc_slots?: string[]
      spc_sha256?: string
      spc_hosts?: Array<{ ip: string; backup?: string; error?: string }>
      restore_status?: string
    }
    if (spcReceipt.spc_slot || spcReceipt.spc_hosts?.length) {
      spcPendingRestore.value = true
      spcHosts.value = spcReceipt.spc_hosts ?? []
      try {
        await logCollectApi.patchTask(task.task_id, {
          spc_slot: spcReceipt.spc_slot ?? 'core',
          spc_slots: spcReceipt.spc_slots ?? [],
          spc_sha256: spcReceipt.spc_sha256,
          spc_hosts: spcReceipt.spc_hosts ?? [],
          restore_status: spcReceipt.restore_status ?? 'pending',
        })
      } catch (patchError: any) {
        // 证据丢失不改变采集事实，但恢复要靠它：必须可见，不能静默吞掉。
        console.warn('[log-collect] spc 回执落库失败', patchError?.message || patchError)
      }
      const missing = (spcReceipt.spc_hosts ?? []).filter((host) => host.error)
      if (missing.length) {
        ElMessage.warning(
          `有 ${missing.length} 台节点未能写入场景清单，已在发起节点前中止本次采集`,
        )
      }
    }

    // 平台任务必须随阶段推进：pending 不能直接跳到终态，否则 PATCH 会被状态机拒绝
    stage.value = 'collecting'
    currentTask.value = await logCollectApi.patchTask(task.task_id, { status: 'collecting' })
    await resumeTaskFlow(ws, task, token, baseline)
  } catch (error: any) {
    // 用户已取消：后台流程静默退出，不再覆盖任务状态
    if (token !== runToken) return
    errorMessage.value = normalizeErrorMessage(String(error?.message || error))
    // 必须在改 stage 之前取：stepActive 一旦看到 failed 就会返回 failedStep 本身，
    // 顺序颠倒会让它永远停在第一步。放在这里可同时覆盖"结果未知"与普通失败两条分支。
    failedStep.value = stepActive.value
    // 轮询超时 / 平台瞬时不可用都属于"结果未知"：不替服务端写终态，否则会出现
    // UI 报失败而后台实际成功（2026-11-06 Q2026110600940：case-service 被
    // OOMKilled 的两秒里网关兜底 503，前端据此把已交付的任务写成 failed）。
    if (error instanceof RelayPendingError) {
      errorMessage.value = normalizeErrorMessage(error.message)
      stage.value = 'failed'
      ElMessage.warning(errorMessage.value)
      return
    }
    // 采集结论已经落库（completed）时，剩下的失败只可能发生在交付段。
    // 此时既不能把采集改写成 failed（状态机本来也拒绝，硬写只会多一条 409 噪音），
    // 更不能让用户以为"采集失败要重新收集"——包好端端地在节点上，只需要补交付。
    if (currentTask.value?.status === 'completed') {
      errorMessage.value = `日志尚未交付：${errorMessage.value}（采集已完成，可点「上传到平台」重试，不需要重新采集）`
      stage.value = 'idle'
      ElMessage.warning(errorMessage.value)
      return
    }
    if (isOutcomeUnknownError(error)) {
      errorMessage.value = '平台暂时不可用，任务仍在后台进行；请稍后点「刷新进度」确认结果'
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
          // 桥侧规范码：没有它，失败原因只能靠读 error_message 文本猜。
          // 平台侧异常同样要保留规范码——只写 axios 原文（"Request failed with
          // status code 500"）会让 error_code 永远为空，事后完全无法归因。
          error_code: lastNodeErrorCode || platformErrorCode(error) || undefined,
          // 失败必须留下节点原始证据：只写一句「节点上已无任务」时，
          // 事后无法区分是 acli 崩溃、专项配置错误还是会话被踢。
          ...(createOutputEvidence ? { detail: { create_output: createOutputEvidence } } : {}),
        })
        // 返回值必须回填：诊断卡要按最新的 error_code 给出检查/处理建议，
        // 不回填就只能等下一次轮询，失败瞬间用户看到的还是上一条任务的码。
        .then((task) => {
          if (task) currentTask.value = task
        })
        .catch(() => undefined)
    }
    // 兜底文案必须可行动：只显示"日志收集失败"等于没给任何信息，
    // 现场只能再点一次碰运气（2026-11-12 Q2026110302155）。
    showCustomerFault(errorMessage.value || UI_TEXT.fallback.no_code, failureCode.value || undefined)
  } finally {
    // 主流程退出（成功/失败/取消）后必须交还写权限，否则自动对账永久停摆，
    // 结果态里「上传到平台」与归档状态就再也不会随节点态刷新。
    // 只有最新一代流程可以解锁：旧流程的 finally 迟到时不得抢走新流程的锁。
    if (mainFlowSeq === myFlow) {
      mainFlowActive.value = false
      if (token === runToken && visible.value && currentTask.value) {
        startAutoSync(
          TERMINAL_TASK_STATUSES.includes(currentTask.value.status) ? TERMINAL_POLL_INTERVAL : POLL_INTERVAL,
        )
      }
      // spc 槽位必须在流程退出时还原：失败/取消同样要撤掉平台清单，
      // 否则下一次采集会拿着上一轮的场景继续收，且没人知道它来自哪里。
      // 旧流程（token 已换代）不得插手新任务的节点状态。
      if (token === runToken && spcPendingRestore.value && currentTask.value) {
        void restoreSpcSlot(ws, currentTask.value.task_id)
      }
    }
  }
}

function resetAcliLive() {
  acliLive.status = ''
  acliLive.message = ''
  acliLive.percent = 0
  acliLive.packageSize = 0
  acliLive.sha256 = ''
  acliLive.md5 = ''
  acliLive.integrity = ''
  acliLive.attempts = 0
  acliLive.installOutput = ''
}

/**
 * 把一次 acli 同步失败转换成可展示、可重试的失败态。
 *
 * 关键约定：**不丢弃 facts**。历史实现只把 message 抛给上层，于是界面只剩
 * 一句“上传失败”，既不知道包多大、摘要是什么，也不知道该重传还是重装。
 */
function recordAcliFailure(data: any, fallbackMessage: string) {
  const facts = parseAcliSyncFacts(data)
  const stageHint = facts.stage || ''
  const suggestions = acliRetrySuggestions({ ...facts, status: 'error' })
  acliFailure.value = {
    message: String(data?.message || fallbackMessage),
    stage: stageHint,
    // 建议为空只会在“不可重试”（如磁盘不足）时出现：那类失败重试也不会变好。
    suggestions,
    facts,
  }
  acliLive.installOutput = facts.installOutput || acliLive.installOutput
}

/** 安装/重试成功后清空失败态，并把 acli 标记为可用 + 重探集群能力。 */
async function markAcliInstalled() {
  acliFailure.value = null
  resetAcliLive()
  probeResult.value = { ...(probeResult.value as ProbeResult), acli_installed: true }
  // 安装成功后必须重探：节点清单与场景目录只有 acli 可用后才读得到，
  // 不重探会让用户"装完 acli 仍看到读不到节点清单"，误判为安装无效。
  // 只探一次（retries: false）：安装已经成功，重探失败不该再拖住用户等待；
  // 真没探到会由事实复查在 30s 后自动补读。
  const refreshed = await prepare({ silent: true, retries: false })
  if (!refreshed && prepareState.value === 'failed') {
    // 重探失败不改写安装结果，也不让面板停在红色错误态：采集仍可继续（-i all 合法）
    prepareState.value = 'ready'
    prepareError.value = ''
    // 重探失败不能就此停在旧快照：交给事实复查把「装好了但没读到清单」补上，
    // 否则用户又回到「必须 F5」的老路
    scheduleFactRecheck()
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
  resetAcliLive()
  acliFailure.value = null
  // 超时预算跟 bridge 的分段预算对齐（下载 120s + 上传 300s + 安装 240s）：
  // 前端先超时会让用户误以为已经失败，并叠出第二次上传。
  const result = await sendAndWait(ws, 'acli_sync', {}, ACLI_SYNC_TIMEOUT_MS)
  // 除了 installed/up_to_date 之外都是失败——包括 disk_space_insufficient：
  // 历史实现只判 status==='error'，会把“磁盘不足”当成装好了，后面采集中再莫名失败。
  if (result.status !== 'installed' && result.status !== 'up_to_date') {
    // 先落失败态（含校验摘要与重试建议），再抛出：调用方的错误提示照旧，
    // 但用户手里多了一组可以继续走下去的按钮，而不是停在红色报错上。
    recordAcliFailure(result, 'acli 安装失败')
    throw new Error(result.message || 'acli 安装失败')
  }
  await markAcliInstalled()
}

/**
 * 重试 acli 安装（兜底入口）。
 *
 * retryStage 直接决定跳过哪些已确认正确的工作：
 * - upload ：复用 bridge 内存缓存里的已校验安装包重传，不再拉官网；
 * - install：复用节点上已校验的安装包重装，不再传任何字节；
 * - all    ：完整重来（缓存过期、或怀疑缓存本身有问题时才用）。
 */
async function retryAcliSync(retryStage: AcliRetryStage) {
  if (acliRetrying.value) return
  if (!nodeIp.value) {
    ElMessage.warning(NODE_MISSING_MESSAGE)
    return
  }
  let ws: WebSocket
  try {
    ws = await openSocket()
  } catch (error: any) {
    showCustomerFault(error.message)
    return
  }
  acliRetrying.value = true
  const previousStage = stage.value
  stage.value = 'installing'
  try {
    const result = await sendAndWait(ws, 'acli_sync', { retryStage }, ACLI_SYNC_TIMEOUT_MS)
    if (result.status !== 'installed' && result.status !== 'up_to_date') {
      recordAcliFailure(result, 'acli 安装重试失败')
      showCustomerFault(result.message || 'acli 安装重试失败')
      return
    }
    await markAcliInstalled()
    ElMessage.success(
      retryStage === 'install' ? 'acli 已用节点上校验通过的安装包重装成功' : 'acli 安装包重传并校验通过',
    )
  } catch (error: any) {
    // 通道断开/超时同样要给出重试建议：这是最常见的可恢复情况。
    recordAcliFailure(
      { status: 'error', stage: 'upload', message: error?.message || 'acli 重试失败' },
      'acli 重试失败',
    )
    showCustomerFault(error?.message || 'acli 重试失败')
  } finally {
    acliRetrying.value = false
    // 失败前如果已经在别的阶段（如采集中），不能被一次重试拉回 installing。
    if (stage.value === 'installing') stage.value = previousStage === 'installing' ? 'idle' : previousStage
  }
}

/** 安装包字节数的人类可读形态；未量到时返回空串，避免把“没测到”显示成 0 字节。 */
function formatAcliBytes(bytes: number): string {
  if (!bytes || bytes <= 0) return ''
  const mb = bytes / (1024 * 1024)
  return mb >= 1 ? `${mb.toFixed(1)} MB（${bytes} 字节）` : `${bytes} 字节`
}

/** 兜底按钮文案：用“从哪一跳继续”而不是笼统的“重试”。 */
function acliRetryLabel(target: AcliRetryStage): string {
  switch (target) {
    case 'upload':
      return '重试上传（复用已校验包）'
    case 'install':
      return '重试安装（复用节点上已校验包）'
    default:
      return '从头重试（重新下载并校验）'
  }
}

/** 校验强度的用户可见表述：size_only 必须显式说明“只比了字节数”。 */
function acliIntegrityText(facts: AcliSyncFacts): string {
  switch (facts.integrityState) {
    case 'sha256':
      return '已通过 SHA-256 内容校验（本地与节点一致）'
    case 'md5':
      return '已通过 MD5 内容校验（节点无 sha256sum，降级为 MD5）'
    case 'size_only':
      return '节点无 sha256sum/md5sum，仅比对字节数（未做内容校验）'
    default:
      return '尚未完成落盘校验'
  }
}

/**
 * 本次任务的目标节点数：完成判据的基准。
 *
 * 不能用"集群在线节点数"——任务可以只选 1 台，而历史报告里的 total 是上一次 all（3 台），
 * 基准取错会让假完成照样通过（工单 Q2026101401683 的 19:55 就是 3 对 3 通过、实际只采 1 台）。
 */
function expectedHostCount(): number {
  const ips = currentTask.value?.ips ?? []
  const isAll = ips.length === 0 || ips.includes('all')
  if (isAll) return probeResult.value?.hosts?.length ?? 0
  return ips.length
}

/** 去掉终端 ANSI 转义：bridge 的原始输出会带 `\x1b[31mFATA\x1b[0m` 这类颜色码 */
function stripAnsi(text: string): string {
  // eslint-disable-next-line no-control-regex
  return text.replace(/\u001b\[[0-9;]*m/g, '')
}

/**
 * 把桥侧原始输出整理成可直接落库的一条原因。
 *
 * 桥侧 Message 常带两类噪音：终端颜色码，以及 `FATA[0000] execing command in
 * container` 这类 bridge 自身的包装行。它们对定位没有帮助，却会污染任务列表
 * 与管理端审计（2026-10-27 实测 error_message 里混着 ANSI 的 FATA 行）。
 * 只保留首行，避免把多行 acli 输出整段灌进任务表。
 */
function normalizeErrorMessage(text: string): string {
  const lines = stripAnsi(text || '')
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line && !/^FATA\[\d+\]\s+execing command in container/.test(line))
  return (lines[0] || '').slice(0, 500)
}

// 平台侧异常的规范错误码：createApiClient 拦截器会把结构化 problem 挂到 error.problem。
// 取不到就返回空串，由调用方回退到桥侧码/空值，绝不臆造一个码。
function platformErrorCode(error: any): string {
  const problem = error?.problem as { code?: unknown } | undefined
  return typeof problem?.code === 'string' ? problem.code : ''
}

/**
 * 当前节点报告原文（基线 / 指纹用）。
 *
 * 独立成函数：handleStart 里刚把 `progress` 置 null，TS 的控制流分析会把
 * `progress.value` 收窄成 never，直接在函数里读会报 TS2339。
 */
function readProgressHash(): string {
  return hashReport(progress.value?.raw ?? '')
}

/** 节点上正在跑其它工单的采集时，记录那个工单号：只用于提示，不参与状态机。 */
const foreignTicket = ref('')
/** 自动轮询连续失败计数：静默重试不能变成永久无声。 */
const syncFailureStreak = ref(0)

/** 探测历史：面板只显示最新一次，历史失败原因必须能回看。 */
const probeHistory = ref<ProbeHistoryItem[]>([])

async function loadProbeHistory() {
  try {
    const { items } = await logCollectApi.listProbes(props.caseId, 10)
    probeHistory.value = items || []
  } catch {
    // 历史只用于排障，读取失败不能影响当前探测与下发。
    probeHistory.value = []
  }
}

/** 历史条目标题：一眼看出"哪次、节点几个、日志服务器通不通"。 */
function probeHistoryTitle(item: ProbeHistoryItem): string {
  const extra = item.extra || {}
  if (item.event === 'log_collect.create') {
    return `下发任务（${String(extra.upload_mode ?? '路径未记录')}）`
  }
  const hosts = Number(extra.hosts_count ?? 0)
  const reachable = extra.log_server_reachable === true ? '可达' : '不可达'
  return `探测：节点 ${hosts} 个 / 日志服务器${reachable}`
}

/** 历史条目详情：完整失败原文（不截断），两类原因分行。 */
function probeHistoryDetail(item: ProbeHistoryItem): string {
  const extra = item.extra || {}
  const lines: string[] = []
  if (extra.catalog_note) lines.push(`节点清单：${String(extra.catalog_note)}`)
  if (extra.note) lines.push(`日志服务器：${String(extra.note)}`)
  if (!lines.length) {
    return item.event === 'log_collect.create' ? '下发参数已记录，无失败信息' : '本次探测无失败信息'
  }
  return lines.join('\n')
}

function probeHistoryTime(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN')
}

/**
 * 读一次节点任务快照。
 *
 * `force` 只给主流程（pollUntilCollected）用：它自己持有轮询权，
 * 需要绕过 `pollingBridgeProgress` 的互斥保护；其余入口一律走互斥。
 */
async function readNodeSnapshot(timeoutMs = 20000, force = false): Promise<ProgressSnapshot | null> {
  if (!sshConnected.value) return null
  if (pollingBridgeProgress && !force) return null
  // 通道要自己确保可用：对账会被「打开面板 / 自动刷新 / 手动刷新」从不同时机触发，
  // 若依赖调用方先开好连接，先到的那次对账会被静默丢弃——表现就是完成态任务
  // 打开面板后永远没有对账结论，补交付入口也就永远不出现。
  let ws = socket
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    try {
      ws = await openSocket()
    } catch {
      return null
    }
  }
  try {
    const result = await sendAndWait(
      ws,
      'progress',
      { ticketNo: props.caseId, execId: currentTask.value?.task_id },
      timeoutMs,
    )
    if (result.status === 'error') return null
    // 归属校验：acli 的采集任务是**节点级单任务**，同一节点可能正挂着别的工单。
    // 桥接原样回显请求里的 ticket_no，据此判断这份进度属于谁；不属于本单时
    // 返回 null（上层按 unknown 处理），绝不把别单进度渲染成本单状态。
    const ticket = String(result.ticket_no ?? '').trim()
    if (ticket && ticket !== props.caseId) {
      foreignTicket.value = ticket
      return null
    }
    foreignTicket.value = ''
    const snapshot = (result.log_data as ProgressSnapshot | null) ?? null
    // 打开面板对账 / 自动刷新 / 手动刷新 全部经过这里：统一记录压缩包大小，
    // 否则会出现"某条路径能看到大小、另一条路径看不到"的随机感。
    rememberObservedArchiveSize(snapshot)
    return snapshot
  } catch {
    return null
  }
}

/** 本次任务的目标主机列表（`all` 展开为探测到的在线节点）。 */
function targetHostList(): string[] {
  const ips = currentTask.value?.ips ?? []
  if (!ips.length || ips.includes('all')) return (probeResult.value?.hosts ?? []).map((host) => host.ip)
  return ips
}

/**
 * 把节点上报的「发送/上传」阶段映射到步骤条。
 *
 * 现场（Q2026101401683 / 23:22）：acli 在把包发给日志服务器时仍输出
 * 「收集任务进行中！总进度 85%」，并在这个百分比上停留 30 多秒；
 * 步骤条却一直停在「采集」，用户以为采集卡死，而节点其实是在上传。
 * `upload_state`（中文「发送中 / 成功 / 失败」，英文 `Sending / Success / Failed`）
 * 是节点自己给出的阶段信号，用它点亮「上传」。文案随节点语言变化，判定统一走
 * `classifyUploadState`，不要在组件里比对某一种语言的字面量。
 */
function applyNodeStage(snapshot: ProgressSnapshot) {
  if (stage.value !== 'collecting') return
  if (classifyUploadState(snapshot.upload_state) === 'uploading') stage.value = 'uploading'
}

/**
 * 记录一次采样到的压缩包字节数（只保留峰值，见 observedArchiveSize 的说明）。
 *
 * 所有拿到节点快照的入口都要走这里：打开面板对账、自动刷新、手动刷新、主流程轮询。
 * 漏掉任何一个入口，都会出现"某条路径下能看到大小、另一条路径下看不到"的随机感。
 */
function rememberObservedArchiveSize(snapshot: ProgressSnapshot | null) {
  const size = Number(snapshot?.archive_size ?? 0)
  if (!Number.isFinite(size) || size <= observedArchiveSize.value) return
  observedArchiveSize.value = size
}

/** 任务上持久化的基线（换端接管时从 detail 恢复）。 */
function baselineFromTask(): string {
  return String((currentTask.value?.detail ?? {})['baseline_report_hash'] ?? '')
}

/** 当前报告相对基线的刷新状态；`undefined` 表示没有基线可比。 */
function reportRefreshedOf(snapshot: ProgressSnapshot): boolean | undefined {
  const baseline = baselineReportHash.value || baselineFromTask()
  const current = hashReport(snapshot.raw ?? '')
  if (!baseline || !current) return undefined
  return current !== baseline
}

/**
 * 累积本次下发的正向证据（L1 采集起点 / L2 上传阶段）。
 *
 * 所有能看到节点快照的入口都必须经过这里：漏掉任何一个，判定层就会以为"从没跑过"，
 * 把真实完成一直挡在 L1 门外面（重演 Q2026101401683 的反向故障）。
 */
function noteNodeObservation(snapshot: ProgressSnapshot | null) {
  if (!snapshot) return
  const nodeState = snapshot.node_state ?? fallbackNodeState(snapshot)
  // 起点只在"还没跑完"时才算数：`completed` 不属于起点证据。
  if (nodeState === 'running' && (snapshot.percent ?? 0) < 100) sessionAdopted.value = true
  const phase = classifyUploadState(snapshot.upload_state)
  if (phase === 'uploading' || phase === 'failed') uploadPhaseSeen.value = true
}

/**
 * 本次下发是否已拿到起点证据。
 *
 * 除内存标记外，还认 `task.progress > 0`：平台只在 running 时才写进度，
 * 所以这个字段是**跨页面刷新**仍然有效的"曾经跑过"证据。
 */
function sessionAdoptedOf(): boolean {
  if (sessionAdopted.value) return true
  return (currentTask.value?.progress ?? 0) > 0
}

/** 同一份"见过上传"的跨会话证据：平台把 uploading 阶段写进过任务状态。 */
function uploadSeenOf(): boolean {
  if (uploadPhaseSeen.value) return true
  return currentTask.value?.status === 'uploading'
}

/** 任务年龄（毫秒）；拿不到 created_at 时返回 undefined（不做 L1 兜底、也不做时间否决）。 */
function taskAgeMsOf(): number | undefined {
  const created = currentTask.value?.created_at
  if (!created) return undefined
  const started = new Date(created).getTime()
  if (!Number.isFinite(started)) return undefined
  return Math.max(0, Date.now() - started)
}

/** 任务年龄（秒）；与 `taskAgeMsOf` 同源，供 L2 时间一致性否决使用。 */
function taskAgeSecondsOf(): number | undefined {
  const ageMs = taskAgeMsOf()
  return ageMs == null ? undefined : ageMs / 1000
}

/** 报告自称的总耗时（秒）；解析失败返回 undefined，判定层据此跳过时间否决。 */
function reportTotalSecondsOf(snapshot: ProgressSnapshot): number | undefined {
  const parsed = parseAcliTotalTime(snapshot.total_time)
  return parsed == null ? undefined : parsed
}

/**
 * 由节点快照推断交付证据（只对 P1 有意义）。
 *
 * P1 的上传发生在 acli 内部，平台看不到任何字节或分片，唯一可读的证据是
 * progress 里的 `upload_state` 文本（"发送成功"/"发送失败" 或 `Success`/`Failed`），
 * 语义由 `classifyUploadState` 归一化，与语言无关。
 * P2 一律返回 undefined：它的交付结果由数据面合并与代传写进 DB，
 * 用节点态去覆盖只会把"平台已交付"改成"未知"。
 */
function deliveryEvidenceOf(
  snapshot: ProgressSnapshot,
  task: LogCollectTask | null,
): DeliveryStatus | undefined {
  if (!task) return undefined
  const isP1 = task.upload_mode === 'acli_direct' || Boolean(task.remote_url)
  if (!isP1) return undefined
  const phase = classifyUploadState(snapshot.upload_state)
  if (phase === 'delivered') return 'delivered'
  if (phase === 'failed') return 'failed'
  return undefined
}

/**
 * 统一对账入口：打开面板 / 自动刷新 / 手动刷新 / 开始收集前守卫，全部走这里。
 *
 * 第一性原理：节点是唯一真相，平台只是投影。先判「报告归属」，
 * 归属确认后节点态直接映射平台态——不再要求完成态自证。
 */
async function syncNodeState(
  options: {
    applyPatches?: boolean
    silent?: boolean
    source?: 'open' | 'auto' | 'manual' | 'preCreate'
  } = {},
): Promise<SyncAction | null> {
  // 互斥：自动轮询与手动刷新可能同时落到这里，两次 progress 会互相顶掉 waiter
  if (syncingNode) return null
  syncingNode = true
  try {
    const snapshot = await readNodeSnapshot()
    if (!snapshot) {
      if (!options.silent) ElMessage.warning('SSH 会话不可用，无法读取节点任务状态')
      return null
    }

    const nodeState = snapshot.node_state ?? fallbackNodeState(snapshot)
    // 先记证据再判定：本次采样本身就是一条"节点跑到哪一步"的证据，
    // 顺序写反会让判定层永远比观测慢一拍，真实完成被 L1 门误挡。
    noteNodeObservation(snapshot)
    // 两个证据源必须分开传，不能在这里合并：
    //   - platformDelivered：**平台侧**已有交付记录（P2 代传成功 / 已登记 delivered）；
    //   - nodeDelivered：**本轮节点报告**里的 P1「发送成功」。
    // 合并成一个是错的——判定层要靠"平台没记录但节点说成功"这个组合来识别
    // "平台漏记、需要补登"（Q2026102701193），合并后这个组合永远不成立。
    const nodeDelivered = deliveryEvidenceOf(snapshot, currentTask.value) === 'delivered'
    const platformDelivered =
      currentTask.value?.delivery_status === 'delivered' || currentTask.value?.relay_status === 'success'
    // 落盘侧核验只在"P1 已完成但未登记交付"时做，不给每条对账都加一次 HTTP。
    // 结果作为唯一权威证据交给判定层；核验不可用返回 'none'，由有界等待兜底。
    const isP1Completed =
      (currentTask.value?.upload_mode === 'acli_direct' || Boolean(currentTask.value?.remote_url)) &&
      nodeState === 'completed' &&
      !platformDelivered
    const archiveVerified: ArchiveVerification = isP1Completed
      ? await verifyArchiveEvidence(
          currentTask.value!.task_id,
          observedArchiveSize.value || currentTask.value!.archive_size || null,
        )
      : 'none'
    const waitMs = isP1Completed ? deliveryWaitElapsed(currentTask.value) : 0
    let action = decideNodeSync({
      platformStatus: currentTask.value?.status ?? null,
      nodeState,
      archivePath: snapshot.archive_path,
      // 包是否存在只认 stat 结论：路径是"曾生成"，文件可能已被插件删除
      archiveExists: snapshot.archive_exists,
      nodeDelivered,
      progressTotal: snapshot.total,
      expectedHosts: expectedHostCount(),
      reportRefreshed: reportRefreshedOf(snapshot),
      // L1/L2：归属正向证据 + 时间一致性否决，两者都只依赖可自证的事实
      requireSessionAdopt: requireSessionAdopt.value,
      sessionAdopted: sessionAdoptedOf(),
      taskAgeMs: taskAgeMsOf(),
      reportTotalSeconds: reportTotalSecondsOf(snapshot),
      taskAgeSeconds: taskAgeSecondsOf(),
      uploadSeen: uploadSeenOf(),
      reportHosts: (snapshot.hosts ?? []).map((host) => host.ip),
      targetHosts: targetHostList(),
      platformDelivered,
      archiveVerified,
      deliveryWaitElapsedMs: waitMs,
    })
    // 节点自述"发送成功"但落盘侧明确否定：两边证据冲突时一律不写库，
    // 转成有界等待，等包/归档件状态稳定再收敛（避免 Q2026101201081 的假失败）。
    if (action.action === 'patch_delivered' && archiveVerified === 'failed') {
      action = {
        action: 'wait_delivery',
        reason: '日志服务器上暂未查到归档件，与节点报告不一致；正在等待上传/归档确认',
        ownership: action.ownership,
      }
    }
    logDeliveryDecision('sync_decision', {
      source: options.source ?? 'unknown',
      action: action.action,
      evidence: archiveVerified,
      node_delivered: nodeDelivered,
      archive_exists: snapshot.archive_exists ?? null,
      wait_ms: waitMs,
      wait_limit_ms: DELIVERY_EVIDENCE_WAIT_MS,
    })
    // 节点报告的包路径就是补交付的源：平台侧的 archive_path 在"从未交付"的任务上是空的。
    nodeArchivePath.value = snapshot.archive_path || ''

    // 旧报告不能拿来当展示数据：上一轮的「已完成 100%」会让面板与平台再次显示矛盾。
    // 只刷新时间戳（证明自动刷新在跑），进度沿用上一次归属确认过的快照。
    if (action.action !== 'wait_refresh') {
      progress.value = snapshot
      applyNodeStage(snapshot)
    }
    lastRefreshAt.value = new Date()

    // 只有需要用户决策时才亮按钮：接管（节点还在跑）、清理（节点异常/游离）
    nodeAdoptMode.value = action.action === 'adopt' ? action.adoptMode ?? 'resume' : 'resume'
    nodeAction.value =
      action.action === 'adopt'
        ? 'adopt'
        : action.action === 'cancel_retry' || action.action === 'warn_node_busy'
          ? 'cancel_retry'
          : action.action === 'warn_completed_conflict' && action.canCorrect
            ? 'correct'
            : ''
    // patch_delivered 是后台补登、不需要用户做任何事：留一条常驻提示只会占用固定区
    nodeSyncHint.value = action.action === 'patch_delivered' ? '' : buildSyncNotice(action)

    // syncPatchBlocked：收到过确定性拒绝（4xx）后不再自动写库。
    // 状态机拒绝不会因为再等 30 秒而变好，继续重试只会把同一条错误刷成日志噪音。
    if (options.applyPatches && currentTask.value && !syncPatchBlocked) {
      if (
        action.action === 'patch_completed' ||
        action.action === 'patch_failed' ||
        action.action === 'patch_delivered'
      ) {
        try {
          if (action.action === 'patch_delivered') {
            // 交付终态只能由服务端写：公共 PATCH 里的 delivery_status 会被服务端
            // 静默忽略（single-writer，Q2026110601054），所以这里不再 PATCH 交付字段。
            //   - verify-archive 刚回查通过 → 服务端已写库，拉回现状即可；
            //   - 核验不可用（票据未配置 / 旧后端）→ 走服务端自报入口，
            //     由服务端判定 upload_state 后标注 acli_self_reported 落库。
            // 判据只认"平台侧最终有没有这条记录"，绝不在这里先宣布已交付。
            const taskId = currentTask.value.task_id
            currentTask.value =
              archiveVerified === 'delivered'
                ? await logCollectApi.getTask(taskId)
                : await logCollectApi.attestP1Delivered(taskId, {
                    upload_state: snapshot.upload_state ?? null,
                    archive_size:
                      snapshot.archive_size ||
                      observedArchiveSize.value ||
                      currentTask.value.archive_size ||
                      null,
                  })
            // 不切视图、不改 stage：用户可能正看着结果页，
            // 只需让归档状态从「尚未交付」变成「已由插件直传」。
            stage.value = 'done'
            if (viewMode.value === 'result') showResult(currentTask.value)
          } else {
            const patch: LogCollectTaskPatch =
              action.action === 'patch_completed'
                ? {
                    // 事实与结论分离：节点只观测到「采集结束、包有多大、包在哪」，
                    // 平台按证据推导结论；前端不再提交 status/progress 当终态。
                    node_observed: {
                      status: 'completed',
                      progress: 100,
                      archive_path: snapshot.archive_path || currentTask.value.archive_path || undefined,
                      // 节点侧只在包还在时量得到；已有值优先保留，避免 P2 的记录被 0 覆盖。
                      // 兜底顺序里必须有 observedArchiveSize：判"完成"的那份快照必然量不到
                      // （P1 上传成功即删包），只用它的话大小永远写不进去。
                      archive_size:
                        snapshot.archive_size ||
                        observedArchiveSize.value ||
                        currentTask.value.archive_size ||
                        undefined,
                    },
                    // 交付路径只有两种，P1 创建时必带 remote_url；补进产物记录，
                    // 避免"已完成但 upload_mode 为空"在结果页显示成"未记录"。
                    // 交付状态不在这里写：有交付证据时判定层给的是 patch_delivered，
                    // 由服务端入口落库；再写一次只会被忽略并刷审计日志。
                    upload_mode:
                      currentTask.value.upload_mode ?? (currentTask.value.remote_url ? 'acli_direct' : undefined),
                  }
                : {
                    status: 'failed' as const,
                    error_message: normalizeErrorMessage(action.reason),
                    // 判定层给的码优先：节点从未接单时桥侧往往没有可用码，
                    // 只能靠 action.errorCode 让 error_code 不再恒为空。
                    error_code: action.errorCode || lastNodeErrorCode || undefined,
                  }
            // 归档核验已在判定前统一做过一次（archiveVerified），此处不再重复调用：
            // 核验明确否定时动作已被改写成 wait_delivery，根本不会走到写终态的分支。
            // 旧实现在这里把一次瞬时 archive_missing 直接写成
            // `delivery_status=failed + LC-TRANSFER-007`，而大包可能还在打包/上传途中
            // （Q2026101201081：16:17 判完成、16:18 包才落盘）——那是假失败。
            currentTask.value = await logCollectApi.patchTask(currentTask.value.task_id, patch)
            if (action.action === 'patch_completed') {
              stage.value = 'done'
              nodeSyncHint.value = buildSyncNotice(action)
              // 与「刷新页面重新进入」保持一致：完成即切结果视图。
              // 否则页面会停在收集表单上、右下角按钮还写着「开始收集」，
              // 用户以为没完成（现场 2026-10-26 23:23）。
              showResult(currentTask.value)
            } else {
              // 必须在改 stage 之前取 stepActive：一旦看到 failed 它会返回 failedStep 本身，
              // 顺序颠倒会让步骤条永远停在第一步「探测」，而本次失败发生在「采集」（对账在 collecting 阶段触发）。
              failedStep.value = stepActive.value
              stage.value = 'failed'
              errorMessage.value = action.reason
            }
          }
          // 已落终态：降频继续对账（而不是停摆）——failed 的节点任务可能仍在跑，
          // 停掉轮询会让「接管」按钮再也不出现。
          startAutoSync(TERMINAL_POLL_INTERVAL)
          if (!options.silent) ElMessage.success(buildSyncNotice(action))
        } catch (error: any) {
          const status = Number(error?.response?.status ?? 0)
          if (status >= 400 && status < 500) {
            // 确定性拒绝（典型是 409：非法状态转换）。停止自动写库，并把平台现状
            // 拉回来，让下一轮判定基于真实状态，而不是继续用过期投影打同一堵墙。
            syncPatchBlocked = true
            await logCollectApi
              .getTask(currentTask.value.task_id)
              .then((latest) => {
                currentTask.value = latest
              })
              .catch(() => undefined)
          }
          if (!options.silent) {
            ElMessage.warning(`平台状态同步失败：${error?.message || error}`)
            syncFailureStreak.value = 0
          } else {
            // 静默重试不能变成永久无声：连续 3 次仍失败说明链路真的断了，
            // 用户看到的只是过期投影（现场就出现过"卡在 85%"却无任何提示）。
            syncFailureStreak.value += 1
            if (syncFailureStreak.value >= 3) {
              syncFailureStreak.value = 0
              ElMessage.warning(
                `连续 3 次同步平台任务状态失败，界面可能不是最新：${error?.message || error}`,
              )
            }
          }
        }
      } else if (action.action === 'none' && nodeState === 'running') {
        // 平台进度只是投影，允许滞后；每 10% 落一次，避免 5s 一次的写放大
        const reported = Number.isFinite(snapshot.percent) ? Math.max(0, Math.min(100, snapshot.percent)) : 0
        const stored = currentTask.value.progress ?? 0
        if (reported >= 100 || reported - stored >= 10) {
          try {
            currentTask.value = await logCollectApi.patchTask(currentTask.value.task_id, { progress: reported })
          } catch {
            // 投影写失败不影响节点态展示：下一次刷新会再对齐
          }
        }
      }
    }
    // 交付证据有界等待：窗口内保持高频轮询尽快收敛，超时后降频并明确"结果未知"。
    // 不写库、不改 stage，换端接管时由同一纯函数重新判定，保证幂等。
    if (action.action === 'wait_delivery') {
      nodeSyncHint.value = buildSyncNotice(action)
      startAutoSync(POLL_INTERVAL)
      logDeliveryDecision('wait_delivery_polling', { wait_ms: waitMs, interval_ms: POLL_INTERVAL })
    } else if (action.action === 'delivery_unknown') {
      nodeSyncHint.value = buildSyncNotice(action)
      startAutoSync(TERMINAL_POLL_INTERVAL)
      logDeliveryDecision('delivery_unknown', { wait_ms: waitMs, interval_ms: TERMINAL_POLL_INTERVAL })
    }
    return action
  } finally {
    syncingNode = false
  }
}

/** 采集完成后的收尾：按任务实际的交付路径决定是等直传还是走中转 */
async function resumeTaskFlow(
  ws: WebSocket,
  task: LogCollectTask,
  token: number,
  baseline = '',
  // 「继续交付」接管：节点任务早已结束、报告定格，必须跳过"报告是否刷新"的归属判定，
  // 否则会判 stale → wait_refresh → 空转到 60 分钟超时再失败。
  adoptedDeliver = false,
) {
  // 主流程自己持有轮询权：先停掉接管态的自动刷新，避免两条链抢同一条 bridge 通道
  stopAutoSync()

  // 接管的是**别人/上一次会话**下发的任务，不是本次会话 create 的：
  // L1 门必须关掉，否则"关页面→重开→接管一个已跑完的任务"会因永远看不到 running
  // 而被永久卡在采集中（Q2026101401683 的回归点）。
  requireSessionAdopt.value = false

  // 平台已进入交付阶段（uploading）：作业在 bridge 里跑，接上轮询即可，
  // 绝不能再走一遍"拉包 + 上传"。这也是断线/关页面后重开面板的恢复入口。
  if (task.status === 'uploading') {
    const jobId = `dj-${task.task_id}`
    const probe = await sendAndWait(ws, 'delivery_status', { jobId }, 10000).catch(() => null)
    if (probe && probe.status === 'ok') {
      await waitDeliveryJob(ws, jobId, token)
      stage.value = 'relaying'
      await pollRelay(task.task_id, token)
      if (token !== runToken) return
      await markCompleted(task.task_id, {
        upload_mode: 'bridge_relay',
        archive_path: task.archive_path || undefined,
      })
      stage.value = 'done'
      return
    }
    // 作业不在：bridge 重启且记录丢失，或任务是在另一台机器发起。
    // 明确报错并给出可操作动作，避免界面永远停"上传中"。
    throw new Error('平台任务处于交付阶段，但 bridge 侧没有对应作业（可能已重启且记录丢失），请使用「取消并重试」')
  }

  const collected = await pollUntilCollected(ws, token, baseline)
  if (token !== runToken) return
  if (!collected) throw new Error('采集未完成')
  const isDirect = task.upload_mode === 'acli_direct' || Boolean(task.remote_url)
  if (!isDirect) {
    // 把「已量到的证据」一起交给 runP2：完成那份快照里的 archive_size 是同一份包的
    // 物理事实，能省一次探测往返；量为 0 时 runP2 自己会走 stat（现已带 ls -l 兜底）。
    // 路径必须用 collected 的，不能用 progress.value——那可能是上一轮的报告。
    await runP2(ws, task, token, {
      mode: 'collect',
      sourcePath: collected.archive_path || undefined,
      totalSize: collected.archive_size || undefined,
    })
    return
  }
  try {
    await finishP1(task, collected)
  } catch (error) {
    // P1 失败自动降级 P2：插件**只有直传成功才会自删压缩包**，
    // 因此失败时源包仍在节点上，可以无损切到中转续传。
    // 这是「P1 仍为默认（不占桥资源、少一次中转）」与「一定传得上去」之间的兜底。
    const recovered = await fallbackToP2(ws, task, token, error)
    if (!recovered) throw error
  }
}

/**
 * P1（acli 直传日志服务器）失败后切 P2（平台中转）。
 *
 * 判据必须用「节点上还有包」而不是「P1 报错」：
 * 插件成功后自删，此时切换只会得到"源包不存在"，必须原样抛出原错误。
 */
async function fallbackToP2(
  ws: WebSocket,
  task: LogCollectTask,
  token: number,
  cause: unknown,
): Promise<boolean> {
  const archivePath = progress.value?.archive_path || ''
  if (!archivePath) return false
  // 主失败原因的码必须先留底：下面这次探测只是"能不能降级"的附加判断，
  // 它失败时（最典型是包已被插件自删 → LC-TRANSFER-001）写入的码是**次生结论**，
  // 不能顶掉主因（P1 归档核验失败是服务端写的 LC-TRANSFER-007）。
  // 顶掉之后库里会出现"码说包不在、文案说核验未通过"的自相矛盾记录。
  const causeCode = lastNodeErrorCode
  // stat 探测不到时不能降级：失败可能是"设备已删包"（多半是"其实传成功了"），
  // 也可能是"命令不可用"，两者都不该把用户引向一次注定失败的搬运。
  // probeArchiveOnNode 已把三态收拢：只有确认在盘上才返回，探测失败直接抛错，此处按 false 处理。
  const probed = await probeArchiveOnNode(ws, archivePath).catch(() => null)
  if (!probed) {
    lastNodeErrorCode = causeCode
    return false
  }

  const reason = cause instanceof Error ? cause.message : String(cause)
  ElMessage.warning(`直传日志服务器失败（${reason}），已自动切换平台中转续传`)
  nodeSyncHint.value = '直传失败，已自动切换平台中转续传'
  stage.value = 'pulling'
  // 降级必须留痕：只写 upload_mode=bridge_relay 会让"用户本来选 P2"与
  // "P1 失败被迫改 P2"在库里长得一模一样，事后无法解释包为什么没走直传。
  // detail 由服务端合并写入，不会覆盖下发时锚定的报告指纹。
  currentTask.value = await logCollectApi.patchTask(task.task_id, {
    upload_mode: 'bridge_relay',
    detail: { upload_mode_switched_from: 'acli_direct', upload_mode_switched_reason: reason.slice(0, 500) },
  })
  await runP2(ws, task, token)
  return true
}

/**
 * 一键「接管」。
 *
 * 两种语义，前提完全不同，因此代码也分开：
 * - `resume`：节点任务还在跑 → 平台终态改回采集中，继续跟进进度；
 * - `deliver`：节点采集已完成、包还在 → **只做搬运**，不碰采集状态机。
 *
 * 为什么 deliver 不能再复用 resume 的那条路：改回 collecting 对 completed 任务会
 * 触发服务端"清空产物字段"（那是纠正语义），而且接下来要再轮询一遍已经定格的
 * progress；bridge 侧还会因为"包早于本次作业创建"被判成上一次任务的产物
 * （delivery_runner.go 的串味判定，超过 5 分钟必然误杀）。补交付的充分必要条件
 * 只有"包在节点上 + 平台有会话"，与采集状态没有因果关系。
 */
/**
 * 结果态「上传到平台」。
 *
 * 为什么要单独包一层：结果态入口按平台记录先给出（见 canUploadFromNode），此时
 * 节点对账可能还没回来，`nodeAdoptMode` 仍是默认的 'resume'——直接调
 * `adoptNodeTask` 会走"续采"分支，把「传已有包」变成「再采一遍」。
 */
async function deliverUploadFromNode() {
  nodeAdoptMode.value = 'deliver'
  await adoptNodeTask()
}

async function adoptNodeTask() {
  const task = currentTask.value ?? resultTask.value
  if (!task) return
  let ws: WebSocket
  try {
    ws = await openSocket()
  } catch (error: any) {
    showCustomerFault(error.message)
    return
  }
  const token = ++runToken
  // 用户显式接管：解除自动对账的写库熔断，按真实状态重新推进
  syncPatchBlocked = false
  nodeAction.value = ''
  nodeSyncHint.value = ''

  if (nodeAdoptMode.value === 'deliver') {
    stage.value = 'pulling'
    // 上一次失败的码不能带进本次重试：否则"第一次磁盘满、第二次网络断"
    // 会把旧码写到新一次失败上，用户照着磁盘指引去清盘，问题却在网络上。
    lastNodeErrorCode = ''
    ElMessage.success('正在经平台中转上传节点上已有的压缩包（不会重新采集）')
    try {
      await uploadExistingPackage(ws, task, token)
    } catch (error: any) {
      if (token !== runToken) return
      // 平台瞬时不可用不代表交付失败：后台代传可能仍在跑，此刻 patchTask 多半
      // 也打不通，写进去的只会是一条误导性原因（2026-11-06 Q2026110600940）。
      if (isOutcomeUnknownError(error)) {
        errorMessage.value = '平台暂时不可用，上传仍在后台继续；请稍后点「刷新进度」确认结果'
        stage.value = 'failed'
        ElMessage.warning(errorMessage.value)
        return
      }
      errorMessage.value = normalizeErrorMessage(String(error?.message || error))
      failedStep.value = stepActive.value
      stage.value = 'failed'
      // 此处失败补丁尚未落库，currentTask.error_code 可能是上一次的旧码，
      // 绝不拿它当本次权威码；交给 customerFaultText 按契约归类。
      showCustomerFault(errorMessage.value)
      // 交付失败不写 status=failed：采集完成是真实结论，抹掉它会让用户
      // 既看不到包在哪、也找不到重试入口。失败只落在 delivery_* 上。
      //
      // 这里**不写** delivery_error：它是代传终态的唯一权威字段，由后端 _finish 落库。
      // 前端一旦回写，就会用一句通用文案覆盖后端记录的真实原因（Q2026102801062：
      // 后端写了原因，被前端覆盖成「错误码见平台任务记录」，结果记录里反而查不到）。
      //
      // 桥侧失败的原因改走 error_code / error_message（PATCH 契约允许）。但必须先确认
      // 后端有没有记录：代传失败时 catch 拿到的 message 本身就是「[码] 原因」的转述，
      // 原样回写会让下次渲染再拼一次码，变成 "[LC-X] [LC-X] ..."。
      const fresh = await logCollectApi.getTask(task.task_id).catch(() => null)
      const alreadyRecorded = Boolean(String(fresh?.error_message ?? '').trim())
      // delivery_status 也不写：终态由服务端单一写入者维护，这里写 'failed' 会被静默忽略。
      // 失败信息走 error_code / error_message（PATCH 契约允许），界面按"未交付 + 可重试"渲染，
      // 这正是事实本身——平台确实没有收到字节。
      const failurePatch: Partial<LogCollectTask> = {}
      // 码与文案分开处理：码是结构化归因，每次终态失败都应以最新一次为准
      // （否则第一次的码会永久粘住，用户第二次遇到的磁盘满反而看不到）；
      // 只有 error_message 需要"平台已记录则不覆盖"的保护。
      const failureCode = lastNodeErrorCode || platformErrorCode(error) || undefined
      if (failureCode) failurePatch.error_code = failureCode
      if (!alreadyRecorded) {
        failurePatch.error_message = errorMessage.value.slice(0, 2000)
      }
      if (Object.keys(failurePatch).length > 0) {
        await logCollectApi.patchTask(task.task_id, failurePatch).catch(() => undefined)
      }
    }
    return
  }

  try {
    stage.value = 'collecting'
    currentTask.value = await logCollectApi.patchTask(task.task_id, { status: 'collecting' })
    ElMessage.success('已接管节点任务，继续跟进进度')
    await resumeTaskFlow(ws, currentTask.value ?? task, token, readProgressHash())
  } catch (error: any) {
    if (token !== runToken) return
    errorMessage.value = normalizeErrorMessage(String(error?.message || error))
    failedStep.value = stepActive.value
    stage.value = 'failed'
    showCustomerFault(errorMessage.value)
  }
}

/**
 * 「以节点为准纠正」：平台已归档，但完成判定不可信（典型是同 ticket 的历史报告）。
 *
 * 后端只放开 completed → collecting 这一条边；纠正会清空本条任务的产物记录
 * （原值写入 error_message 留审计），但不会删除节点或日志服务器上的压缩包。
 */
async function correctFromNode() {
  const task = currentTask.value
  if (!task) return
  try {
    await ElMessageBox.confirm(
      '将把本条任务改回「采集中」并清除它的产物记录（节点与日志服务器上的压缩包不会被删除）。' +
        '仅用于平台完成判定失真、节点任务仍在运行的情况。是否继续？',
      '以节点为准纠正',
      { type: 'warning', confirmButtonText: '纠正', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  let ws: WebSocket
  try {
    ws = await openSocket()
  } catch (error: any) {
    showCustomerFault(error.message)
    return
  }
  const token = ++runToken
  try {
    // 用户显式纠偏：解除自动对账的写库熔断
    syncPatchBlocked = false
    stage.value = 'collecting'
    currentTask.value = await logCollectApi.patchTask(task.task_id, { status: 'collecting' })
    resultTask.value = null
    viewMode.value = 'form'
    nodeAction.value = ''
    nodeSyncHint.value = ''
    ElMessage.success('已按节点状态改回采集中，继续跟进进度')
    await resumeTaskFlow(ws, currentTask.value ?? task, token, readProgressHash())
  } catch (error: any) {
    if (token !== runToken) return
    errorMessage.value = normalizeErrorMessage(String(error?.message || error))
    failedStep.value = stepActive.value
    stage.value = 'failed'
    showCustomerFault(errorMessage.value)
  }
}

/** 一键「取消并重试」：先清理节点任务，再按当前表单重新下发 */
async function cancelAndRetry() {
  const task = currentTask.value
  if (task) {
    const result = await cancelLogCollectTask({
      stopNode: async () => {
        if (!socket || socket.readyState !== WebSocket.OPEN) throw new Error('bridge 连接已断开')
        return await sendAndWait(socket, 'cancel', {}, 120000)
      },
      cancelPlatform: () => logCollectApi.cancelTask(task.task_id, '对账后清理重试'),
    })
    if (!result.stopped) {
      ElMessage.warning(`节点任务未确认终止（${result.error}），请稍后再试`)
      return
    }
  }
  nodeAction.value = ''
  nodeSyncHint.value = ''
  currentTask.value = null
  progress.value = null
  relayStatus.value = 'none'
  stage.value = 'idle'
  errorMessage.value = ''
  await handleStart()
}

/**
 * 收尾还原 spc 槽位。
 *
 * 为什么平台还要主动发一次：bridge 的内存态随进程消失（重启/被杀），
 * 那时后台恢复协程已经不存在，而"平台清单留在节点"只有备份名这一个可复原依据。
 * 这里把还原变成确定性收尾，而不是"希望后台协程还活着"。
 *
 * 幂等：节点上备份不存在时 bridge 返回 skipped，仍按 success 记。
 * 写库口径：只有拿到逐台结果才写 restore_status；通道异常一律保持 pending——
 * 把"结果未知"写成 failed 与 relay 误判（Q2026110601054）是同一类错误。
 */
async function restoreSpcSlot(ws: WebSocket, taskId: string): Promise<void> {
  if (ws.readyState !== WebSocket.OPEN) return
  try {
    const result = await sendAndWait(
      ws,
      'restore_spc',
      {
        ticketNo: props.caseId,
        execId: taskId,
        // 多槽位必须逐槽位还原；空数组时桥回退到内存态/单槽位兼容路径。
        spcSlots: currentTask.value?.spc_slots ?? [],
      },
      200000,
    )
    const data = (result.log_data ?? {}) as {
      restore_status?: string
      spc_hosts?: Array<{ ip: string; backup?: string; error?: string }>
    }
    const status = result.status === 'ok' ? String(data.restore_status || 'success') : ''
    if (!status) {
      console.warn('[log-collect] 场景槽位还原失败', result.message || result.error_code)
      return
    }
    spcPendingRestore.value = status !== 'success'
    if (data.spc_hosts?.length) spcHosts.value = data.spc_hosts
    await logCollectApi
      .patchTask(taskId, { restore_status: status, spc_hosts: spcHosts.value })
      .catch((error: any) =>
        console.warn('[log-collect] 还原结果落库失败', error?.message || error),
      )
    if (status !== 'success') {
      ElMessage.warning('场景清单未能完整还原，可在任务详情里重试还原')
    }
  } catch (error: any) {
    console.warn('[log-collect] 场景槽位还原请求异常', error?.message || error)
  }
}

async function pollUntilCollected(
  ws: WebSocket,
  token: number,
  baseline = '',
): Promise<ProgressSnapshot | null> {
  // 采集上限 1 小时（720 次 × 5s）：超时明确失败，避免面板无限转圈。
  pollingBridgeProgress = true
  // 基线缺失时退化为"首次看到的进行中报告"：之后出现不同的报告即可判定归属。
  let baselineHash = baseline
  try {
    for (let attempt = 0; attempt < 720; attempt++) {
      if (token !== runToken) return null
      // 通道守卫：WS 已关闭时继续轮询只会空等到 60s 超时，必须立刻失败。
      // 这里刻意**不看** sshSessionLost / sshConnected：shell EOF 后 bridge 保留会话，
      // 进度查询仍可用，此时终止轮询等于把能跑完的采集判死。
      if (ws.readyState !== WebSocket.OPEN) throw new Error(BRIDGE_CHANNEL_LOST)
      const result = await sendAndWait(ws, 'progress', {}, 60000)
      // bridge 明确报错（如会话已断开）必须立刻上抛：
      // 早先这里只读 log_data，错误结果会被当成"还没有进度"，面板空转一小时仍显示 0%。
      if (result.status === 'error') {
        // 与 create 失败同样处理：先把规范码留住，再由 catch 统一写库。
        lastNodeErrorCode = String(result.error_code || '')
        // 会话缺失必须说出真因与正确处置（重连后接管），只回显 bridge 的简短 message
        // 会把用户推向"重新采集"，而插件单任务互斥必然拒绝。
        throw new Error(
          lastNodeErrorCode === 'LC-ENV-006'
            ? SSH_LOST_MESSAGE
            : result.message || '节点侧进度查询失败',
        )
      }
      const snapshot = result.log_data as ProgressSnapshot | null
      if (snapshot) {
        // 主流程自己直连 sendAndWait，不走 readNodeSnapshot：这里单独记一次大小。
        rememberObservedArchiveSize(snapshot)
        // L1/L2 证据必须在这里累积：主流程是采集期间**唯一**的采样者，
        // 漏记就等于判定层永远看不到起点，真实完成会被挡在门外。
        noteNodeObservation(snapshot)
        // 与「刷新进度」共用同一个判定：归属 → 映射，不再另写一套完成逻辑。
        // 这里的平台态固定按"本次采集进行中"算——主流程本身就在 collecting 上跑，
        // 若读取 currentTask 的瞬时值，会在 patch 后把 completed 再判成"已归档"而空转。
        const action = decideNodeSync({
          platformStatus: 'collecting',
          nodeState: snapshot.node_state ?? fallbackNodeState(snapshot),
          archivePath: snapshot.archive_path,
          progressTotal: snapshot.total,
          expectedHosts: expectedHostCount(),
          reportRefreshed: baselineHash ? hashReport(snapshot.raw ?? '') !== baselineHash : undefined,
          // 主流程是采集期间唯一的采样者：不把"本次亲自下发"传进判定层，
          // create 接单宽限门永远不会生效，又会在下发后几秒把任务写成失败。
          requireSessionAdopt: requireSessionAdopt.value,
          sessionAdopted: sessionAdoptedOf(),
          taskAgeMs: taskAgeMsOf(),
          reportTotalSeconds: reportTotalSecondsOf(snapshot),
          taskAgeSeconds: taskAgeSecondsOf(),
          uploadSeen: uploadSeenOf(),
          reportHosts: (snapshot.hosts ?? []).map((host) => host.ip),
          targetHosts: targetHostList(),
        })
        // 旧报告同样不能当展示数据（否则会显示上一轮的 100%）
        if (action.action !== 'wait_refresh') {
          progress.value = snapshot
          applyNodeStage(snapshot)
        }
        lastRefreshAt.value = new Date()
        if (action.action === 'patch_completed') {
          nodeSyncHint.value = ''
          return snapshot
        }
        if (action.action === 'patch_failed') {
          // 判定层的规范码必须带回主流程：这里的失败以 throw 收尾，
          // 若不在抛出前留底，catch 里写库时 error_code 恒为空。
          if (action.errorCode) lastNodeErrorCode = action.errorCode
          throw new Error(action.reason)
        }
        if (action.action === 'wait_refresh') {
          // 仍是上一次任务的报告：不算失败、不计数，等本次任务的报告写出来
          nodeSyncHint.value = buildSyncNotice(action)
        } else {
          // 进行中：把当前报告记成基线（"首次进行中报告"退化路径）
          if (!baselineHash && snapshot.raw) baselineHash = hashReport(snapshot.raw)
          nodeSyncHint.value = ''
        }
      }
      await sleep(POLL_INTERVAL)
    }
    return null
  } finally {
    pollingBridgeProgress = false
  }
}

/**
 * 落盘侧归档核验：交付结论的唯一权威证据源。
 *
 * 三态返回，绝不能把"接口不可用 / 抛错"当成"核验通过"——旧后端与前端是两个独立发布物，
 * 用 try/catch 而不是 `.catch()`，因为方法根本不存在时同步 TypeError 不会进 Promise 链。
 */
async function verifyArchiveEvidence(taskId: string, size: number | null): Promise<ArchiveVerification> {
  try {
    const verdict = await logCollectApi.verifyP1Archive(taskId, size)
    if (verdict?.verified === true) return 'delivered'
    if (verdict?.verified === false) return 'failed'
  } catch {
    // 核验不可用：按"无结论"处理，交给有界等待
  }
  return 'none'
}

async function finishP1(task: LogCollectTask, snapshot: ProgressSnapshot | null) {
  // 必须用"通过完成判据的那一份快照"，不能回读 progress.value：
  // 否则上传状态/包路径可能来自另一次读取（早先就是拿旧报告的「成功」放行了假完成）。
  const uploadState = (snapshot?.upload_state || '').trim()
  // 语言无关分类（#1453）：中文「失败」与英文 `Failed` 同等对待。
  const uploadPhase = classifyUploadState(uploadState)
  if (uploadPhase === 'failed') {
    throw new Error(`日志服务器上传失败：${uploadState}，可改用 P2 中转`)
  }
  const archiveSize = snapshot?.archive_size || observedArchiveSize.value || task.archive_size || null
  const evidence = await verifyArchiveEvidence(task.task_id, archiveSize)
  const nodeSuccess = uploadPhase === 'delivered'
  // 交付结论只有两个来源，且都必须经服务端落库：
  //   - 落盘侧核验通过（最强，与节点态无关）→ verify-archive 内部已写 delivered；
  //   - 核验不可用（票据未配置 / 旧后端）且节点明确报「发送成功」→ 走服务端自报入口。
  // `upload_state` 为空既不证明成功也不证明失败：acli 某些版本根本不打印该字段，
  // 且完成报告可以早于打包/上传（Q2026101201081：16:17 报完成、16:18 包才落盘）。
  // 旧实现把它当成功写 delivered、把一次瞬时 archive_missing 当失败写 failed，两头都说谎。
  // L2：从未观测到上传过程、且压缩包已不在节点时，这句 Success 只是采集完成瞬间的信号。
  // 实测 acli 状态序列是 Success → Sending → Success：第一次 Success 时包还没开始传，
  // 直接写 delivered 会得到一条**零产物**的交付记录（5ad6842a：delivered 但 size=0）。
  // 只有"见过上传"或"包确实还在盘（可补交付）"两种情形，nodeSuccess 才可信。
  const successIsTrustworthy = uploadSeenOf() || snapshot?.archive_exists !== false
  const delivered =
    evidence === 'delivered' || (evidence === 'none' && nodeSuccess && successIsTrustworthy)
  const patch: Partial<LogCollectTask> = {
    upload_mode: 'acli_direct',
    // 交付状态不放进 PATCH：公共接口会静默忽略它（single-writer），
    // 写进去只会变成一条永远不会生效的"假成功"。
    archive_path: snapshot?.archive_path || undefined,
    // 大小来自 bridge 的 stat（插件 progress 不打印大小）。上传成功那一份快照里
    // 包已被插件自删、size 必然为 0，所以必须回退到过程中记录的峰值。
    archive_size: snapshot?.archive_size || observedArchiveSize.value || undefined,
  }
  // 弱证据必须先落到服务端，再由服务端把权威状态带回来；失败按"未登记"处理，
  // 不能在这里宣布已交付（P1 的包已被插件删除，说错就无法纠正）。
  let recorded = delivered
  if (delivered && evidence !== 'delivered') {
    try {
      await logCollectApi.attestP1Delivered(task.task_id, {
        upload_state: uploadState || null,
        archive_size: archiveSize,
      })
    } catch {
      recorded = false
    }
  }
  await markCompleted(task.task_id, patch)
  stage.value = 'done'
  if (recorded) {
    deliveryWaitStartedAt = 0
    cleanupTip.value = 'P1 由插件直传日志服务器；上传成功后插件会自动删除 HCI 上的压缩包，无需人工清理。'
    ElMessage.success('日志已归档到日志服务器')
    logDeliveryDecision('delivered', { evidence, upload_state: uploadState, wait_ms: 0 }, task)
  } else {
    // 采集结论照常落库，交付结论进入有界等待：不写 failed、不写 delivered。
    // 窗口从这一刻起算；关页面重开时由 deliveryWaitElapsed() 回退到任务 updated_at。
    // 注意 `delivered && !recorded` 这一支：节点说成功、平台却登记失败（服务端拒绝或不可达），
    // 此时既不能说"已归档"（平台没有记录），也不能让用户点重传（包已被插件删除），
    // 只能如实说"正在确认"，把人工核验入口留着。
    deliveryWaitStartedAt = Date.now()
    nodeSyncHint.value = delivered
      ? '采集已完成；节点报告已发送成功，但平台尚未登记交付记录，正在确认日志服务器归档件（最多等待 10 分钟）'
      : '采集已完成，正在确认日志服务器归档件（最多等待 10 分钟）；压缩包仍在节点时可直接点「上传到平台」经平台中转补交付'
    startAutoSync(POLL_INTERVAL)
    ElMessage.info(
      delivered ? '节点报告已发送成功，正在等待平台登记交付记录' : '采集已完成，正在确认日志服务器归档件',
    )
    logDeliveryDecision(delivered ? 'delivery_attest_unrecorded' : 'wait_delivery', {
      evidence,
      upload_state: uploadState,
      wait_ms: 0,
    }, task)
  }
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

/**
 * 探测节点上压缩包此刻的真实状态（交付链路上「包在不在」的唯一判据出口）。
 *
 * 为什么必须只留一个出口：``runP2`` 与「上传到平台」各判一次，判据一漂就分叉——
 * Q2026102900165（2026-10-29）现场就是这样：节点 aSV 受限 shell 里 stat 不存在（exit 127），
 * 包却真在盘上 29.9 MB，界面却断言「压缩包已不在节点上（P1 发送成功后插件会自动删除）」，
 * 把用户直接推向「重新采集」（而重采只会把旧包挪进 archive/ 目录）。
 *
 * 三态语义（bridge 已改为 stat → ls -l 两级探测）：
 * - true：确认在盘上；
 * - false：确认不在（只有探测命令真的跑通且明确报 missing 才会出现）；
 * - null：无法确认（命令不可用 / 通道异常 / 旧版 bridge 不下发该字段）。
 * 只有 false 才允许说「已不在」；null 必须如实说「无法确认」。
 */
async function probeArchiveOnNode(
  ws: WebSocket,
  archivePath: string,
): Promise<{ size: number; exists: boolean | null }> {
  const outcome = interpretArchiveProbe(
    await sendAndWait(ws, 'stat', { sourcePath: archivePath }, 30000),
  )
  if (outcome.failure) {
    // 探测失败绝不能翻译成「包已删除」：把桥的原话（含 stat:exit=127 这类诊断）抛出去，
    // 让用户知道该等一等、还是该去节点确认，而不是白重采一遍。
    if (outcome.failure.errorCode) lastNodeErrorCode = outcome.failure.errorCode
    throw new Error(outcome.failure.message)
  }
  return { size: outcome.size, exists: outcome.exists }
}

/**
 * 把任务推进到 uploading（仅用于"采集刚完成"的自动衔接）。
 *
 * 第一性原理：交付与采集是两件正交的事。`status` 记的是**采集结论**，交付是否
 * 能继续与它无关；`completed → uploading` 被状态机拒绝，只说明采集结论已经落库，
 * 不代表包不能搬。因此这里只吞掉"这一跳不被允许"（409 + LC-PLATFORM-003），
 * 其余错误（令牌、网络、5xx）照旧抛出——那才是真的传不上去。
 *
 * 现场依据（2026-11-09 Q2026110900356）：自动对账抢先写了 completed，本函数
 * 所在的主流程随后 PATCH 吃 409，异常一路抛到 handleStart，/deliver 已经开好的
 * 64 片会话一个字节都没传，bridge 连 delivery_submit 都没收到。
 */
async function advanceToUploading(taskId: string) {
  // 已经是终态就不必再试：状态机会拒绝，且采集结论不能被交付阶段改写。
  if (TERMINAL_TASK_STATUSES.includes(String(currentTask.value?.status ?? ''))) return
  try {
    currentTask.value = await logCollectApi.patchTask(taskId, { status: 'uploading' })
  } catch (error) {
    if (!isStatusTransitionRejected(error)) throw error
    // 409 只是"这一跳不被允许"，但**不代表可以继续**：cancelled → uploading 同样是
    // LC-PLATFORM-003。必须用服务端最新状态裁决，否则会把用户已取消的包传出去。
    const latest = await logCollectApi.getTask(taskId).catch(() => null)
    if (!latest || !canResumeDeliveryAfterReject(latest.status)) {
      throw new Error(
        `任务状态已变为「${latest?.status ?? '未知'}」，本次交付已中止；如需交付请重新采集后再试`,
      )
    }
    currentTask.value = latest
    nodeSyncHint.value = '采集已完成，正在继续把日志包上传到平台'
  }
}

/**
 * P2 中转交付。
 *
 * `mode` 决定 bridge 侧怎么起作业：
 * - `collect`：采集刚完成、由主流程衔接而来，允许 bridge 等采集写完再搬；
 * - `direct`：包早就采好了（补交付），直接读盘搬运，**不得**再等 progress。
 * 两种模式对应两种不同前提，不能合并成一个"大概已经采完了吧"。
 */
async function runP2(
  ws: WebSocket,
  task: LogCollectTask,
  token: number,
  options: { mode: 'collect' | 'direct'; sourcePath?: string; totalSize?: number } = { mode: 'collect' },
) {
  const archivePath = options.sourcePath || progress.value?.archive_path || task.archive_path || ''
  if (!archivePath) throw new Error('未获取到压缩包路径，无法中转')

  // 1) 取包大小（init 需要 total_size）。stat 顺带给出"文件此刻是否真的在"：
  //    补交付场景里包可能已被插件删除（P1 发送成功后自删），必须在这里说人话，
  //    而不是把 shell 的 exit 1 原样抛给用户（现场顶部弹出的 "Process exited with status 1"）。
  //    调用方已经量过时（补交付入口先探存在性）直接复用，省一次 30s 预算的往返。
  let totalSize = options.totalSize ?? 0
  if (totalSize <= 0) {
    totalSize = (await probeArchiveOnNode(ws, archivePath)).size
  }
  if (totalSize <= 0) throw new Error('压缩包大小异常（节点报告 0 字节），无法中转')

  // 会话几何在 init 时按 total_size 冻结，因此"包还在写"时建立会话必然失败。
  // 这里用节点已上报的态做零往返门禁：不额外发起 WS 请求，避免给补交付链路
  // 增加超时面。节点态缺失（老 bridge）时不拦，交给 bridge 的 waitArchiveSettled。
  const nodeState = String((progress.value as Record<string, unknown> | null)?.node_state ?? '')
  if (nodeState && nodeState !== 'completed' && nodeState !== 'done') {
    throw new Error(`采集尚未收尾（节点状态 ${nodeState}），请等日志包生成完毕再上传`)
  }

  // 2) 平台签发分片会话：走 /deliver 而非 /uploads。
  //    /deliver 由服务端负责"会话大小不一致"的自愈与会话记账；
  //    /uploads 会把 409 抛给前端，而前端从不 abort → 会话就此死锁（现场复现过）。
  const delivered = await logCollectApi.deliverTask(task.task_id, {
    total_size: totalSize,
    source_path: archivePath,
    // 内容指纹的可用近似：平台够不着节点、拿不到包摘要，只能登记
    // "这次提交认的是哪个路径上的多大一份"。真正的代际隔离由服务端
    // 单调递增的 delivery_epoch 承担（编进上传令牌，旧令牌自动失效）。
    archive_fingerprint: `${totalSize}:${archivePath}:${task.created_at || task.task_id}`,
  })
  if (delivered.already_delivered) {
    // 幂等短路：服务端没有再开会话。不要继续下发作业去"再传一次"。
    // delivery_status 不在这里写：服务端既已判定已交付，它自己就是权威（single-writer）。
    await markCompleted(task.task_id, {
      upload_mode: 'bridge_relay',
      archive_path: archivePath,
      archive_size: totalSize,
    })
    stage.value = 'done'
    ElMessage.success('日志包此前已交付，无需重复上传')
    return
  }

  // 3) 交付作业化：submit 立刻返回 job_id，执行权交给 bridge。
  //    这一步之后浏览器关掉也不影响交付；旧实现要在这里挂 2 小时等 transfer 回包。
  stage.value = 'pulling'
  // 只有"采集刚完成"的流程才把任务推进到 uploading：补交付时任务可能已是
  // completed（终态），PATCH uploading 会被状态机拒绝，且会抹掉采集结论。
  if (options.mode === 'collect') {
    await advanceToUploading(task.task_id)
  }
  await runDeliveryJob(
    ws,
    token,
    {
      task,
      sourcePath: archivePath,
      totalSize,
      session: delivered.upload,
    },
    options.mode,
  )

  // 5) 合并与代传由平台在数据面 complete 后自动触发（bridge 用上传令牌收尾），
  //    前端只等 relay 终态：这最后一步也不再依赖浏览器一直在线。
  stage.value = 'relaying'
  await pollRelay(task.task_id, token)
  if (token !== runToken) return

  // 归档成功才推进任务终态：否则任务会永久停在 uploading，并占住该工单的进行中名额。
  // delivery_status=delivered 由 relay 服务端在 _finish(success) 时一次性收敛
  // （status/completed + delivery_status/delivered + 清 error_*），前端再写一次只会被忽略。
  await markCompleted(task.task_id, {
    upload_mode: 'bridge_relay',
    archive_path: archivePath,
    archive_size: totalSize,
  })
  stage.value = 'done'
  // 同样不能无条件展示：archivePath 来自节点报告，但仍要过一遍白名单，
  // 否则某个异常桥版本回传相对路径/对象 key 时就会给用户一条错误路径。
  cleanupTip.value =
    `日志包已归档到日志服务器（${totalSize} 字节）。\n` +
    buildNodeCleanupTip(task.case_id, [archivePath])
  ElMessage.success('日志已上传平台并归档到日志服务器')
}

/**
 * 「上传到平台」：把节点上**已有的**压缩包经平台中转交付。
 *
 * 与 runP2 的唯一区别是前提：这里认定采集已经结束（节点报 completed、包在盘上），
 * 所以既不轮询 progress、也不把平台状态改回采集中，bridge 侧用 direct 模式跳过
 * waitCollect —— 那条路径里的"串味判定"会把早于本次作业创建的包判成上一次任务的
 * 产物，补交付场景下必然误杀（包本来就是过去采的）。
 */
async function uploadExistingPackage(ws: WebSocket, task: LogCollectTask, token: number) {
  const archivePath = nodeArchivePath.value || progress.value?.archive_path || task.archive_path || ''
  if (!archivePath) {
    throw new Error('节点上没有可交付的压缩包路径（包可能已被移除）；如需产物请重新采集')
  }

  // 先确认包真的还在，再动平台状态：否则 status 被改成 collecting 之后才发现传无可传，
  // 平台就被留在"采集中"的假态里，只能靠下一轮对账再纠回来。
  // 判据只认 probeArchiveOnNode 的三态结论：探测失败会直接抛出真因，不再冒充"包已不在"。
  //
  // "包是否已经写完"不在这里再判一次：前端多一次 stat 就多一次往返与超时面，
  // 且与 bridge 的判据必然分叉（Q2026102900165 的教训）。bridge 的 direct 路径
  // 有 EOCD 校验、collect 路径有 waitArchiveSettled，统一由它返回 LC-TRANSFER-010。
  const probe = await probeArchiveOnNode(ws, archivePath)

  // failed / cancelled → completed 在状态机里没有直连边，唯一合法出口是回到 collecting。
  // 这里走它只是为了"能落完成后态"，与重新采集无关：不发 create、不轮询 progress。
  if (task.status === 'failed' || task.status === 'cancelled') {
    currentTask.value = await logCollectApi.patchTask(task.task_id, { status: 'collecting' })
  }

  await runP2(ws, task, token, {
    mode: 'direct',
    sourcePath: archivePath,
    totalSize: probe.size,
  })
}

/** 交付作业视图（与 bridge `deliveryJobView` 对齐）。 */
interface DeliveryJobView {
  job_id?: string
  phase?: string
  progress?: number
  source_size?: number
  pulled_bytes?: number
  uploaded_parts?: number
  part_count?: number
  needs_reauth?: boolean
  last_error?: string
  /**
   * bridge 给出的结构化失败归因（如 LC-TRANSFER-008 客户机磁盘满）。
   *
   * 必须单独传：last_error 是给人看的整句中文，前端若靠文本匹配来分支，
   * 文案一改就失效；码才是稳定的契约。
   */
  error_code?: string
  /** 本地中转副本是否已被清理（交付成功后自动删除）。 */
  local_deleted?: boolean
}

/**
 * 轮询交付作业直到终态。
 *
 * 为什么不订阅：作业在 bridge 里跑，浏览器随时可能断；轮询无状态，
 * 重连后拿 job_id 再查一次就能继续显示，不需要服务端保存订阅关系。
 *
 * 时间预算必须与 bridge 的 `deliveryJobTimeout`（6 小时）对齐：以前前端 2 小时就
 * 放弃并回写 delivery_status=failed，而 bridge 侧作业还在传——于是"界面说失败、
 * 平台记 failed、包其实传完了"同时成立，比失败本身更难排查。
 */
const DELIVERY_POLL_FAST_MS = 2000
const DELIVERY_POLL_SLOW_MS = 10000
/** 前 2 分钟用短间隔拿快速反馈，之后降频，避免长作业把请求量放大 6 倍。 */
const DELIVERY_POLL_FAST_ROUNDS = 60
const DELIVERY_POLL_BUDGET_MS = 6 * 60 * 60 * 1000

async function waitDeliveryJob(ws: WebSocket, jobId: string, token: number) {
  const deadline = Date.now() + DELIVERY_POLL_BUDGET_MS
  let retryingNotified = false
  for (let attempt = 0; Date.now() < deadline; attempt++) {
    const result = await sendAndWait(ws, 'delivery_status', { jobId }, 10000)
    if (token !== runToken) return
    if (result.status !== 'ok') {
      // 作业丢失（bridge 重启且记录未落盘）：降级为失败，由用户重试
      throw new Error(result.message || '交付作业不存在')
    }
    const view = (result.log_data || {}) as DeliveryJobView
    if (view.phase === 'pulling') stage.value = 'pulling'
    if (view.phase === 'uploading' || view.phase === 'completing' || view.phase === 'retrying') {
      stage.value = 'uploading'
    }
    transfer.stage = String(view.phase || '')
    transfer.percent = view.progress || 0
    transfer.pulled = view.pulled_bytes || 0
    if (view.source_size && view.source_size > 0) transfer.totalSize = view.source_size
    transfer.uploadedParts = view.uploaded_parts || 0
    transfer.partCount = view.part_count || 0

    if (view.phase === 'done') return view
    if (view.needs_reauth) {
      throw new Error('交付需要重新认证：请重新连接 SSH 后继续（作业进度已保留）')
    }
    if (view.phase === 'retrying') {
      // retrying 是 bridge 的**中间态**（后台正在退避重排），不是结论。
      // 见到它就抛错会让"还能自愈的作业"被提前判死，并污染平台状态。
      if (!retryingNotified) {
        retryingNotified = true
        ElMessage.warning('链路抖动，bridge 正在自动重试（已传分片与进度均保留）')
      }
      await sleep(attempt < DELIVERY_POLL_FAST_ROUNDS ? DELIVERY_POLL_FAST_MS : DELIVERY_POLL_SLOW_MS)
      continue
    }
    if (isTerminalDeliveryFailure(view.phase)) {
      // 与节点侧失败同一套约定：先把规范码留住，再由 catch 统一写库。
      // 磁盘满/并发容量不足需要前端分支提示（LC-TRANSFER-008/009），
      // 只回显整句 last_error 会让平台记录里永远没有可统计的码。
      if (view.error_code) lastNodeErrorCode = String(view.error_code)
      throw new Error(view.last_error || '交付失败（作业进度已保留，可重试）')
    }
    await sleep(attempt < DELIVERY_POLL_FAST_ROUNDS ? DELIVERY_POLL_FAST_MS : DELIVERY_POLL_SLOW_MS)
  }
  throw new Error('交付超过 6 小时仍未完成，作业仍在 bridge 侧继续，可稍后刷新查看')
}

/**
 * 向用户确认"该平台地址使用自签证书"。
 *
 * 文案必须同时说清三件事：为什么会失败（自签 CA 不可能在客户机系统信任库里）、
 * 授权范围（仅本次浏览器会话 + 仅本任务）、代价（中间人可被冒充）。
 * 只说"是否信任证书"会让用户无从判断。
 */
async function confirmTrustPlatformCertificate(origin: string): Promise<boolean> {
  try {
    await ElMessageBox.confirm(
      `平台（${origin}）使用自签证书，客户机的系统根证书库不会预置这份 CA，` +
        '因此上传必然报 x509 证书不受信。\n' +
        '确认这是你正在使用的平台地址后，本次上传将跳过证书校验；授权仅在当前浏览器会话内有效，关闭页面即失效。',
      '平台证书不受信',
      {
        confirmButtonText: '确认并继续上传',
        cancelButtonText: '取消',
        type: 'warning',
      },
    )
    return true
  } catch {
    return false
  }
}

/**
 * 提交一次交付作业，返回 job_id。TLS 授权随作业下发，不写任何全局状态。
 *
 * 载荷由 `buildDeliverySubmitPayload` 构造并单测覆盖：漏传字段的代价是
 * 整场上传失败（如 `tls_insecure`），不能在组件里手写。
 */
async function submitDeliveryJob(
  ws: WebSocket,
  args: { task: LogCollectTask; sourcePath: string; totalSize: number; session: UploadInitResult },
  tlsInsecure: boolean,
  mode: 'collect' | 'direct',
): Promise<string> {
  const payload = buildDeliverySubmitPayload(
    {
      taskId: args.task.task_id,
      sourcePath: args.sourcePath,
      totalSize: args.totalSize,
      session: args.session,
      mode,
    },
    window.location.origin,
    tlsInsecure,
  )
  const submitted = await sendAndWait(ws, 'delivery_submit', { ...payload }, 30000)
  if (submitted.status !== 'ok') throw new Error(submitted.message || '交付作业提交失败')
  return String(
    (submitted.log_data as { job_id?: string } | null)?.job_id || `dj-${args.task.task_id}`,
  )
}

/**
 * 提交并等待交付作业，证书不受信时向用户确认一次后自动重试。
 *
 * 为什么先试严格模式、失败后再问：
 * 证书是否受信是**结果**不是预测——前端既拿不到 bridge 的信任链，也无法预判
 * 客户机是否装过该 CA（有的现场确实装过）。先按严格走，只有真的 x509 才打扰用户，
 * 这样"证书受信"的正常现场不会被多余弹窗拦一道。
 *
 * 为什么重试是安全的：job 进入终态后 `submit` 会替换并重新入队，已传分片从平台
 * `/uploads` 重新读回，因此是**续传**而不是从头再来。
 */
async function runDeliveryJob(
  ws: WebSocket,
  token: number,
  args: { task: LogCollectTask; sourcePath: string; totalSize: number; session: UploadInitResult },
  mode: 'collect' | 'direct',
): Promise<void> {
  const origin = window.location.origin
  let tlsInsecure = platformCertTrusted(origin)

  let certRetries = 0
  let sessionRebuilds = 0
  // 上限同时覆盖：证书授权重试 1 次 + 会话重建 2 次 + 常规裕量。
  for (let attempt = 0; attempt < 8; attempt += 1) {
    const jobId = await submitDeliveryJob(ws, args, tlsInsecure, mode)
    try {
      await waitDeliveryJob(ws, jobId, token)
      return
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err)

      // 源包在提交后被重新采集覆盖：旧会话的分片几何已作废，继续传只会每片失败。
      // 第一性原理给出的不是"让用户手动重点上传"，而是系统自己重新量包、
      // 重新签发会话并重提作业。服务端 /deliver 会推进代际并自动 abort 旧会话，
      // 旧令牌因代际签名不匹配而失效，不会把两代字节混进同一个成品。
      if (isSessionDiverged(message) && sessionRebuilds < 2) {
        sessionRebuilds += 1
        const fresh = await probeArchiveOnNode(ws, args.sourcePath)
        const nextSize = fresh.size > 0 ? fresh.size : args.totalSize
        const rebuilt = await logCollectApi.deliverTask(args.task.task_id, {
          total_size: nextSize,
          source_path: args.sourcePath,
          force: true,
        })
        if (rebuilt.already_delivered) return // 服务端已确认交付，不需要重传
        if (!rebuilt.upload?.upload_token) {
          throw new Error('重建上传会话失败：服务端未签发上传令牌')
        }
        args.totalSize = nextSize
        args.session = rebuilt.upload
        stage.value = 'pulling'
        ElMessage.warning('源包内容已变化，已自动按新基线重建上传会话并续跑')
        continue
      }

      // 只在"首次 + 尚未授权 + 确属证书问题"时打扰用户，其余错误原样抛出
      if (certRetries > 0 || tlsInsecure || !isCertificateUntrusted(message)) throw err
      certRetries += 1
      if (!(await confirmTrustPlatformCertificate(origin))) throw err
      rememberPlatformCertTrust(origin)
      tlsInsecure = true
      // 以平台记录为准刷新已传分片：失败可能发生在第 N 片，续传不能靠旧快照
      args.session = await logCollectApi.getUploadStatus(args.task.task_id)
      // 硬校验：刷新出来的会话必须带令牌才能重提。缺了它 bridge 会回
      // "task_id 与 upload_token 必填"，把证书问题伪装成参数问题——2026-10-27
      // 现场就是在这条重试路径上反复返工，而首次提交一切正常。
      if (!args.session?.upload_token) {
        throw new Error('上传会话令牌缺失或已过期，请重新发起上传')
      }
      stage.value = 'uploading'
    }
  }
}

/** 归档由服务端后台任务驱动，前端轮询超时只代表"结果未知"，不代表任务失败。 */
class RelayPendingError extends Error {}

/**
 * 平台瞬时不可用时允许静默重试的次数（× POLL_INTERVAL=5s，约 1 分钟）。
 *
 * 为什么要静默而不是立刻报错：代传是**服务端后台任务**，前端这一次轮询失败与
 * 归档本身没有因果关系。case-service 被 OOMKilled 后通常 1~2 秒就恢复，
 * 为此打断一次 9.59 GiB 的归档并把任务写成 failed，代价远大于多等两秒
 * （2026-11-06 Q2026110600940 正是这样把已交付的任务判成了失败）。
 *
 * 为什么仍要有上限：平台持续不可用超过 1 分钟时，继续静默会让用户误以为
 * "还在传"；此时落「结果未知」，把判断权交回给人，且**不替服务端写终态**。
 */
const TRANSIENT_RETRY_LIMIT = 12

/** 取一次归档快照；平台瞬时不可用时返回 null（由调用方继续轮询）。 */
async function pollRelayOnce(taskId: string): Promise<RelaySnapshot | null> {
  try {
    return await logCollectApi.getRelay(taskId)
  } catch (error) {
    if (!isTransientPlatformError(error)) throw error
    return null
  }
}

async function pollRelay(taskId: string, token: number) {
  // 上限 2 小时（1440 次 × 5s）：代传是长任务，但不能无限轮询
  let leaseRecoveryAttempted = false
  let transientFailures = 0
  for (let attempt = 0; attempt < 1440; attempt++) {
    if (token !== runToken) return
    const snapshot = await pollRelayOnce(taskId)
    if (!snapshot) {
      transientFailures += 1
      if (transientFailures >= TRANSIENT_RETRY_LIMIT) {
        throw new RelayPendingError(
          '平台暂时不可用，归档仍在后台继续；请稍后点「刷新进度」确认结果',
        )
      }
      await sleep(POLL_INTERVAL)
      continue
    }
    transientFailures = 0
    relayStatus.value = snapshot.relay_status
    relayProgress.value = snapshot
    if (snapshot.relay_status === 'success') return
    if (snapshot.relay_status === 'failed') {
      // 原因必须从任务记录读，不能就地拼一句通用文案。
      // Q2026102801062：界面写「错误码见平台任务记录」，而记录里 error_code /
      // error_message 恰恰是空 —— 用户被指去一个没有答案的地方。
      // 代传终态的权威原因由后端 _finish 落库，前端只负责转述；
      // getTask 失败时按"平台未记录"如实说，绝不编造原因。
      const detail = await logCollectApi.getTask(taskId).catch(() => null)
      throw new Error(formatFailureReason(detail, '归档到日志服务器失败，平台未记录原因（可在桥日志中按工单号检索）'))
    }
    if (shouldRecoverRelayLease(snapshot, leaseRecoveryAttempted)) {
      // 发布/重启把执行代传的进程杀掉了，状态会永久停在 running：
      // 强制续跑只重放搬运（upload_id 与已传分片都已落库），不重传数据；
      // 抢占仍由服务端裁决，别的标签页/新实例已经接管时 started=false 而已。
      leaseRecoveryAttempted = true
      await logCollectApi.startRelay(taskId, true).catch(() => undefined)
      continue
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
      '将同时终止平台任务与 HCI 上的采集任务；已生成的压缩包不会自动清理。',
      '确认取消',
      { type: 'warning' },
    )
  } catch {
    // 用户点了"返回"，保持现状
    return
  }
  const task = currentTask.value
  // 先让后台轮询退出，再改状态，避免轮询回写覆盖
  runToken++

  const result = await cancelLogCollectTask({
    // 必须走 sendAndWait：它会给消息注入 nodeIp，而会话键是 caseID@nodeIP。
    // 历史实现直接 socket.send({...}) 漏传 nodeIp，bridge 只能找到"键为 caseID"的会话，
    // 于是 cancel 恒报 session_missing——平台显示已取消，节点那侧其实还在采集。
    stopNode: async () => {
      if (!socket || socket.readyState !== WebSocket.OPEN) throw new Error('bridge 连接已断开')
      return await sendAndWait(socket, 'cancel', {}, 120000)
    },
    cancelPlatform: () => logCollectApi.cancelTask(task.task_id, '用户取消'),
  })

  progress.value = null
  relayStatus.value = 'none'
  stage.value = 'idle'

  const notice = buildCancelNotice(result)
  if (notice.level === 'success') {
    ElMessage.success(notice.text)
  } else {
    ElMessage.warning(notice.text)
  }
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
    if (!open) {
      // 收起面板（v-show，不销毁）时停止自动刷新与事实复查，避免不可见还在后台发 SSH 命令
      stopAutoSync()
      stopFactRecheck()
      return
    }
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
    // 每次打开都重探：面板关闭时 App.vue 的 v-if 仍成立（组件不卸载），
    // 若沿用 'ready' 就跳过探测，界面会一直显示关闭前那份快照——
    // acli 在此期间被自动升级（1.0.0 → 1.0.1）就会造成"平台显示旧版本，终端手敲是新版本"。
    // 卡死自愈已下沉到 prepare 内部（按耗时判定），这里不再用状态挡重入：
    // 用状态挡会把「上一次探测卡死」变成「永远不再探测」，只能 F5 复活。
    probeRecheckCount.value = 0
    void prepare({ silent: true })
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
    // 上一单的 acli 失败态与校验摘要必须一起清掉：带着 A 单的 sha256 去展示
    // B 单的安装失败，只会把排障引到错误的包上。
    acliFailure.value = null
    resetAcliLive()
    // 节点包路径属于上一单的节点视图，必须一起清掉，否则补交付可能拿着 A 单的路径去 stat
    nodeArchivePath.value = ''
    nodeAction.value = ''
    nodeSyncHint.value = ''
    // 复查计数属于上一单：不复位会把 B 单的第一次复查直接吞掉
    stopFactRecheck()
    probeRecheckCount.value = 0
    if (visible.value) void restoreTaskState(true)
  },
)

// 会话主机变了（重新登录到另一台节点），上一轮的探测结论（目录、节点清单、P1/P2 判定）
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
      // 换了节点 = 换了一个环境：上一台的复查计数没有意义
      stopFactRecheck()
      probeRecheckCount.value = 0
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
  // 准备阶段的令牌与定时器同样要失效，否则重探/复查会在卸载后回写状态
  prepareToken++
  stopFactRecheck()
  if (nodeIpTimer) {
    clearTimeout(nodeIpTimer)
    nodeIpTimer = null
  }
  stopAutoSync()
  waiters.clear()
  socket?.close()
  socket = null
})
</script>

<template>
  <!--
    左栏「日志收集」页签的内容。
    外壳（标题、收起、拖拽宽度、折叠后的图标入口）统一由 PanelShell 提供，
    这里不再自带抽屉，避免与 el-drawer 的遮罩/焦点/位移动画语义互相打架。
  -->
  <div v-show="visible" class="lc-panel lc-body">
    <!--
      两段式：内容区（.lc-scroll）自己滚，底条（.lc-composer）是固定的 flex 尾项。

      为什么不能继续用 position:sticky 把底条钉在滚动容器底部：sticky 元素钉住时
      只是被"平移"上去，并没有腾出原位置的流动空间，所以只要不在最顶部滚到底，
      它就会盖住此刻位于它下方的那条内容——实测表现为「节点任务进行中」这类提示条
      下半截被底条吃掉（提示文案换行时更明显，因为提示条更高、被吃掉的比例更大）。
      内容与底条同处一个滚动层，就必然互相抢占像素；分成两层才没有抢占关系。
    -->
    <!--
      固定提示区：这里只放两类提示——「不处置就没法继续」（归属不符、环境准备失败），
      和「必须让用户看到的结论」（节点对账、清理提醒）。

      它们原先散在滚动内容里，滚到两端会被视口边缘裁掉一截，移出滚动层是为了让它们
      始终完整可见。但请注意：移出滚动层只解决"边界裁切"，不解决"提示条被压扁"——
      后者（现场第三次反馈的真实成因）见样式区 `.lc-scroll > *` 的说明，
      根因是 flex 收缩不是遮挡，所以留在滚动层里的引导类 info 提示同样需要那个守卫。
      高度上限 45% 是兜底：提示再多也不至于把内容区挤没；该区同样是 flex 列 + 可滚动，
      子项一样必须禁止收缩，否则提示会在这个"新家"里被压成一条缝。
    -->
    <div v-if="hasPinnedAlerts" class="lc-alerts">
      <!-- 跨身份复用他人工单：采集不阻断，但日志回传/录像回放必定被归属校验拒绝 -->
      <el-alert
        v-if="chatStore.logCollectCaseOwned === false"
        type="warning"
        :closable="false"
        show-icon
        title="该工单属于其他会话身份"
      >
        <div class="lc-claim">
          <p class="lc-claim-text">
            采集可以继续，但日志回传与终端录像回放会被平台按归属拒绝。如果这是你自己上一段会话（换浏览器、清
            Cookie、换机器）留下的工单，认领到当前身份即可继续上传；否则请换用本人名下的工单号。
          </p>
          <div class="lc-claim-actions">
            <!--
              只提示不给出路时，用户唯一的选择是重新采集——而重采会覆盖节点上
              那份还没交付的包（Q2026111301229，454MB）。认领是显式、可审计的动作。
            -->
            <el-button
              v-if="chatStore.logCollectCaseClaimable"
              size="small"
              type="primary"
              :loading="chatStore.logCollectClaiming"
              @click="claimOwnership"
            >
              认领并继续
            </el-button>
            <span v-else-if="claimBlockedText" class="lc-claim-blocked">{{ claimBlockedText }}</span>
          </div>
        </div>
      </el-alert>
      <!-- 准备失败的原样输出：acli 的 FATA 行就是用户唯一能拿去求助的证据，必须完整可见 -->
      <el-alert
        v-if="prepareState === 'failed'"
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
      <!--
        acli 安装失败兜底：必须同时给出“为什么失败”和“下一步做什么”。
        历史实现失败后只弹一句错误就不再管，用户唯一的办法是重头点“开始收集”（
        又一次 20~30MB 下载 + 上传），这恰恰是 2026-10-29 现场反复上传的原因之一。
      -->
      <el-alert v-if="acliFailure" class="lc-acli-fail" type="error" :closable="false" show-icon>
        <template #title>
          <span>acli 安装失败：{{ acliFailure.message }}</span>
        </template>
        <div class="lc-acli-facts">
          <span v-if="acliFailure.facts.errorCode" class="lc-acli-chip">
            错误码 {{ acliFailure.facts.errorCode }}
          </span>
          <span v-if="acliFailure.stage" class="lc-acli-chip">失败环节 {{ acliFailure.stage }}</span>
          <span v-if="acliFailure.facts.attempts" class="lc-acli-chip">
            已尝试 {{ acliFailure.facts.attempts }} 次
          </span>
          <span v-if="acliFailure.facts.packageFilename" class="lc-acli-chip">
            {{ acliFailure.facts.packageFilename }}
          </span>
          <span v-if="formatAcliBytes(acliFailure.facts.packageSize)" class="lc-acli-chip">
            {{ formatAcliBytes(acliFailure.facts.packageSize) }}
          </span>
          <span class="lc-acli-chip">{{ acliIntegrityText(acliFailure.facts) }}</span>
          <span v-if="acliFailure.facts.installExitCode" class="lc-acli-chip">
            安装退出码 {{ acliFailure.facts.installExitCode }}
          </span>
        </div>
        <!-- 摘要必须可见：事后核对“到底传上去的是哪个包”只能靠这两行 -->
        <div v-if="acliFailure.facts.packageSha256 || acliFailure.facts.packageMd5" class="lc-acli-digests">
          <div v-if="acliFailure.facts.packageSha256">
            本地 sha256：{{ acliFailure.facts.packageSha256 }}
          </div>
          <div v-if="acliFailure.facts.packageMd5">本地 md5：{{ acliFailure.facts.packageMd5 }}</div>
          <div v-if="acliFailure.facts.remoteDigest">
            节点 {{ acliFailure.facts.remoteAlgo || 'digest' }}：{{ acliFailure.facts.remoteDigest }}
          </div>
        </div>
        <pre v-if="acliFailure.facts.installOutput || acliLive.installOutput" class="lc-acli-output">{{
          stripAnsi(acliFailure.facts.installOutput || acliLive.installOutput)
        }}</pre>
        <div v-if="acliFailure.facts.packagePath" class="lc-acli-path">
          节点路径：{{ acliFailure.facts.packagePath }}
        </div>
        <div class="lc-sync-actions">
          <el-button
            v-for="option in acliFailure.suggestions"
            :key="option"
            size="small"
            type="primary"
            :plain="option === 'all'"
            :loading="acliRetrying"
            :disabled="acliRetrying || running"
            @click="retryAcliSync(option)"
          >
            {{ acliRetryLabel(option) }}
          </el-button>
          <el-button v-if="!acliFailure.suggestions.length" size="small" text @click="acliFailure = null">
            知道了
          </el-button>
        </div>
      </el-alert>
      <!--
        两端对账结论：节点是唯一真相源，平台只是投影。
        不一致时这里给出唯一出口——接管（节点还在跑）或取消并重试（节点异常），
        用户不必再去 SSH 敲 acli log_collect cancel。
      -->
      <el-alert
        v-if="nodeSyncHint && !(stage === 'failed' && !nodeAction)"
        :title="nodeSyncHint"
        :type="nodeAction ? 'warning' : 'info'"
        :closable="false"
        show-icon
        class="lc-sync"
      >
        <div v-if="nodeAction" class="lc-sync-actions">
          <el-button v-if="nodeAction === 'adopt'" size="small" type="primary" @click="adoptNodeTask">
            {{ nodeAdoptMode === 'deliver' ? '上传到平台' : '接管继续' }}
          </el-button>
          <el-button v-if="nodeAction === 'cancel_retry'" size="small" type="danger" plain @click="cancelAndRetry">
            取消并重试
          </el-button>
          <el-button v-if="nodeAction === 'correct'" size="small" type="warning" plain @click="correctFromNode">
            以节点为准纠正
          </el-button>
        </div>
      </el-alert>
      <el-alert
        v-if="cleanupTip"
        :title="cleanupTip"
        type="warning"
        :closable="false"
        show-icon
        class="lc-cleanup"
      />
      <!-- 结果态同样有 P1 直传的清理说明：只要它是"结论"，就一并留在可见区 -->
      <el-alert
        v-if="viewMode === 'result' && resultCleanupTip"
        :title="resultCleanupTip"
        type="warning"
        :closable="false"
        show-icon
        class="lc-cleanup"
      />
    </div>

    <div class="lc-scroll">
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
        <span class="lc-spacer"></span>
        <span v-if="viewMode === 'form' && lastRefreshText" class="lc-refresh-time">
          {{ lastRefreshText }}
        </span>
        <el-button
          v-if="viewMode === 'form'"
          text
          size="small"
          :loading="refreshingProgress"
          @click="refreshProgress()"
        >
          刷新进度
        </el-button>
        <!--
          重新探测必须是常驻入口：探测结论是快照，而环境会被外部改变
          （acli 在终端被装、节点上下线、日志服务器权限放开）。
          旧实现只把入口埋在探测表下方，现场找不到，只能 F5 重建组件。

          disabled 不能直接写 running：running 包含 'probing'，而探测本身就在设 probing——
          那样一旦探测卡死，按钮也永远是禁用的，恰好把唯一的自愈入口堵死。
          这里只挡「真的在采集/安装」的阶段。
        -->
        <el-button
          v-if="viewMode === 'form'"
          text
          size="small"
          type="primary"
          :loading="prepareState === 'probing'"
          :disabled="!nodeIpReady || (running && prepareState !== 'probing')"
          @click="prepare()"
        >
          重新探测
        </el-button>
      </div>

      <!--
        结果态优先：已完成是平台上的既成事实，先把结论摆出来；
        重新收集只是可选项，不是默认动作。
      -->
      <div v-if="viewMode === 'result' && resultTask" class="lc-result">
        <el-alert
          :type="resultHeadline.type"
          :closable="false"
          show-icon
          :title="resultHeadline.title"
          :description="resultHeadline.description"
        />
        <!-- 一眼定界：三段五组件 + 首个异常点 + S1-S8 阶段条，与管理端同源 -->
        <LogCollectFaultCard :display="resultTask.display" />
        <!-- 观察事实单独成行：多值并存，不能被折叠进一条横幅描述里。 -->
        <div v-if="resultTask.display?.conditions?.length" class="lc-evidence">
          观察事实：
          <el-tag
            v-for="item in resultTask.display.conditions"
            :key="item.key"
            size="small"
            type="warning"
            effect="plain"
          >{{ item.label }}</el-tag>
        </div>
        <el-descriptions :column="descColumns" border size="small">
          <el-descriptions-item label="发起节点">{{ resultTask.origin_node_ip }}</el-descriptions-item>
          <el-descriptions-item label="交付路径">{{ resultDeliveryText }}</el-descriptions-item>
          <el-descriptions-item label="收集场景">{{ resultModulesText }}</el-descriptions-item>
          <el-descriptions-item label="时间范围">{{ resultRangeText }}</el-descriptions-item>
          <el-descriptions-item label="目标主机">{{ resultHostsText }}</el-descriptions-item>
          <el-descriptions-item label="压缩包大小">{{ resultSizeText }}</el-descriptions-item>
          <!-- 产物位置在左、归档状态在右：长路径靠左读，短文案靠右收口，行高由路径决定 -->
          <el-descriptions-item label="产物位置">
            <span :title="resultArtifactText">{{ resultArtifactText }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="归档状态">{{ resultArchiveText }}</el-descriptions-item>
          <el-descriptions-item label="完整性">{{ resultIntegrityText }}</el-descriptions-item>
          <!--
            spc 场景的第三类事实：采集成功 ≠ 节点已收拾干净。core.json 被平台清单覆写过，
            没还原回来就没人知道下一次采集读的是谁的定义，所以必须单独一行并可重试。
          -->
          <el-descriptions-item v-if="spcRestoreCard" label="场景清单" :span="descColumns">
            <div class="lc-spc-restore">
              <el-tag :type="spcRestoreCard.tone" size="small">{{ spcRestoreCard.label }}</el-tag>
              <span class="lc-hint">{{ spcRestoreCard.tip }}</span>
              <el-button
                v-if="spcRestoreCard.canRetry"
                size="small"
                :loading="spcRetrying"
                @click="retrySpcRestore"
              >
                重试还原
              </el-button>
            </div>
          </el-descriptions-item>
          <!--
            failed / cancelled 才出现：失败原因是采集结论的一部分，不是可选调试信息。
            顶部提示条说"失败原因见下方错误信息"，就必须真的有这一行。
          -->
          <el-descriptions-item
            v-if="resultTask.status === 'failed' || resultTask.status === 'cancelled'"
            label="失败原因"
            :span="descColumns"
          >
            <span class="lc-error-text">{{ resultFailureText }}</span>
          </el-descriptions-item>
        </el-descriptions>
        <!-- 结果态同样要有「怎么查、怎么处理」：隔天回来查看的场景主要走这里 -->
        <LogCollectDiagnosis
          v-if="showFailureDiagnosis"
          :code="failureCode"
          :reason="failureReasonText"
        />
        <p class="lc-hint">
          当前客户端在本工单下共 {{ historyTotal }} 条收集记录；已完成的任务不会被覆盖，重新收集会新建一条任务。
        </p>
        <!--
          必须能切换查看：同一工单经常有多条记录，用户要能确认"面板说的是哪一次"。
          只展示一条且不给入口时，展示错了任务会被直接读成"系统判错了交付路径"
          （Q2026102701193：用户选 P2 建的任务失败，面板却显示早上那条 P1 的结果）。
        -->
        <div v-if="historyTasks.length > 1" class="lc-history">
          <span class="lc-hint">展示记录：</span>
          <el-button
            v-for="item in historyTasks"
            :key="item.task_id"
            size="small"
            :type="item.task_id === resultTask.task_id ? 'primary' : 'default'"
            @click="switchDisplayTask(item.task_id)"
          >
            {{ historyLabel(item) }}
          </el-button>
        </div>
      </div>

      <template v-else>
        <el-descriptions v-if="probeResult" :column="descColumns" border size="small" class="lc-probe">
          <el-descriptions-item label="架构">{{ probeResult.architecture }}</el-descriptions-item>
          <el-descriptions-item label="acli">
            <span :title="acliTitle">{{ acliVersionText }}</span>
          </el-descriptions-item>
          <el-descriptions-item v-if="deliveryModeFact" label="建议路径">
            <!-- 取值定死两个：P1 直传日志服务器 / P2 经平台中转（不加修饰词） -->
            <el-tag :type="deliveryModeFact.type" size="small">
              {{ deliveryModeFact.label }}
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

        <!-- 探测结论是快照：acli 可能被自动升级，面板关闭重开或点上方「重新探测」都会重新读一次 -->
        <div v-if="probeResult" class="lc-probe-meta">
          <span v-if="probedAtText">探测于 {{ probedAtText }}</span>
          <span v-if="probeResult.acli_binary" class="lc-probe-binary" :title="probeResult.acli_binary">
            {{ probeResult.acli_binary }}
          </span>
        </div>

        <!--
          进度状态紧跟集群信息表格：采集一开跑，用户第一眼要看的就是"到哪一步了/为什么失败"，
          原先放在表单最底部，抽屉内容一长就要往下滚才能看到，等于看不到。
          注意不要挪进上面 probeResult 的 v-if 里：探测失败（probeResult 为空）时任务仍可能存在，
          状态块必须照常显示，否则失败原因会整块消失。
        -->
        <div v-if="running || stage === 'done' || stage === 'failed'" class="lc-progress">
          <el-steps
            :active="stepActive"
            finish-status="success"
            :process-status="stage === 'failed' ? 'error' : 'process'"
            simple
          >
            <el-step title="探测" />
            <el-step title="采集" />
            <el-step title="上传" />
            <el-step title="归档" />
          </el-steps>
          <el-progress :percentage="percent" :status="progressStatus" />
          <div class="lc-status">{{ statusText }}</div>
          <div v-if="platformDisplay" class="lc-evidence">
            <div>
              {{ UI_TEXT.labels.platform_conclusion }}：{{ platformDisplay.conclusion_label }} · {{ platformDisplay.phase_label }} ·
              {{ UI_TEXT.labels.synthetic_progress }} {{ platformDisplay.progress_percent ?? UI_TEXT.fallback.unknown }}<span v-if="platformDisplay.progress_percent !== null">%</span>
            </div>
            <div v-if="platformTransfer" class="lc-hint">
              {{ platformTransfer.stage_label ?? UI_TEXT.fallback.unknown }} · 速率 {{ platformTransfer.rate_text ?? UI_TEXT.fallback.unknown }} ·
              预计完成 {{ formatEtaText(platformTransfer.eta_sec) }}
              <span v-if="platformTransfer.stalled"> · 传输已停滞 {{ platformTransfer.stalled_seconds ?? 0 }} 秒</span>
            </div>
            <div v-if="platformNetwork" class="lc-hint">
              网络质量：{{ platformNetwork.grade_label }} · 相关目标 {{ platformNetwork.relevant_target ?? UI_TEXT.mode_text.unknown }}
              <span v-for="item in platformNetwork.targets" :key="item.target">
                ｜{{ item.target }} {{ item.grade_label }}；上行 {{ item.up_text ?? UI_TEXT.fallback.unknown }}；下行 {{ item.down_text ?? UI_TEXT.fallback.unknown }}
                <span v-if="item.advice">（{{ item.advice }}）</span>
              </span>
            </div>
            <!-- 无数据提示按判据合并成一条，不再把「没数据」直接说成「桥接端版本较旧」。 -->
            <div v-if="evidenceHint" class="lc-hint">{{ evidenceHint }}</div>
            <div v-if="visibleConditions.length" class="lc-hint">
              {{ UI_TEXT.labels.observed_facts }}：{{ visibleConditions.map(item => item.label).join('、') }}
            </div>
          </div>
        </div>

        <!--
          失败诊断：用户必须一眼看出「哪一步、什么原因、怎么确认、怎么处理」。
          只给一句"日志收集失败"会把人推回"再点一次"的碰运气循环（2026-11-12 现场）。
        -->
        <LogCollectDiagnosis
          v-if="showFailureDiagnosis"
          :code="failureCode"
          :reason="failureReasonText"
        />

        <!--
          接管态说明：下面的表单此时展示的是「平台记录的下发参数」，不是待填草稿。
          不加这句话，用户会以为表单被"清空"了——实际是从来没回填过。
        -->
        <el-alert
          v-if="resumedTaskId && running"
          class="lc-prepare"
          type="info"
          :closable="false"
          show-icon
          :title="`以下参数来自平台任务记录（发起节点 ${currentTask?.origin_node_ip ?? '—'}）：任务进行中不可修改，结束后可「重新收集」`"
        />

        <!--
          准备状态的唯一出口：用户在填表之前就能看见"能不能收、可选什么"。
          文案必须点名面板内就能完成的动作（底部「连接 SSH」），不再把用户指去右侧终端。
        -->
        <el-alert
          v-if="!nodeIpReady"
          class="lc-prepare"
          type="info"
          :closable="false"
          show-icon
          :title="
            sshConnected
              ? '已连接 SSH，但读不到会话主机：请点击「连接 SSH」重新登录以确认发起节点'
              : '尚未连接 HCI 节点：点击底部「连接 SSH」登录后即可读取场景目录与节点清单'
          "
        />
        <el-alert
          v-else-if="prepareState === 'probing' && !probeRetry"
          class="lc-prepare"
          type="info"
          :closable="false"
          show-icon
          title="正在读取集群环境：架构 / acli / 场景目录 / 节点清单…"
        />
        <!-- 重试必须可见：用户要能分清「没反应」与「正在重试」，这正是现场误判的根源 -->
        <el-alert
          v-if="probeRetry"
          class="lc-prepare"
          type="warning"
          :closable="false"
          show-icon
          :title="probeRetryText"
        />
        <!-- 探测降级只在这里出现：影响用户下一步动作的只有「读不到节点清单」 -->
        <el-alert
          v-for="issue in probeIssues"
          :key="issue.key"
          class="lc-prepare"
          type="warning"
          :closable="false"
          show-icon
          :title="issue.title"
        >
          <div class="lc-probe-detail">{{ issue.detail }}</div>
        </el-alert>
        <el-alert
          v-if="foreignTicket"
          class="lc-prepare"
          type="warning"
          :closable="false"
          show-icon
          :title="`节点上存在其它工单的采集任务（${foreignTicket}），本单不采用该进度`"
        />
        <!-- 探测历史：只显示最新一次会让"上次为什么失败"永久丢失 -->
        <el-collapse v-if="probeHistory.length" class="lc-probe-history">
          <el-collapse-item :title="`探测历史（最近 ${probeHistory.length} 条）`" name="probe">
            <div
              v-for="(item, index) in probeHistory"
              :key="`${item.created_at}-${index}`"
              class="lc-probe-history-item"
            >
              <div class="lc-probe-history-head">
                {{ probeHistoryTime(item.created_at) }} · {{ probeHistoryTitle(item) }}
              </div>
              <div class="lc-probe-detail">{{ probeHistoryDetail(item) }}</div>
            </div>
          </el-collapse-item>
        </el-collapse>
        <!-- 探测期间只锁"提交"，不锁表单：用户可以先看清可填项，不必等探测结束 -->
        <el-form
          :model="form"
          :label-position="formLabelPosition"
          label-width="90px"
          :disabled="running"
        >
          <el-form-item label="故障场景">
            <el-radio-group v-model="form.scenarioMode" class="lc-scenario-mode" :disabled="running">
              <el-radio-button value="default">默认场景</el-radio-button>
              <el-radio-button value="custom" :disabled="!customScenarios.length">
                自定义场景（含基础日志）
              </el-radio-button>
            </el-radio-group>
            <template v-if="form.scenarioMode === 'default'">
            <el-checkbox-group v-model="form.modules" class="lc-checkbox-group lc-module-group">
              <el-checkbox
                v-for="item in moduleOptions"
                :key="item.value"
                :value="item.value"
                :disabled="moduleDisabled(item.value)"
                border
                size="small"
                class="lc-module-item"
              >
                <span class="lc-module-name">{{ item.label }}</span>
                <span v-if="item.summary" class="lc-module-summary">{{ item.summary }}</span>
              </el-checkbox>
            </el-checkbox-group>
            <div v-if="hasModuleConflict" class="lc-hint lc-hint-warn">
              当前选择同时包含「不收集上述场景」与其他专项（沿历史任务回填），建议只保留其中一类
            </div>
            <!-- 空选会发生什么是反馈，不是说明：必须保留，否则用户不知道默认会收什么 -->
            <div v-if="emptyModulesHint" class="lc-hint">{{ emptyModulesHint }}</div>
            <div v-if="selectedScenarioDocs.length" class="lc-scenario-docs">
              <div v-for="item in selectedScenarioDocs" :key="item.value" class="lc-scenario-doc">
                <div class="lc-scenario-doc-head">
                  <span class="lc-scenario-doc-name">{{ item.label }}</span>
                  <span v-if="item.durationHint" class="lc-scenario-doc-duration">
                    {{ item.durationHint }}
                  </span>
                </div>
                <div v-if="item.collects" class="lc-scenario-doc-line">采集：{{ item.collects }}</div>
                <div v-if="item.typicalSymptoms" class="lc-scenario-doc-line">
                  典型症状：{{ item.typicalSymptoms }}
                </div>
              </div>
            </div>
            <el-collapse v-if="selectedManifestPreviews.length" class="lc-manifest-preview">
              <el-collapse-item title="本次各场景实际收集的日志 / 配置文件">
                <div v-for="manifest in selectedManifestPreviews" :key="manifest.module" class="lc-manifest-block">
                  <div class="lc-manifest-head">
                    {{ manifest.label }}（{{ manifest.module }}）
                    <span class="lc-manifest-badge">
                      {{ manifest.customized ? `已自定义 v${manifest.revision}` : '平台默认快照' }}
                    </span>
                    <span>{{ manifest.item_count }} 条</span>
                  </div>
                  <div v-for="(entry, index) in manifest.preview.slice(0, 8)" :key="index" class="lc-manifest-path">
                    {{ entry.path }}
                  </div>
                </div>
              </el-collapse-item>
            </el-collapse>
            </template>
            <template v-else>
              <div v-if="!customScenarios.length" class="lc-hint">
                管理台尚未定义自定义场景，请切回默认场景
              </div>
              <el-radio-group v-model="form.customScenarioId" class="lc-custom-scenarios">
                <el-radio-button
                  v-for="item in customScenarios"
                  :key="item.scenario_id"
                  :value="item.scenario_id"
                  class="lc-custom-scenario"
                >
                  <span class="lc-custom-name">{{ item.label }}</span>
                  <span class="lc-custom-meta">{{ item.item_count }} 条路径 · v{{ item.revision }}</span>
                </el-radio-button>
              </el-radio-group>
              <div v-if="selectedCustomScenario?.summary" class="lc-hint">
                {{ selectedCustomScenario.summary }}
              </div>
            </template>
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
            <div class="lc-hint">
              日志按天归档，将收集所选日期全天日志（单次最多 30 天，超出会在下发前拦下）
            </div>
          </el-form-item>
          <el-form-item label="交付路径">
            <!--
              路径不是偏好，是由可达性决定的物理选择：
              可达时默认 P1（用户可主动改 P2），不可达时 P1 直接置灰——
              把一个注定失败的选项留着，只会骗用户先用一次失败去做验证。
            -->
            <el-radio-group v-model="pathMode" :disabled="running" @change="pathModeTouched = true">
              <el-radio-button value="P1" :disabled="p1Disabled">P1 直传日志服务器</el-radio-button>
              <el-radio-button value="P2">P2 经平台中转</el-radio-button>
            </el-radio-group>
            <div class="lc-hint">
              {{ pathModeHint }}
            </div>
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
            <el-input v-else v-model="form.ips" placeholder="all，或英文逗号分隔的节点 IP" />
            <div class="lc-hint">
              {{ hostOptions.length ? '不勾选任何主机 = 全部在线节点；勾选全部同样等价于 all' : manualHostHint }}
            </div>
          </el-form-item>
          <!--
            额外收集路径（acli -p）刻意折叠：
            它是「默认范围之外还要补什么」，属于补充手段；展开放在主流程上，
            会让不需要它的用户多做一个"要不要填"的决策。
          -->
          <el-collapse v-model="advancedOpen" class="lc-advanced">
            <el-collapse-item name="extra-paths">
              <template #title>
                <span class="lc-advanced-title">高级选项：额外收集路径</span>
                <span v-if="form.extraPaths.length" class="lc-advanced-count">
                  已添加 {{ form.extraPaths.length }} 条
                </span>
              </template>
              <div class="lc-extra-paths">
                <el-tag
                  v-for="path in form.extraPaths"
                  :key="path"
                  closable
                  type="info"
                  class="lc-extra-path-tag"
                  :disable-transitions="true"
                  @close="removeExtraPath(path)"
                >
                  {{ path }}
                </el-tag>
                <el-input
                  v-model="extraPathInput"
                  size="small"
                  class="lc-extra-path-input"
                  placeholder="绝对路径，回车添加；可一次粘贴多条（逗号或换行分隔）"
                  :disabled="running || extraPathsLimitReached"
                  @keyup.enter="addExtraPaths"
                  @blur="addExtraPaths"
                />
              </div>
              <div class="lc-hint">
                {{ extraPathsHint }}；必须是绝对路径，且不含空格、逗号与 ..；
                收集完成后会与专项日志一起打包
              </div>
              <div v-if="extraPathError" class="lc-hint lc-hint-warn">{{ extraPathError }}</div>
            </el-collapse-item>
          </el-collapse>
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
      </template>
    </div>

    <!--
      底部操作区：与中央会话、SSH 终端同构（hint 行 + 36px 控件行 + 按钮组），
      规范在 styles/panel-composer.css。这里没有输入控件，控件行留给状态摘要。
    -->
    <div class="panel-composer lc-composer">
      <div class="panel-composer__hint">
        <template v-if="viewMode === 'result'">
          上次采集 {{ resultFinishedText }} · 本工单共 {{ historyTotal }} 条记录
          <!-- 对账还没回来时按钮先不出现，但必须说明"在等什么"，否则就是干等 -->
          <span v-if="nodeVerifyPending"> · 正在与节点核对包状态…</span>
        </template>
        <template v-else-if="!canCollect">
          采集依赖 SSH 会话：点「连接 SSH」登录后即可开始收集
        </template>
        <template v-else-if="lastRefreshText">{{ lastRefreshText }}</template>
        <template v-else>选择时间范围与采集模块后开始收集</template>
      </div>
      <div class="panel-composer__body">
        <div class="panel-composer__actions">
          <template v-if="viewMode === 'result'">
            <!--
              采集已完成但产物没到日志服务器时，「重新收集」是唯一的旧出口——
              而包就在节点上，重采一遍是纯粹的浪费。这里把补交付提为主操作。
            -->
            <el-button v-if="canUploadFromNode" type="primary" @click="deliverUploadFromNode">
              上传到平台
            </el-button>
            <!--
              P1 的"未交付"只代表"这次回查没查到"，不代表真的没传上去：
              给用户一个重跑核验的入口，避免只能用「重新收集」去洗掉一条误判记录。
            -->
            <el-button
              v-if="canReverifyArchive"
              :loading="reverifyingArchive"
              @click="reverifyArchive"
            >
              重新核验归档
            </el-button>
            <el-button :type="canUploadFromNode ? 'default' : 'primary'" @click="startRecollect">
              重新收集
            </el-button>
          </template>
          <template v-else>
            <!--
              未连接时按钮**不禁用**，文案变成可执行的「连接 SSH」并弹出统一登录框。
              禁用 + "请先连接 SSH" 是把前置条件写成了状态：用户被要求做一件事却没有入口，
              只能自己发现右侧终端里还有同名按钮（2026-11-02 现场反馈）。
              仅在「已连接但正在探测」时禁用：此刻再点没有第二条路可走。
            -->
            <el-button
              v-if="!running"
              type="primary"
              :disabled="canCollect && prepareState === 'probing'"
              :loading="sshConnectOpening"
              @click="handlePrimaryAction"
            >
              {{
                !canCollect
                  ? '连接 SSH'
                  : prepareState === 'probing'
                    ? '正在读取环境…'
                    : '开始收集'
              }}
            </el-button>
            <el-button v-else type="danger" plain @click="handleCancel">取消</el-button>
          </template>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
/*
 * 两段式布局：根容器不滚动，只负责「内容区 + 底条」两行。
 * padding 与 gap 下沉到 .lc-scroll：底条要通栏贴边，根容器就不能有内边距。
 */
.lc-panel {
  display: flex;
  flex-direction: column;
  /* 面板高度由 PanelShell 的 body 决定；滚动交给 .lc-scroll，根容器裁掉溢出 */
  overflow: hidden;
  height: 100%;
  box-sizing: border-box;
}

/*
 * 固定提示区：与 .lc-scroll 平级，同样不参与滚动。
 * 提示文案多长，它就长多高；换个说法是"提示永远完整，代价是内容区少几行"，
 * 这个取舍对报错是对的——内容少几行可以滚，报错少几行就没有第二份了。
 * 45% 上限是最后一道兜底：提示条再多也不能把内容区挤成一条缝。
 */
.lc-alerts {
  flex: 0 0 auto;
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px 16px 0;
  box-sizing: border-box;
  max-height: 45%;
  overflow-y: auto;
}

/* 唯一滚动容器：内容再长也只在这里滚，底条不参与滚动，两者不会互相覆盖 */
.lc-scroll {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding: 12px 16px;
  box-sizing: border-box;
}

.lc-spacer {
  flex: 1;
}

/*
 * 滚动容器的子项一律禁止被压缩——这是"提示条只剩半行字"的真正病灶。
 *
 * 机制（不靠猜，可复算）：.lc-scroll / .lc-alerts 都是 flex 列 + overflow-y:auto，
 * 内容超出时产生负剩余空间，按 flex 规则必须由子项的 flex-shrink 消化。而
 * el-form、.lc-progress 等子项 overflow:visible，`min-height:auto` 解析为内容高度，
 * 一分钱都压不动；el-alert 自带 `overflow:hidden`，自动最小尺寸直接退化为 0，
 * 于是全部负空间都灌到提示条上：两行 64px 被压成 16px，再叠加 .el-alert 的
 * `align-items:center`，上下各裁一半，两行文字就叠成现场的"一条半行"。
 *
 * 换句话说：提示条不是被谁盖住，是被自己压扁后裁掉的。盒子整齐堆叠、永不重叠，
 * 所以过去用"overlap=0"验收必然通过——指标错了，不是改得不够。
 * 职责划分：滚动容器负责溢出，子项负责完整显示，两者不能混。
 *
 * 验收：scripts/verify/verify_log_collect_alert_layout.py（真浏览器量
 * clientHeight >= scrollHeight）；vitest 里的同名单测只是防退化门铃。
 */
.lc-scroll > *,
.lc-alerts > * {
  flex-shrink: 0;
}

/*
 * 底部操作区：作为 flex 尾项占据真实高度——内容不足时靠 margin-top:auto 顶到底部，
 * 内容溢出时它仍钉在可视区底部（因为内容区自己滚，它本来就没动过）。
 * 不再需要 sticky / 负 bottom / z-index：那些都是为了在"同一滚动层"里假装贴底，
 * 代价就是必然压住一条内容（见模板里那段说明）。
 */
.lc-composer {
  margin-top: auto;
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
/* 对账提示与一键动作：与进度条同区，避免用户去别处找入口 */
.lc-sync {
  margin-top: 8px;
}
.lc-sync-actions {
  display: flex;
  gap: 8px;
  margin-top: 6px;
}
/* acli 安装失败兜底：事实用小胶囊平铺，摘要单行可复制，输出限高可滚 */
.lc-acli-facts {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: 6px 0;
}
.lc-acli-chip {
  padding: 1px 8px;
  border-radius: 10px;
  background: rgba(0, 0, 0, 0.05);
  font-size: 12px;
  line-height: 20px;
  word-break: break-all;
}
.lc-acli-digests {
  margin: 4px 0;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  line-height: 18px;
  word-break: break-all;
}
/* 安装输出往往带进度条与 ANSI：限高 + 可滚，保证结论行不被刷掉也不拖垮布局 */
.lc-acli-output {
  max-height: 160px;
  overflow: auto;
  margin: 6px 0 0;
  padding: 8px;
  border-radius: 4px;
  background: rgba(0, 0, 0, 0.04);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  line-height: 16px;
  white-space: pre-wrap;
  word-break: break-all;
}
.lc-acli-path {
  margin-top: 4px;
  font-size: 12px;
  word-break: break-all;
  opacity: 0.8;
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
/* 失败原因常带长 URL 与 TLS 原文：必须允许任意位置换行，否则会把描述表撑破 */
.lc-error-text {
  color: var(--el-color-danger);
  word-break: break-all;
  line-height: 1.6;
}
/* 选择与探测结论冲突时只加色不改文案：提示必须同时说清"劝你别选"和"但你仍可以选" */
.lc-hint-warn {
  color: var(--el-color-warning);
}
/* 归属不同时的认领区：文案与动作同块，避免按钮孤零零挂在提示条外被忽略 */
.lc-claim {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.lc-claim-text {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
}
.lc-claim-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
/* 不能认领时也要说明为什么，否则用户只会反复点或直接去重新采集 */
.lc-claim-blocked {
  font-size: 12px;
  color: var(--el-color-warning);
}
/* 场景清单还原：标签 + 说明 + 重试按钮同排，窄屏自动换行，避免按钮被挤出可视区 */
.lc-spc-restore {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.lc-spc-restore .lc-hint {
  margin-top: 0;
}
/* 探测失败原文：可能夹着 acli 的日志行，必须保留空白与长串，不能截断成一句 */
.lc-probe-detail {
  margin-top: 4px;
  font-size: 12px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-all;
  font-family: var(--el-font-family-mono, monospace);
}
.lc-probe-history {
  margin-top: 8px;
  font-size: 12px;
}
.lc-probe-history-item + .lc-probe-history-item {
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px dashed var(--el-border-color-lighter);
}
.lc-probe-history-head {
  font-weight: 600;
  color: var(--el-text-color-regular);
}
/* 历史记录切换：一行内可换行，条数多时不撑破抽屉 */
.lc-history {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}
/* 探测元信息：时间 / 命中的二进制 / 重新探测，弱化到不抢描述表的注意力 */
.lc-probe-meta {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.lc-probe-binary {
  font-family: var(--el-font-family-mono, monospace);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 220px;
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
/* 平台派生证据块：与进度条同区，但明确区分「事实」与「结论」 */
.lc-evidence {
  margin-top: 8px;
  padding: 8px 10px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  font-size: 12px;
  line-height: 1.7;
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
/*
 * 场景项两行布局：第一行短名、第二行一句话释义。
 * 释义必须换行而不是挤在同一行——十个选项横排时，单行会在抽屉窄宽度下
 * 变成“短名 + 半句被截断的说明”，比不显示更让人困惑。
 */
.lc-module-group {
  align-items: stretch;
}
.lc-module-group :deep(.lc-module-item) {
  max-width: 100%;
}
.lc-module-group :deep(.lc-module-item .el-checkbox__label) {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.lc-module-name {
  font-weight: 500;
  white-space: normal;
}
.lc-module-summary {
  font-size: 11px;
  color: var(--el-text-color-secondary);
  line-height: 1.4;
  white-space: normal;
}
/* 已选场景的采集范围：浅底框与表单控件区分，不抢主流程视线 */
.lc-scenario-docs {
  width: 100%;
  margin-top: 6px;
  padding: 8px 10px;
  background: var(--el-fill-color-lighter);
  border-radius: 4px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.lc-scenario-doc-head {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  font-weight: 600;
  color: var(--el-text-color-regular);
}
.lc-scenario-doc-duration {
  font-weight: 400;
  font-size: 11px;
  color: var(--el-color-warning-dark-2);
}
.lc-scenario-doc-line {
  font-size: 12px;
  line-height: 1.5;
  color: var(--el-text-color-regular);
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
/* 高级选项：额外收集路径。默认折叠，展开后与表单同层级但不抢主流程视线 */
.lc-advanced {
  border-top: none;
  margin-top: 2px;
}
.lc-advanced :deep(.el-collapse-item__header) {
  height: 34px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.lc-advanced-title {
  font-size: 12px;
}
.lc-advanced-count {
  margin-left: 8px;
  font-size: 11px;
  color: var(--el-color-primary);
}
.lc-extra-paths {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
  width: 100%;
}
/* 路径可能很长：单行省略，hover 由 el-tag 的 title 兜底，避免把表单撑宽 */
.lc-extra-path-tag {
  max-width: 100%;
}
.lc-extra-path-tag :deep(.el-tag__content) {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: Consolas, Monaco, monospace;
  font-size: 11px;
}
.lc-extra-path-input {
  flex: 1 1 240px;
  min-width: 200px;
}
/* 场景模式切换与自定义场景卡片 */
.lc-scenario-mode {
  display: block;
  margin-bottom: 8px;
}
.lc-custom-scenarios {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  width: 100%;
}
.lc-custom-scenario :deep(.el-radio-button__inner) {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
  padding: 7px 10px;
  line-height: 1.45;
}
.lc-custom-name {
  font-size: 12.5px;
  font-weight: 600;
}
.lc-custom-meta {
  font-size: 11px;
  color: var(--el-text-color-secondary);
}
</style>
