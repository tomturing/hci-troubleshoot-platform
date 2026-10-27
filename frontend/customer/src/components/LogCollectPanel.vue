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
  createBridgeSocket,
  resolveBridgeUrl,
} from '@/api/terminal'
import {
  logCollectApi,
  type LogCollectModule,
  type LogCollectTask,
  type RelayStatus,
  type UploadInitResult,
} from '@/api/logCollect'
import {
  buildDeliverySubmitPayload,
  isCertificateUntrusted,
  platformCertTrusted,
  rememberPlatformCertTrust,
} from '@/utils/logCollectDelivery'
import { buildCancelNotice, cancelLogCollectTask } from '@/services/logCollectCancel'
import {
  buildSyncNotice,
  decideNodeSync,
  fallbackNodeState,
  hashReport,
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

const RELAY_URL = 'http://acli.sangfor.com.cn:5888'
const POLL_INTERVAL = 5000
// 终态任务的低频对账间隔：failed 的节点侧任务可能仍在跑（需要「接管」），
// 完全停轮询会让接管入口再也不出现；但每 5s 打一次 SSH 也没有必要。
const TERMINAL_POLL_INTERVAL = 30000

// 最近一次桥侧失败的规范错误码（LC-<LAYER>-<NNN>）。
//
// 桥侧 log_collect_result 已经带 error_code（layer/owner/retryable 由 sendMsg 补全），
// 但失败是以 throw 抛出的，码会在异常链里丢失。落库前用它把码补回平台任务，
// 否则 error_code 恒为 NULL（2026-10-27 实测 35/38），错误码目录无法统计与分流。
let lastNodeErrorCode = ''

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
  /** acli 编译时间，bridge 一直有回传，早期前端未声明导致只显示版本号 */
  acli_build_time?: string
  /** 本次探测真正命中的 acli 路径：同机存在多份 acli 时，它解释「为何与手敲版本不同」 */
  acli_binary?: string
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
  /**
   * 压缩包字节数，由 bridge 在上传窗口内 stat 得到。
   * 插件 progress 本身不打印大小，P1 成功后包会被自删，所以经常为 0（= 未量到）。
   */
  archive_size?: number
  upload_state: string
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
const probeResult = ref<ProbeResult | null>(null)
const pathMode = ref<'P1' | 'P2' | ''>('')
/**
 * 用户是否手动选过交付路径。
 *
 * 探测结果只提供**默认值**：可达性判据可能因探测时机（5888 临时抖动、ACL 未放通）
 * 而误判，用户若已明确选择就必须尊重，否则每次点「开始收集」都被静默改回。
 */
const pathModeTouched = ref(false)
const currentTask = ref<LogCollectTask | null>(null)
// 面板视图：form = 新建收集；result = 该工单已完成收集的只读结果
const viewMode = ref<'form' | 'result'>('form')
const resultTask = ref<LogCollectTask | null>(null)
// 该工单历史任务总数：用于告知"已完成是既成事实，可以重新收集但不是必须"
const historyTotal = ref(0)
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
// 对账提示：用于告诉用户"节点态与平台态不一致、下一步该做什么"
const nodeSyncHint = ref('')
// 僵尸任务/游离任务的一键动作（取消并重试 / 接管），空串表示无需处理
const nodeAction = ref<'' | 'cancel_retry' | 'adopt' | 'correct'>('')
// 接管按钮语义：resume=节点还在跑，接上进度；deliver=节点已采完，接着交付
const nodeAdoptMode = ref<'resume' | 'deliver'>('resume')
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
const moduleOptions = ref<ModuleOption[]>([...BUILTIN_MODULES])
const hostOptions = ref<HostOption[]>([])
const prepareState = ref<PrepareState>('idle')
const prepareError = ref('')
// 本次探测完成的时刻：探测结论是快照（acli 可能被自动升级），界面必须显式说明"何时读的"。
const probedAt = ref<number | null>(null)
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

const resultDeliveryText = computed(() => {
  if (resultMode.value === 'acli_direct') return 'P1 直传日志服务器'
  if (resultMode.value === 'bridge_relay') return 'P2 经平台中转'
  return '未记录'
})

/**
 * 交付路径的选择说明。
 *
 * 两条路径不是 UI 偏好，而是**由网络可达性决定的物理选择**：
 * P1 要求 HCI 自己能连日志服务器 5888；P2 改由客户机经平台代传，只要求 HCI 能连平台。
 * 因此文案必须把"代价"讲清楚（P1 平台不留副本），而不是只给一个枚举值。
 *
 * 探测结论只用来**提示风险**，不用来禁止选择：可达性是时刻变化的（ACL 临时放通、
 * 5888 抖动都会让探测误判），用户比探测更清楚现场网络。
 */
const pathModeHint = computed(() => {
  const reachable = probeResult.value?.log_server_reachable
  if (pathMode.value === 'P1') {
    return reachable
      ? 'acli 插件从 HCI 直接上传到日志服务器：省一跳中转，但平台不留副本，事后无法回看/下载'
      : '探测显示 HCI 当前无法访问日志服务器，P1 可能在采集完成后上传失败；拿不准请选 P2'
  }
  if (pathMode.value === 'P2') {
    return 'HCI 只生成压缩包，由客户机经平台中转到日志服务器：平台留副本，可回看/下载，但需要客户机在线'
  }
  return '未选择时按探测结果自动决定'
})

/**
 * 压缩包大小。
 *
 * P1 的字节数只有两种来源：插件 progress 输出（不含大小）或节点上的文件本身（上传成功后
 * 插件立刻自删）。bridge 会在上传窗口内 stat 一次回填，但轮询间隔内完成的小包常常错过，
 * 因此 P1 空值属预期，文案要说清原因，别让用户以为平台漏采。
 */
const resultSizeText = computed(() => {
  const size = resultTask.value?.archive_size
  if (!size) {
    return resultMode.value === 'acli_direct' ? '未记录（直传后压缩包已删除）' : '未记录'
  }
  const mb = size / 1024 / 1024
  return mb >= 1024 ? `${(mb / 1024).toFixed(2)} GB` : `${mb.toFixed(1)} MB`
})

const resultArchiveText = computed(() => {
  const task = resultTask.value
  if (!task) return ''
  if (resultMode.value === 'acli_direct') return '已由插件直传日志服务器'
  if (task.relay_status === 'success') return '已归档到日志服务器'
  if (task.relay_status === 'failed') return '归档失败（详见平台任务记录）'
  return '未记录'
})

const resultArtifactText = computed(() => {
  const task = resultTask.value
  if (!task) return ''
  if (task.archive_deleted) return 'HCI 上的压缩包已清理'
  if (task.archive_path) return task.archive_path
  return resultMode.value === 'acli_direct' ? '由插件上传后自动清理，路径未记录' : '未记录'
})

const resultCleanupTip = computed(() => {
  const task = resultTask.value
  if (!task || task.archive_deleted) return ''
  if (resultMode.value === 'acli_direct') {
    return 'P1 由插件直传日志服务器，上传成功后插件会自动删除 HCI 上的压缩包，无需人工清理。'
  }
  if (resultMode.value === 'bridge_relay' && task.archive_path) {
    return `平台不自动删除 HCI 上的采集包，请在节点上自行清理：rm -f ${task.archive_path}（不要删除 /sf/data/local/acli/plugins 与 tasks 目录）`
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
    Boolean(nodeSyncHint.value) ||
    Boolean(cleanupTip.value) ||
    Boolean(resultCleanupTip.value),
)

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
    probedAt.value = Date.now()
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

/**
 * 进度百分比只有一个来源：节点报告。
 *
 * 旧实现对「接管态」读 `currentTask.progress`（平台快照）、对「自己发起的任务」
 * 读 `progress.percent`（节点），于是同屏出现「已完成 2/2 + 进度 0%」。
 * 平台 progress 只是投影，接管态下它可能整场都没被写过。
 */
const percent = computed(() => {
  if (stage.value === 'done') return 100
  const nodePercent = Math.min(100, Math.max(0, progress.value?.percent ?? 0))
  if (stage.value === 'relaying') {
    return Math.max(nodePercent, 95 + Math.min(4, relayStatus.value === 'running' ? 2 : 0))
  }
  if (stage.value === 'uploading' || stage.value === 'pulling') {
    // P1 直传没有分片进度，只能用节点自己的百分比，避免出现 60% 起跳的假进度
    if (isDirectDelivery.value && stage.value === 'uploading') return nodePercent
    return Math.max(nodePercent, 60 + Math.round(transfer.percent * 0.3))
  }
  return nodePercent
})

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

const statusText = computed(() => {
  if (stage.value === 'failed') return errorMessage.value || currentTask.value?.error_message || '任务失败'
  if (stage.value === 'done') return '日志已归档到日志服务器'
  if (stage.value === 'collecting') {
    const p = progress.value
    return p ? `${p.status}（${p.done}/${p.total}）` : '采集进行中'
  }
  if (stage.value === 'pulling') return `正在从 HCI 拉取：${transfer.pulled} 字节`
  if (stage.value === 'uploading') {
    if (isDirectDelivery.value) {
      const p = progress.value
      return `正在发送到日志服务器：${p?.upload_state || '发送中'}（${p?.percent ?? 0}%）`
    }
    return `正在上传平台：${transfer.uploadedParts}/${transfer.partCount} 片`
  }
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
  // 桥的结构化日志也会推给本连接：面板这条 WS 没有绑定工单（sub.caseID 为空），
  // bridge 按「全量订阅者」把日志推过来。历史实现在这里直接 return，
  // 于是面板通道上的桥日志从未进入回采链路——2026-10-27 现场
  // 云端查不到 log_collect.session_missing 的直接原因之一就是这个分支缺失。
  if (data.type === 'bridge_log') {
    chatStore.forwardBridgeLog(data as Record<string, unknown>)
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
  action:
    | 'probe'
    | 'create'
    | 'progress'
    | 'cancel'
    | 'stat'
    | 'transfer'
    | 'acli_sync'
    | 'delivery_submit'
    | 'delivery_status'
    | 'delivery_resume'
    | 'delivery_cancel',
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
      // 与 log_collect 同一锚点：不带 node_ip 时 bridge 会去找键为 caseID 的会话，
      // 而真实会话键是 caseID@nodeIP → 必然 session_missing（安装链路根本不会启动）。
      ws.send(buildAcliSyncMessage(props.caseId, { force: false, minDiskMb: 100, nodeIp: nodeIp.value }))
    } else if (action.startsWith('delivery_')) {
      // 交付作业：submit 立刻返回 job_id，执行权在 bridge；
      // 后续只轮询 status，页面关闭不影响交付（重连后按同一 job_id 继续）。
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
          action as 'probe' | 'create' | 'progress' | 'cancel' | 'stat' | 'transfer',
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
      e?.response?.status === 404 ? '任务不存在或不属于当前客户端' : '任务状态加载失败',
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
    stage.value = TASK_STATUS_STAGE[latest.status] ?? stage.value
    if (latest.error_message) errorMessage.value = latest.error_message

    if (!sshConnected.value) {
      if (!silent) ElMessage.warning('SSH 会话已断开，节点侧进度不可用；已刷新平台任务状态')
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
 * 因此恢复顺序固定为：进行中 → 最近一次已完成 → 最近一次失败 → 空白表单（可重新收集）。
 * 失败记录必须恢复：节点侧任务可能仍在跑，接管只能从这条记录发起；退回空白表单
 * 会让用户以为"没有任务"，只能重新下发并撞插件互斥（2026-10-27 修正）。
 */
async function restoreTaskState(force = false) {
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
    // 已完成优先展示：这是用户最需要看到的结果（收了什么、什么时候收的、包在哪）
    const completed = items.find((item) => item.status === 'completed')
    if (completed) {
      showResult(completed)
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
  stage.value = 'done'
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
  nodeSyncHint.value = ''
  nodeAction.value = ''
  // 新一次下发 = 解除上一轮的写库熔断
  syncPatchBlocked = false
  lastRefreshAt.value = null
  stopAutoSync()
  relayStatus.value = 'none'
  cleanupTip.value = ''
  errorMessage.value = ''
  if (last) applyTaskToForm(last)
  if (prepareState.value !== 'ready') void prepare({ silent: true })
}

// ── 主流程 ──────────────────────────────────────────────────────────────────

async function handleStart() {
  if (!form.dateRange || form.dateRange.length !== 2) {
    ElMessage.warning('请选择时间范围')
    return
  }

  // 会话必须先活着。openLogCollect 只在「打开面板那一刻」校验过，
  // 面板开着期间 SSH 可能已经断开，此时任何 bridge 动作都只会拿到 session_missing——
  // 与其把注定失败的请求发出去，不如当场告诉用户去重连。
  if (!sshConnected.value) {
    ElMessage.warning('SSH 会话已断开，请先在 SSH 终端重新连接节点')
    return
  }

  const targetNode = nodeIp.value
  if (!targetNode) {
    ElMessage.warning('请先在 SSH 终端连接 HCI 节点')
    return
  }

  cleanupTip.value = ''
  errorMessage.value = ''
  failedStep.value = 0
  transfer.sha256 = ''
  currentTask.value = null
  progress.value = null
  // 新任务从 0 开始记：不继承上一次的峰值
  observedArchiveSize.value = 0
  relayStatus.value = 'none'
  // 新任务重新开始：上一次失败的规范码不能带进来，否则会把旧码写到新任务上。
  lastNodeErrorCode = ''
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

    // 只在用户没手选过时用探测结论兜底；已手选则原样保留（见 pathModeTouched 注释）
    if (!pathModeTouched.value || !pathMode.value) {
      pathMode.value = probeResult.value.log_server_reachable ? 'P1' : 'P2'
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
    if (preSync && ['adopt', 'cancel_retry', 'warn_node_busy'].includes(preSync.action)) {
      stage.value = 'idle'
      ElMessage.warning(buildSyncNotice(preSync))
      return
    }

    // 下发前锚定基线：create 不会让报告立刻刷新，下发后的头几次 progress
    // 仍可能是上一次任务的完成报告。指纹随任务入库（detail.baseline_report_hash），
    // 换浏览器/重启后接管任务时仍能把"这一份报告属于哪次下发"判清楚。
    // 用函数取值而不是直接读 progress.value：本函数前面刚赋过 null，TS 会把 .value 收窄成 never。
    const baseline = readProgressHash()
    baselineReportHash.value = baseline

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
        // 探测事实随任务落库：失败任务必须能回答"哪版 acli / 什么架构"，
        // 只留在前端内存时，关面板或换端后这条证据就没了。
        arch: probeResult.value?.architecture || undefined,
        acli_version: probeResult.value?.acli_version || undefined,
        baseline_report_hash: baseline || undefined,
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
    if (created.status !== 'ok') {
      // 下发失败的规范码必须留住：它是后面 patch failed 写库的唯一来源。
      lastNodeErrorCode = String(created.error_code || '')
      throw new Error(created.message || '任务下发失败')
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
          // 桥侧规范码：没有它，失败原因只能靠读 error_message 文本猜。
          // 平台侧异常同样要保留规范码——只写 axios 原文（"Request failed with
          // status code 500"）会让 error_code 永远为空，事后完全无法归因。
          error_code: lastNodeErrorCode || platformErrorCode(error) || undefined,
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
  // 安装成功后必须重探：节点清单与场景目录只有 acli 可用后才读得到，
  // 不重探会让用户"装完 acli 仍看到读不到节点清单"，误判为安装无效。
  const refreshed = await prepare({ silent: true })
  if (!refreshed && prepareState.value === 'failed') {
    // 重探失败不改写安装结果，也不让面板停在红色错误态：采集仍可继续（-i all 合法）
    prepareState.value = 'ready'
    prepareError.value = ''
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

/**
 * 读一次节点任务快照。
 *
 * `force` 只给主流程（pollUntilCollected）用：它自己持有轮询权，
 * 需要绕过 `pollingBridgeProgress` 的互斥保护；其余入口一律走互斥。
 */
async function readNodeSnapshot(timeoutMs = 20000, force = false): Promise<ProgressSnapshot | null> {
  if (!sshConnected.value || !socket || socket.readyState !== WebSocket.OPEN) return null
  if (pollingBridgeProgress && !force) return null
  try {
    const result = await sendAndWait(socket, 'progress', {}, timeoutMs)
    if (result.status === 'error') return null
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
 * `upload_state`（发送中 / 成功 / 失败）是节点自己给出的阶段信号，用它点亮「上传」。
 */
function applyNodeStage(snapshot: ProgressSnapshot) {
  if (stage.value !== 'collecting') return
  if (/发送中|上传中/.test(snapshot.upload_state ?? '')) stage.value = 'uploading'
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
    const action = decideNodeSync({
      platformStatus: currentTask.value?.status ?? null,
      nodeState,
      archivePath: snapshot.archive_path,
      progressTotal: snapshot.total,
      expectedHosts: expectedHostCount(),
      reportRefreshed: reportRefreshedOf(snapshot),
      reportHosts: (snapshot.hosts ?? []).map((host) => host.ip),
      targetHosts: targetHostList(),
    })

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
    nodeSyncHint.value = buildSyncNotice(action)

    // syncPatchBlocked：收到过确定性拒绝（4xx）后不再自动写库。
    // 状态机拒绝不会因为再等 30 秒而变好，继续重试只会把同一条错误刷成日志噪音。
    if (options.applyPatches && currentTask.value && !syncPatchBlocked) {
      if (action.action === 'patch_completed' || action.action === 'patch_failed') {
        try {
          const patch =
            action.action === 'patch_completed'
              ? {
                  status: 'completed' as const,
                  progress: 100,
                  // 交付路径只有两种，P1 创建时必带 remote_url；补进产物记录，
                  // 避免"已完成但 upload_mode 为空"在结果页显示成"未记录"
                  upload_mode:
                    currentTask.value.upload_mode ?? (currentTask.value.remote_url ? 'acli_direct' : undefined),
                  archive_path: snapshot.archive_path || currentTask.value.archive_path || undefined,
                  // 节点侧只在包还在时量得到；已有值优先保留，避免 P2 的记录被 0 覆盖。
                  // 兜底顺序里必须有 observedArchiveSize：判"完成"的那份快照必然量不到
                  // （P1 上传成功即删包），只用它的话大小永远写不进去。
                  archive_size:
                    snapshot.archive_size ||
                    observedArchiveSize.value ||
                    currentTask.value.archive_size ||
                    undefined,
                }
              : {
                  status: 'failed' as const,
                  error_message: normalizeErrorMessage(action.reason),
                  error_code: lastNodeErrorCode || undefined,
                }
          currentTask.value = await logCollectApi.patchTask(currentTask.value.task_id, patch)
          if (action.action === 'patch_completed') {
            stage.value = 'done'
            nodeSyncHint.value = buildSyncNotice(action)
            // 与「刷新页面重新进入」保持一致：完成即切结果视图。
            // 否则页面会停在收集表单上、右下角按钮还写着「开始收集」，
            // 用户以为没完成（现场 2026-10-26 23:23）。
            showResult(currentTask.value)
          } else {
            stage.value = 'failed'
            errorMessage.value = action.reason
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
          if (!options.silent) ElMessage.warning(`平台状态同步失败：${error?.message || error}`)
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

  const collected = await pollUntilCollected(ws, token, baseline, adoptedDeliver)
  if (token !== runToken) return
  if (!collected) throw new Error('采集未完成')
  const isDirect = task.upload_mode === 'acli_direct' || Boolean(task.remote_url)
  if (!isDirect) {
    await runP2(ws, task, token)
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
  // stat 失败说明包已被插件删除（多半是"其实传成功了"），不能降级
  const statResult = await sendAndWait(ws, 'stat', { sourcePath: archivePath }, 30000).catch(() => null)
  if (!statResult || statResult.status !== 'ok') return false

  const reason = cause instanceof Error ? cause.message : String(cause)
  ElMessage.warning(`直传日志服务器失败（${reason}），已自动切换平台中转续传`)
  nodeSyncHint.value = '直传失败，已自动切换平台中转续传'
  stage.value = 'pulling'
  currentTask.value = await logCollectApi.patchTask(task.task_id, { upload_mode: 'bridge_relay' })
  await runP2(ws, task, token)
  return true
}

/** 一键「接管」：平台终态改回采集中，继续跟进节点上仍在跑的任务 */
async function adoptNodeTask() {
  const task = currentTask.value
  if (!task) return
  let ws: WebSocket
  try {
    ws = await openSocket()
  } catch (error: any) {
    ElMessage.error(error.message)
    return
  }
  const token = ++runToken
  try {
    // 用户显式接管：解除自动对账的写库熔断，按真实状态重新推进
    syncPatchBlocked = false
    stage.value = 'collecting'
    currentTask.value = await logCollectApi.patchTask(task.task_id, { status: 'collecting' })
    const delivering = nodeAdoptMode.value === 'deliver'
    nodeAction.value = ''
    nodeSyncHint.value = ''
    ElMessage.success(
      delivering ? '已接管节点任务，正在继续交付（不会重新采集）' : '已接管节点任务，继续跟进进度',
    )
    await resumeTaskFlow(
      ws,
      currentTask.value ?? task,
      token,
      // 接管交付时报告已定格：基线必须置空，否则会与"当下这份报告"逐字相同而判 stale。
      delivering ? '' : readProgressHash(),
      delivering,
    )
  } catch (error: any) {
    if (token !== runToken) return
    errorMessage.value = normalizeErrorMessage(String(error?.message || error))
    failedStep.value = stepActive.value
    stage.value = 'failed'
    ElMessage.error(errorMessage.value)
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
    ElMessage.error(error.message)
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
    ElMessage.error(errorMessage.value)
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

async function pollUntilCollected(
  ws: WebSocket,
  token: number,
  baseline = '',
  adoptedDeliver = false,
): Promise<ProgressSnapshot | null> {
  // 采集上限 1 小时（720 次 × 5s）：超时明确失败，避免面板无限转圈。
  pollingBridgeProgress = true
  // 基线缺失时退化为"首次看到的进行中报告"：之后出现不同的报告即可判定归属。
  let baselineHash = baseline
  try {
    for (let attempt = 0; attempt < 720; attempt++) {
      if (token !== runToken) return null
      const result = await sendAndWait(ws, 'progress', {}, 60000)
      // bridge 明确报错（如会话已断开）必须立刻上抛：
      // 早先这里只读 log_data，错误结果会被当成"还没有进度"，面板空转一小时仍显示 0%。
      if (result.status === 'error') {
        // 与 create 失败同样处理：先把规范码留住，再由 catch 统一写库。
        lastNodeErrorCode = String(result.error_code || '')
        throw new Error(result.message || '节点侧进度查询失败')
      }
      const snapshot = result.log_data as ProgressSnapshot | null
      if (snapshot) {
        // 主流程自己直连 sendAndWait，不走 readNodeSnapshot：这里单独记一次大小。
        rememberObservedArchiveSize(snapshot)
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
          reportHosts: (snapshot.hosts ?? []).map((host) => host.ip),
          targetHosts: targetHostList(),
          // 接管交付：报告在接管那一刻已定格，reportRefreshed 恒为 false，
          // 必须显式放行，否则会判 stale 后空转到 60 分钟超时。
          adoptedDeliver,
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

async function finishP1(task: LogCollectTask, snapshot: ProgressSnapshot | null) {
  // 必须用"通过完成判据的那一份快照"，不能回读 progress.value：
  // 否则上传状态/包路径可能来自另一次读取（早先就是拿旧报告的「成功」放行了假完成）。
  if (snapshot?.upload_state && !snapshot.upload_state.includes('成功')) {
    throw new Error(`日志服务器上传失败：${snapshot.upload_state}，可改用 P2 中转`)
  }
  await markCompleted(task.task_id, {
    upload_mode: 'acli_direct',
    archive_path: snapshot?.archive_path || undefined,
    // 大小来自 bridge 的 stat（插件 progress 不打印大小）。这里的 snapshot 是
    // "上传成功"那一份——包已被插件自删，size 必然为 0，所以必须回退到过程中记录的峰值，
    // 否则每次 P1 都会写成「未记录」。
    archive_size: snapshot?.archive_size || observedArchiveSize.value || undefined,
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

async function runP2(ws: WebSocket, task: LogCollectTask, token: number) {
  const archivePath = progress.value?.archive_path || ''
  if (!archivePath) throw new Error('未获取到压缩包路径，无法中转')

  // 1) 取包大小（init 需要 total_size）
  const statResult = await sendAndWait(ws, 'stat', { sourcePath: archivePath }, 30000)
  if (statResult.status !== 'ok') throw new Error(statResult.message || '读取压缩包大小失败')
  const totalSize = Number(statResult.log_data?.size || 0)
  if (totalSize <= 0) throw new Error('压缩包大小异常')

  // 2) 平台签发分片会话：走 /deliver 而非 /uploads。
  //    /deliver 由服务端负责"会话大小不一致"的自愈与会话记账；
  //    /uploads 会把 409 抛给前端，而前端从不 abort → 会话就此死锁（现场复现过）。
  const delivered = await logCollectApi.deliverTask(task.task_id, {
    total_size: totalSize,
    source_path: archivePath,
  })

  // 3) 交付作业化：submit 立刻返回 job_id，执行权交给 bridge。
  //    这一步之后浏览器关掉也不影响交付；旧实现要在这里挂 2 小时等 transfer 回包。
  stage.value = 'pulling'
  currentTask.value = await logCollectApi.patchTask(task.task_id, { status: 'uploading' })
  await runDeliveryJob(ws, token, {
    task,
    sourcePath: archivePath,
    totalSize,
    session: delivered.upload,
  })

  // 5) 合并与代传由平台在数据面 complete 后自动触发（bridge 用上传令牌收尾），
  //    前端只等 relay 终态：这最后一步也不再依赖浏览器一直在线。
  stage.value = 'relaying'
  await pollRelay(task.task_id, token)
  if (token !== runToken) return

  // 归档成功才推进任务终态：否则任务会永久停在 uploading，并占住该工单的进行中名额
  await markCompleted(task.task_id, {
    upload_mode: 'bridge_relay',
    archive_path: archivePath,
    archive_size: totalSize,
  })
  stage.value = 'done'
  cleanupTip.value =
    `日志包已归档到日志服务器（${totalSize} 字节）。` +
    '平台保留副本供回看，HCI 上的采集包仍在，请在节点上自行清理：\n' +
    `rm -f ${archivePath}\n` +
    '注意：不要删除 /sf/data/local/acli/plugins 与 tasks 目录。'
  ElMessage.success('日志已上传平台并归档到日志服务器')
}

/** 交付作业视图（与 bridge `deliveryJobView` 对齐）。 */
interface DeliveryJobView {
  job_id?: string
  phase?: string
  progress?: number
  pulled_bytes?: number
  uploaded_parts?: number
  part_count?: number
  needs_reauth?: boolean
  last_error?: string
}

/**
 * 轮询交付作业直到终态。
 *
 * 为什么不订阅：作业在 bridge 里跑，浏览器随时可能断；轮询无状态，
 * 重连后拿 job_id 再查一次就能继续显示，不需要服务端保存订阅关系。
 */
async function waitDeliveryJob(ws: WebSocket, jobId: string, token: number) {
  // 上限 2 小时（3600 次 × 2s）：超过仍未完成交给外层提示，不再无限等待
  for (let attempt = 0; attempt < 3600; attempt++) {
    const result = await sendAndWait(ws, 'delivery_status', { jobId }, 10000)
    if (token !== runToken) return
    if (result.status !== 'ok') {
      // 作业丢失（bridge 重启且记录未落盘）：降级为失败，由用户重试
      throw new Error(result.message || '交付作业不存在')
    }
    const view = (result.log_data || {}) as DeliveryJobView
    if (view.phase === 'pulling') stage.value = 'pulling'
    if (view.phase === 'uploading' || view.phase === 'completing') stage.value = 'uploading'
    transfer.percent = view.progress || 0
    transfer.pulled = view.pulled_bytes || 0
    transfer.uploadedParts = view.uploaded_parts || 0
    transfer.partCount = view.part_count || 0

    if (view.phase === 'done') return view
    if (view.needs_reauth) {
      throw new Error('交付需要重新认证：请重新连接 SSH 后继续（作业进度已保留）')
    }
    if (view.phase === 'failed') {
      throw new Error(view.last_error || '交付失败（作业进度已保留，可重试）')
    }
    await sleep(2000)
  }
  throw new Error('交付超过 2 小时仍未完成，作业仍在 bridge 侧继续，可稍后刷新查看')
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
): Promise<string> {
  const payload = buildDeliverySubmitPayload(
    {
      taskId: args.task.task_id,
      sourcePath: args.sourcePath,
      totalSize: args.totalSize,
      session: args.session,
      mode: 'collect',
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
): Promise<void> {
  const origin = window.location.origin
  let tlsInsecure = platformCertTrusted(origin)

  for (let attempt = 0; attempt < 2; attempt += 1) {
    const jobId = await submitDeliveryJob(ws, args, tlsInsecure)
    try {
      await waitDeliveryJob(ws, jobId, token)
      return
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err)
      // 只在"首次 + 尚未授权 + 确属证书问题"时打扰用户，其余错误原样抛出
      if (attempt > 0 || tlsInsecure || !isCertificateUntrusted(message)) throw err
      if (!(await confirmTrustPlatformCertificate(origin))) throw err
      rememberPlatformCertTrust(origin)
      tlsInsecure = true
      // 以平台记录为准刷新已传分片：失败可能发生在第 N 片，续传不能靠旧快照
      args.session = await logCollectApi.getUploadStatus(args.task.task_id)
      stage.value = 'uploading'
    }
  }
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
      // 收起面板（v-show，不销毁）时停止自动刷新，避免不可见还在后台发 SSH 命令
      stopAutoSync()
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
    if (prepareState.value !== 'probing') {
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
        description="采集可以继续，但日志回传与终端录像回放会被平台按归属拒绝；建议换用本人名下的工单号，或直接用当前工单重新采集。"
      />
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
        两端对账结论：节点是唯一真相源，平台只是投影。
        不一致时这里给出唯一出口——接管（节点还在跑）或取消并重试（节点异常），
        用户不必再去 SSH 敲 acli log_collect cancel。
      -->
      <el-alert
        v-if="nodeSyncHint"
        :title="nodeSyncHint"
        :type="nodeAction ? 'warning' : 'info'"
        :closable="false"
        show-icon
        class="lc-sync"
      >
        <div v-if="nodeAction" class="lc-sync-actions">
          <el-button v-if="nodeAction === 'adopt'" size="small" type="primary" @click="adoptNodeTask">
            {{ nodeAdoptMode === 'deliver' ? '继续交付' : '接管继续' }}
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
        </el-descriptions>
        <p class="lc-hint">
          当前客户端在本工单下共 {{ historyTotal }} 条收集记录；已完成的任务不会被覆盖，重新收集会新建一条任务。
        </p>
      </div>

      <template v-else>
        <el-descriptions v-if="probeResult" :column="descColumns" border size="small" class="lc-probe">
          <el-descriptions-item label="架构">{{ probeResult.architecture }}</el-descriptions-item>
          <el-descriptions-item label="acli">
            <span :title="acliTitle">{{ acliVersionText }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="交付路径">
            <el-tag :type="probeResult.log_server_reachable ? 'success' : 'warning'" size="small">
              {{ probeResult.log_server_reachable ? 'P1 直传日志服务器' : 'P2 经平台中转' }}
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

        <!-- 探测结论是快照：acli 可能被自动升级，面板关闭重开或点这里都要重新读一次 -->
        <div v-if="probeResult" class="lc-probe-meta">
          <span v-if="probedAtText">探测于 {{ probedAtText }}</span>
          <span v-if="probeResult.acli_binary" class="lc-probe-binary" :title="probeResult.acli_binary">
            {{ probeResult.acli_binary }}
          </span>
          <el-button
            text
            size="small"
            type="primary"
            :disabled="prepareState === 'probing'"
            @click="prepare()"
          >
            重新探测
          </el-button>
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
        </div>

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
        <!-- 探测期间只锁"提交"，不锁表单：用户可以先看清可填项，不必等探测结束 -->
        <el-form
          :model="form"
          :label-position="formLabelPosition"
          label-width="90px"
          :disabled="running"
        >
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
          <el-form-item label="交付路径">
            <!--
              必须可改：探测到的可达性只代表"那一刻"，ACL 临时放通、5888 抖动都会误判；
              而两条路径的代价不同（P1 平台不留副本），只有用户能权衡。
            -->
            <el-radio-group v-model="pathMode" :disabled="running" @change="pathModeTouched = true">
              <el-radio-button value="P2">P2 经平台中转</el-radio-button>
              <el-radio-button value="P1">P1 直传日志服务器</el-radio-button>
            </el-radio-group>
            <div class="lc-hint" :class="{ 'lc-hint-warn': pathMode === 'P1' && probeResult && !probeResult.log_server_reachable }">
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
        </template>
        <template v-else-if="lastRefreshText">{{ lastRefreshText }}</template>
        <template v-else>选择时间范围与采集模块后开始收集</template>
      </div>
      <div class="panel-composer__body">
        <div class="panel-composer__actions">
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
/* 选择与探测结论冲突时只加色不改文案：提示必须同时说清"劝你别选"和"但你仍可以选" */
.lc-hint-warn {
  color: var(--el-color-warning);
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
