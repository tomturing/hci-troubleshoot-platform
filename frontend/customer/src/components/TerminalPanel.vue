<script setup lang="ts">
/**
 * TerminalPanel.vue
 * 两态连接 + 双交互模式实现（SSH 会话已与工单解耦）：
 * - 未连接 → "连接 SSH" → 打开统一登录弹框（SshConnectDialog / terminal-only）
 * - 已连接 → 显示 xterm 终端，支持通过顶部下拉菜单在两种模式间即时切换：
 *   - 模式一（Agent 协作模式：关闭自动执行 / 安全模式 / 激进模式）：上方输出框 + 底部命令输入框，配合排障 Agent 使用
 *   - 模式二（交互终端 / 实时 Shell）：全屏实时交互终端，支持 Tab 自动补全、方向键历史、Ctrl+C 中断、
 *     Ctrl+Shift+C/V 复制粘贴、PTY 窗口尺寸自适应（vim/top/less）
 *
 * 终端、文件下载、日志收集、Agent 排障共用同一条 SSH 会话：连接本身不再要求
 * 先建工单，因此不存在"无工单 → 先弹创建工单"的中间态。工单只决定录像/历史
 * 归档到哪条工单下（无工单照常可连，只是不归档）。
 *
 * Task 42: 终端操作录制功能
 * - 模式一与模式二的命令输入和输出回显均自动录制并上传数据库
 * - 用于排障复盘和审计回放（TerminalReplay）
 */
import { ref, computed, watch, nextTick, onMounted, onBeforeUnmount } from 'vue'
import { ElMessage } from 'element-plus'
import { Close, Monitor, VideoCamera } from '@element-plus/icons-vue'
import { Terminal } from 'xterm'
import { FitAddon } from 'xterm-addon-fit'
import 'xterm/css/xterm.css'

import { useChatStore } from '@/stores/chat'
import { checkBridgeBeforeOpen } from '@/api/terminal'
import {
  createRecordingState,
  startRecording,
  stopRecording,
  recordInput,
  recordOutput,
  forceFlushOutput,
} from '@/api/terminal-recording'
import { InteractiveInputTracker } from '@/utils/interactiveInputRecorder'

type TerminalHeaderMode = 'off' | 'safe-only' | 'aggressive' | 'interactive'

const chatStore = useChatStore()
const emit = defineEmits<{ 'open-terminal-history': [] }>()

// ===== 终端操作录制状态 =====
const recordingState = createRecordingState()
const inputTracker = new InteractiveInputTracker()
// 串行化录制写入，保证「前序输出 → 本次输入 → 本次输出」的 seq_number 严格有序
let recordingChain: Promise<void> = Promise.resolve()

const terminalInputRef = ref<{ focus: () => void } | null>(null)
const terminalContainer = ref<HTMLElement | null>(null)

const localInput = ref('')
const manualDisconnect = ref(false)
// 最近一条真正下发到远端 shell 的命令，供「上条命令」一键回填。
// 断开连接时清空：换主机/换会话后再回填上一台机器的命令，等于把 A 机的操作
// 顺手送到 B 机，这类误发在客户生产环境里代价很高。
const lastExecutedCommand = ref('')

// ===== 终端交互模式（模式一：agent / 模式二：interactive） =====
const isInteractiveMode = computed(() => (chatStore.terminalMode ?? 'agent') === 'interactive')
const selectedHeaderMode = computed<TerminalHeaderMode>(() =>
  isInteractiveMode.value ? 'interactive' : chatStore.autoExecuteMode,
)

function handleHeaderModeChange(mode: TerminalHeaderMode) {
  if (typeof chatStore.setTerminalHeaderMode === 'function') {
    chatStore.setTerminalHeaderMode(mode)
    return
  }
  // 兼容仅挂载基础字段的测试 store stub
  if (mode === 'interactive') {
    chatStore.setTerminalMode?.('interactive')
  } else {
    chatStore.setTerminalMode?.('agent')
    chatStore.setAutoExecuteMode(mode)
  }
}

// ===== 前置检测 loading =====
const checkingBridge = ref(false)

async function handleOpenSshDialog() {
  // SSH 是独立能力：无论有没有工单，入口都是同一条登录弹框。
  checkingBridge.value = true
  try {
    const status = await checkBridgeBeforeOpen()
    chatStore.sshConnectDialogBridgeStatus = status
    chatStore.openSshFlowDialog(
      chatStore.currentCase?.case_id || chatStore.logCollectCaseId || null,
      'terminal-only',
    )
  } finally {
    checkingBridge.value = false
  }
}

const fullOutputText = ref('')
const currentCommandOutput = ref('')
const lastCommandOutput = ref('')
const hasExecutedCommand = ref(false)

// 使用 chatStore 的全局 SSH 状态
const isConnected = computed(() => chatStore.sshConnectionState === 'connected')
const isConnecting = computed(() => chatStore.sshConnectionState === 'connecting')
const isError = computed(() => chatStore.sshConnectionState === 'error')
const connectingLabel = computed(() => {
  if (chatStore.sshConnectionState === 'connecting') return '正在连接...'
  return '连接中...'
})
const errorMessage = computed(() => chatStore.sshErrorMessage)

// 终端录像按工单归档：优先当前聊天工单，其次日志收集绑定的工单。
// 日志收集在没有聊天工单时会按用户指定的工单号建档/复用，采集会话的录像同样
// 挂在该工单上；只看 currentCase 会让"日志收集已建单"的用户仍点击无反应。
// 按钮保持可点（点了必须给出原因），而不是静默 return 让人以为功能坏了。
const historyCaseId = computed(
  () => chatStore.currentCase?.case_id || chatStore.logCollectCaseId || '',
)
const hasCaseContext = computed(() => Boolean(historyCaseId.value))
const historyHint = computed(() =>
  hasCaseContext.value
    ? '查看终端回放历史'
    : '终端历史按工单归档，请先创建工单或在日志收集中指定工单号',
)

function openTerminalHistory() {
  if (!hasCaseContext.value) {
    ElMessage.warning('终端历史按工单归档，请先创建工单或在日志收集中指定工单号后再查看')
    return
  }
  emit('open-terminal-history')
}

let xterm: Terminal | null = null
let fitAddon: FitAddon | null = null
let resizeObserver: ResizeObserver | null = null
let dataDisposable: { dispose: () => void } | null = null
let lastHandledChunkSeq = 0

// 同步当前诊断阶段到录制状态（便于回放按阶段筛选）
watch(
  () => chatStore.diagnosticStage,
  (stage) => {
    if (stage) {
      recordingState.diagnosticStage.value = stage
    }
  },
  { immediate: true },
)

// 监听无损 SSH 输出分片（带单调递增序号，确保连按相同字符/退格不丢帧）
watch(
  () => chatStore.sshTerminalOutputChunk,
  (chunk) => {
    if (!chunk?.data || chunk.seq === lastHandledChunkSeq) return
    if (chatStore.sshCommandConsumer === 'collection') return
    lastHandledChunkSeq = chunk.seq
    writeTerminalRaw(chunk.data)
  },
)

// 兼容旧版单测仅触发 sshTerminalOutputEvent 字符串的场景（真实运行时由 chunk 接管，避免重复写入）
watch(
  () => chatStore.sshTerminalOutputEvent,
  (output) => {
    if (chatStore.sshTerminalOutputChunk != null) return
    if (output && chatStore.sshCommandConsumer === 'terminal') {
      writeTerminalRaw(output)
    }
  },
)

// 监听 bridge 提示（如"输入无法路由到会话"）：写进终端，避免出现"点了毫无反应"的观感。
// 与 ssh_error 严格区分——这里只是提示，不改变连接状态。
watch(
  () => chatStore.sshTerminalNotice,
  (notice) => {
    if (!notice?.text) return
    writeInfoLine(notice.text)
  },
)

// 监听命令卡片「发送到终端」注入
watch(
  () => chatStore.terminalInputCommand,
  (cmd) => {
    if (!cmd) return

    if (isInteractiveMode.value && isConnected.value) {
      const normalized = cmd.replace(/[\r\n]+$/, '')
      // 安全红线：多行命令直接写入未启用 bracketed paste 的远端 shell 会立即逐行执行。
      // 因此检测到多行命令时自动切回模式一输入框，由用户逐行核对后再确认执行。
      if (normalized.includes('\n') || normalized.includes('\r')) {
        handleHeaderModeChange('off')
        localInput.value = cmd
        ElMessage.info('多行命令已切回命令输入模式，请确认后按 Enter 执行')
        nextTick(() => {
          chatStore.clearTerminalInput()
          terminalInputRef.value?.focus()
        })
        return
      }

      // 单行命令直接注入远端 shell 当前命令行（不附加换行，等待用户按 Enter 确认）
      if (typeof chatStore.sendSSHInjectCommand === 'function') {
        chatStore.sendSSHInjectCommand(normalized)
      } else {
        chatStore.sendSSHRawInput?.(normalized)
      }
      nextTick(() => {
        chatStore.clearTerminalInput()
        focusInteractiveTerminal()
      })
      return
    }

    localInput.value = cmd
    nextTick(() => {
      chatStore.clearTerminalInput()
      terminalInputRef.value?.focus()
    })
  },
  { immediate: true },
)

// 监听连接状态变化
watch(isConnected, async (connected) => {
  if (!connected) {
    // 断开连接 → 停止录制 + 销毁 xterm 实例
    await recordingChain
    await stopRecording(recordingState)
    disposeTerminal()
    inputTracker.reset()
    lastHandledChunkSeq = 0
    lastExecutedCommand.value = ''
    return
  }
  // 连接成功 → 开始录制。工单只决定录像归档位置：
  // 有聊天工单优先用它，否则用日志收集绑定的人工工单；两者都没有则不归档
  // （SSH 仍可正常使用，避免把"没有工单"变成"不能登录"）。
  if (historyCaseId.value) {
    startRecording(recordingState, historyCaseId.value, {
      conversationId: chatStore.conversationId ?? undefined,
      diagnosticStage: chatStore.diagnosticStage ?? undefined,
    })
  }
  await nextTick()
  initTerminal()
  // double rAF：等待浏览器完成布局后再 fit，避免 Drawer 动画期间取到错误尺寸
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      fitTerminal()
      if (isInteractiveMode.value) {
        focusInteractiveTerminal()
      }
    })
  })
})

// 监听模式一 ↔ 模式二切换：原地切换 xterm 交互属性，保留历史滚屏并重新 fit 同步 PTY 尺寸
watch(isInteractiveMode, async (interactive) => {
  inputTracker.reset()
  applyXtermInteractionOptions(interactive)
  if (!isConnected.value) return
  await nextTick()
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      fitTerminal()
      if (interactive) {
        focusInteractiveTerminal()
      }
    })
  })
})

/**
 * 页签切回终端时重新 fit。
 *
 * 切到「文件下载」页签后终端被 v-show 隐藏，容器尺寸变成 0；
 * 再切回来时必须重新计算行列，否则列数会停留在隐藏前的值（表现为换行错位）。
 */
watch(
  () => chatStore.isRightPanelOpen && chatStore.rightPanelTab === 'terminal',
  async (visible) => {
    if (!visible || !isConnected.value) return
    await nextTick()
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        fitTerminal()
        if (isInteractiveMode.value) {
          focusInteractiveTerminal()
        }
      })
    })
  },
)

function applyXtermInteractionOptions(interactive: boolean) {
  if (!xterm || !xterm.options) return
  xterm.options.disableStdin = !interactive
  xterm.options.cursorBlink = interactive
  xterm.options.convertEol = !interactive
}

function focusInteractiveTerminal() {
  if (!isInteractiveMode.value || !isConnected.value || !xterm) return
  if (typeof xterm.focus === 'function') {
    xterm.focus()
  }
}

function handleStageClick() {
  if (!isInteractiveMode.value || !xterm) return
  // 正在拖选复制文本时不抢焦点，避免清掉用户刚选中的区域
  if (typeof xterm.hasSelection === 'function' && xterm.hasSelection()) return
  focusInteractiveTerminal()
}

async function copyTextToClipboard(text: string) {
  if (!text) return
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text)
    return
  }
  const ta = document.createElement('textarea')
  ta.value = text
  ta.style.cssText = 'position:fixed;opacity:0'
  document.body.appendChild(ta)
  ta.select()
  document.execCommand('copy')
  document.body.removeChild(ta)
}

async function pasteFromClipboardToInteractive() {
  if (!isInteractiveMode.value || !isConnected.value) return
  if (!navigator.clipboard || typeof navigator.clipboard.readText !== 'function') {
    ElMessage.warning('当前浏览器环境不支持快捷粘贴，请使用右键粘贴')
    return
  }
  try {
    const pasted = await navigator.clipboard.readText()
    if (pasted) {
      handleInteractiveData(pasted)
    }
  } catch {
    ElMessage.warning('无法读取剪贴板，请允许剪贴板权限或使用右键粘贴')
  }
}

/**
 * 模式二键盘与粘贴输入处理：
 * 1. 先从当前 xterm 屏幕缓冲区提取即将提交的完整命令（兼容 Tab 补全与方向键历史，并屏蔽密码提示）；
 * 2. 零延迟将原始字节透传远端 PTY；
 * 3. 有序刷盘前序输出并录制本次输入，使模式二与模式一共享完整的终端历史回放。
 */
function handleInteractiveData(data: string) {
  if (!isConnected.value || !isInteractiveMode.value || !data) return

  const recordedEvents = inputTracker.consume(data, xterm)
  chatStore.sendSSHRawInput?.(data)

  if (recordedEvents.length === 0) return

  for (const evt of recordedEvents) {
    if (!evt.isControl) {
      if (currentCommandOutput.value.trim()) {
        lastCommandOutput.value = currentCommandOutput.value
      }
      currentCommandOutput.value = ''
      hasExecutedCommand.value = true
      lastExecutedCommand.value = evt.command
    }

    if (recordingState.isRecording.value) {
      recordingChain = recordingChain
        .then(async () => {
          await forceFlushOutput(recordingState)
          await recordInput(recordingState, evt.command)
        })
        .catch((err) => {
          console.error('[TerminalPanel] 录制交互输入失败:', err)
        })
    }
  }
}

function initTerminal() {
  if (!terminalContainer.value || xterm) return

  const interactive = isInteractiveMode.value
  xterm = new Terminal({
    convertEol: !interactive,
    disableStdin: !interactive,
    // 等宽字体栈：优先现代终端字体，缺失时逐级回退，避免不同机器行高漂移
    fontFamily: "'JetBrains Mono', 'Cascadia Mono', Consolas, Monaco, 'Courier New', monospace",
    fontSize: 13,
    lineHeight: 1.3,
    cursorBlink: interactive,
    cursorStyle: 'block',
    theme: {
      background: '#1e1e1e',
      foreground: '#d4d4d4',
      cursor: '#d4d4d4',
      selectionBackground: 'rgba(64, 158, 255, 0.35)',
      black: '#1e1e1e',
      red: '#f56c6c',
      green: '#67c23a',
      yellow: '#e6a23c',
      blue: '#409eff',
      magenta: '#c678dd',
      cyan: '#56b6c2',
      white: '#d4d4d4',
      brightBlack: '#5c6370',
      brightRed: '#ff7b72',
      brightGreen: '#8bc34a',
      brightYellow: '#fbc02d',
      brightBlue: '#61afef',
      brightMagenta: '#d19a66',
      brightCyan: '#56b6c2',
      brightWhite: '#ffffff',
    },
    // 采集/安装日志动辄数百行，2000 行会把前面的安装告警挤出可见历史
    scrollback: 5000,
  })
  fitAddon = new FitAddon()
  xterm.loadAddon(fitAddon)
  xterm.open(terminalContainer.value)

  // 模式二实时键盘流绑定（防御检查：兼容测试 stub）
  if (typeof xterm.onData === 'function') {
    dataDisposable = xterm.onData((data) => {
      handleInteractiveData(data)
    })
  }

  // 主流终端快捷键习惯：
  // - Ctrl+Shift+C / 有选区时 Ctrl+C：复制选中内容（无选区时 Ctrl+C 透传 \x03 中断远端进程）
  // - Ctrl+Shift+V：粘贴剪贴板文本（不拦截纯 Ctrl+V，保留 vim 可视块模式）
  if (typeof xterm.attachCustomKeyEventHandler === 'function') {
    xterm.attachCustomKeyEventHandler((e: KeyboardEvent) => {
      if (!isInteractiveMode.value || e.type !== 'keydown') return true

      const key = e.key.toLowerCase()
      const hasModifier = e.ctrlKey || e.metaKey
      if (hasModifier && e.shiftKey && key === 'c') {
        const selection = typeof xterm?.getSelection === 'function' ? xterm.getSelection() : ''
        if (selection) {
          void copyTextToClipboard(selection)
          xterm?.clearSelection?.()
        }
        return false
      }
      if (hasModifier && !e.shiftKey && key === 'c') {
        const selection = typeof xterm?.getSelection === 'function' ? xterm.getSelection() : ''
        if (selection) {
          void copyTextToClipboard(selection)
          xterm?.clearSelection?.()
          return false
        }
        return true
      }
      if (hasModifier && e.shiftKey && key === 'v') {
        e.preventDefault()
        void pasteFromClipboardToInteractive()
        return false
      }
      return true
    })
  }

  fitTerminal()

  if (fullOutputText.value) {
    xterm.write(isInteractiveMode.value ? fullOutputText.value : normalizeTerminalText(fullOutputText.value))
  } else if (chatStore.sshOutputBuffer && chatStore.sshCommandConsumer !== 'collection') {
    // 刚建立 SSH 连接时，把远端已发出的登录欢迎语与初始 Shell 提示符渲染出来
    if (chatStore.sshTerminalOutputChunk?.seq) {
      lastHandledChunkSeq = chatStore.sshTerminalOutputChunk.seq
    }
    writeTerminalRaw(chatStore.sshOutputBuffer)
  }

  // 抽屉宽度现在会随三栏布局连续变化（拖拽 / 主区让位过渡），
  // 每帧都 fit 会让 xterm 反复 reflow 并闪烁；合并到 60ms 一次。
  resizeObserver = new ResizeObserver(() => {
    scheduleFit()
  })
  resizeObserver.observe(terminalContainer.value)
}

let fitTimer: number | null = null

/** 合并连续尺寸变化后再 fit，避免宽度动画期间的逐帧重排 */
function scheduleFit() {
  if (fitTimer !== null) window.clearTimeout(fitTimer)
  fitTimer = window.setTimeout(() => {
    fitTimer = null
    fitTerminal()
  }, 60)
}

/**
 * 计算并应用终端的列数与行数，并同步给 terminal_bridge 调整远端 PTY 尺寸。
 *
 * 为什么不能只调 `fitAddon.fit()`：xterm 的字符高度是浮点值，FitAddon 按浮点算行数，
 * 渲染时却按整像素铺行，实测内容会比容器高出一行的一部分（本机 779px vs 771px）。
 * 多出来的部分被 `overflow: hidden` 裁掉，观感就是"终端最后一行被输入区压住/切半"。
 * 这里 fit 之后核对一次实际高度，超出就少一行——少一行几乎不可见，被裁半行非常刺眼。
 */
function fitTerminal() {
  if (!fitAddon || !xterm || !terminalContainer.value) return
  fitAddon.fit()
  const host = terminalContainer.value.querySelector('.xterm') as HTMLElement | null
  const screen = terminalContainer.value.querySelector('.xterm-screen') as HTMLElement | null
  if (host && screen && xterm.rows > 2 && screen.offsetHeight > host.clientHeight) {
    xterm.resize(xterm.cols, xterm.rows - 1)
  }
  if (isConnected.value && xterm.cols >= 2 && xterm.rows >= 2) {
    chatStore.sendSSHResize?.(xterm.cols, xterm.rows)
  }
}

function disposeTerminal() {
  if (fitTimer !== null) {
    window.clearTimeout(fitTimer)
    fitTimer = null
  }
  if (dataDisposable) {
    dataDisposable.dispose()
    dataDisposable = null
  }
  if (resizeObserver) {
    resizeObserver.disconnect()
    resizeObserver = null
  }
  if (xterm) {
    xterm.dispose()
    xterm = null
  }
  fitAddon = null
}

function normalizeTerminalText(text: string): string {
  return text.replace(/\r?\n/g, '\r\n')
}

/** 写入来自远端 PTY 的原始输出流（模式二原样保留控制序列，模式一归一化换行） */
function writeTerminalRaw(text: string) {
  if (!text) return
  fullOutputText.value += text
  if (hasExecutedCommand.value) {
    currentCommandOutput.value += text
  }
  if (xterm) {
    xterm.write(isInteractiveMode.value ? text : normalizeTerminalText(text))
  }

  // Task 42: 录制输出（异步上传，不影响实时渲染）
  if (recordingState.isRecording.value) {
    recordOutput(recordingState, text)
  }
}

/** 写入前端本地生成的提示/命令回显（统一转 CRLF） */
function writeTerminal(text: string) {
  if (!text) return
  fullOutputText.value += text
  if (hasExecutedCommand.value) {
    currentCommandOutput.value += text
  }
  if (xterm) {
    xterm.write(normalizeTerminalText(text))
  }

  if (recordingState.isRecording.value) {
    recordOutput(recordingState, text)
  }
}

function writeInfoLine(text: string) {
  writeTerminal(`\n[Bridge] ${text}\n`)
}

function stripAnsiForText(text: string): string {
  if (!text) return ''

  // 仅用于复制/发送给助手时清理控制序列，终端渲染保留原始流。
  let cleaned = text
    .replace(/\x1b\][^\x07\x1b]*(\x07|\x1b\\)/g, '')
    .replace(/\x1b\[[0-9;?]*[ -/]*[@-~]/g, '')
    .replace(/\x1b[@-Z\\-_]/g, '')

  cleaned = cleaned
    .split('')
    .filter((ch) => {
      const code = ch.charCodeAt(0)
      return ch === '\n' || ch === '\r' || ch === '\t' || code >= 0x20
    })
    .join('')

  return cleaned.trim()
}

function latestOutputText(): string {
  const current = currentCommandOutput.value.trim()
  if (current) return stripAnsiForText(current)
  const prev = lastCommandOutput.value.trim()
  if (prev) return stripAnsiForText(prev)
  return stripAnsiForText(fullOutputText.value)
}

function disconnectSsh() {
  manualDisconnect.value = true
  inputTracker.reset()
  // Task 42: 停止录制（先刷新输出缓冲区）
  if (recordingState.isRecording.value) {
    forceFlushOutput(recordingState)
    stopRecording(recordingState)
  }
  chatStore.disconnectSSH()
  writeInfoLine('已断开 SSH 会话')
}

function executeCommand() {
  const command = localInput.value.trim()
  if (!command || !isConnected.value) return

  // Task 42: 刷新上一条命令的输出缓冲区
  if (recordingState.isRecording.value) {
    forceFlushOutput(recordingState)
  }

  if (currentCommandOutput.value.trim()) {
    lastCommandOutput.value = currentCommandOutput.value
  }
  currentCommandOutput.value = ''
  hasExecutedCommand.value = true

  writeTerminal(`\n$ ${command}\n`)

  // Task 42: 录制输入命令
  if (recordingState.isRecording.value) {
    recordInput(recordingState, command)
  }

  chatStore.sendSSHCommand(command, 'terminal')
  lastExecutedCommand.value = command
  localInput.value = ''
}

/**
 * 回填上一条已执行的命令（不自动执行）。
 *
 * 只回填不重发：终端命令直接打到客户生产节点上，重发前用户必须能看见内容、
 * 并有机会先改参数（多数场景是「上一条少个参数/换个节点」）。回填后聚焦输入框，
 * 用户按 Enter 即发，比手打一遍快，也不会绕过人的确认。
 */
function recallLastCommand() {
  if (!isConnected.value || !lastExecutedCommand.value) return
  localInput.value = lastExecutedCommand.value
  nextTick(() => {
    terminalInputRef.value?.focus()
  })
}

function handleKeydown(e: KeyboardEvent) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    executeCommand()
  }
}

function clearInput() {
  localInput.value = ''
}

function clearOutput() {
  fullOutputText.value = ''
  currentCommandOutput.value = ''
  lastCommandOutput.value = ''
  hasExecutedCommand.value = false
  inputTracker.reset()
  if (xterm) xterm.clear()
}

async function copyLatestOutput() {
  const text = latestOutputText()
  if (!text) return
  await copyTextToClipboard(text)
  writeInfoLine('本次输出已复制到剪贴板')
}

function sendLatestOutputToAssistant() {
  const text = latestOutputText()
  if (!text) return
  const prompt = `请基于以下终端输出分析问题并给出下一步排障建议：\n\n${text}`
  chatStore.setAssistantDraftText(prompt)
  writeInfoLine('本次输出已发送到助手输入框')
}

function closePanel() {
  disconnectSsh()
  chatStore.closeTerminalSidebar()
}

onMounted(() => {
  // 如果 SSH 已连接（如统一登录弹框：登录成功后右栏才挂载），
  // watch(isConnected) 不会触发（因为没有 transition），需在 onMounted 手动启动录制
  if (isConnected.value && historyCaseId.value) {
    startRecording(recordingState, historyCaseId.value, {
      conversationId: chatStore.conversationId ?? undefined,
      diagnosticStage: chatStore.diagnosticStage ?? undefined,
    })
  }
  // Drawer 可能正处于开启动画中，此时容器尺寸未稳定。
  // 用 double rAF 确保浏览器完成布局后再执行 fit。
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      initTerminal()
      fitTerminal()
      if (isInteractiveMode.value && isConnected.value) {
        focusInteractiveTerminal()
      }
    })
  })
})

onBeforeUnmount(() => {
  disconnectSsh()
  disposeTerminal()
})
</script>

<template>
  <div class="terminal-panel">
    <div class="terminal-header">
      <div class="header-left">
        <el-tag v-if="isConnected" size="small" type="success" effect="plain">已连接</el-tag>
        <el-tag v-else-if="isConnecting" size="small" type="warning" effect="plain">{{ connectingLabel }}</el-tag>
        <el-tag v-else-if="isError" size="small" type="danger" effect="plain">连接错误</el-tag>
        <el-tag v-else size="small" type="info" effect="plain">未连接</el-tag>
        <!-- 模式切换下拉（仅 SSH 已连接时显示）：前 3 项为模式一（配合排障 Agent），第 4 项为模式二（实时交互终端） -->
        <el-select
          v-if="isConnected"
          :model-value="selectedHeaderMode"
          size="small"
          class="auto-exec-select"
          placeholder="终端模式"
          @change="handleHeaderModeChange($event)"
        >
          <el-option value="off" label="关闭自动执行" />
          <el-option value="safe-only" label="安全模式" />
          <el-option value="aggressive" label="激进模式（有风险）" />
          <el-option value="interactive" label="交互终端（实时 Shell）" />
        </el-select>
      </div>
      <div class="header-actions">
        <el-button text size="small" @click="copyLatestOutput">复制本次输出</el-button>
        <el-button text size="small" @click="sendLatestOutputToAssistant">发送到助手</el-button>
        <el-button text size="small" @click="clearOutput">清空</el-button>
        <el-tooltip :content="historyHint" placement="top">
          <span>
            <el-button text size="small" @click="openTerminalHistory">
              <el-icon><VideoCamera /></el-icon>
              终端历史
            </el-button>
          </span>
        </el-tooltip>
        <el-button text size="small" @click="closePanel">
          <el-icon><Close /></el-icon>
        </el-button>
      </div>
    </div>

    <!-- ===== 连接区域：未连接时只要求登录，不再引导建工单 ===== -->
    <div v-if="!isConnected" class="ssh-connect-area">
      <el-button
        type="primary"
        size="large"
        :loading="checkingBridge || isConnecting"
        class="btn-connect-ssh"
        @click="handleOpenSshDialog"
      >
        <el-icon><Monitor /></el-icon>
        {{ checkingBridge ? '正在检测 Bridge...' : (isConnecting ? '正在连接...' : '连接 SSH') }}
      </el-button>
      <p v-if="isError" class="connect-error">{{ errorMessage }}</p>
      <p class="connect-hint">终端 / 文件下载 / 日志收集 / Agent 排障共用这一条 SSH 会话</p>
      <p v-if="!hasCaseContext" class="connect-hint connect-hint--weak">
        未绑定工单也可连接；终端录像与回放历史需有工单后归档
      </p>
    </div>

    <!-- 已连接 → 显示终端（模式二全屏交互、模式一只读配合底部输入框） -->
    <div v-else class="terminal-stage" :class="{ 'terminal-stage--interactive': isInteractiveMode }">
      <div
        ref="terminalContainer"
        class="terminal-canvas"
        :class="{ 'terminal-canvas--interactive': isInteractiveMode }"
        @click="handleStageClick"
      />
    </div>

    <!--
      文件下载已从终端底部拆出，成为右栏与「SSH 终端」平级的独立页签
      （见 App.vue 的右栏 rail 与 SftpPanel.vue）。终端这里不再嵌 SFTP 面板。
    -->

    <!-- 模式二底部精简交互状态栏：隐藏命令输入框，保留快捷键说明与断开入口 -->
    <div v-if="isConnected && isInteractiveMode" class="interactive-statusbar">
      <span class="interactive-statusbar__hint">
        实时交互模式 · Tab 补全 · ↑↓ 历史 · Ctrl+C 中断 · Ctrl+Shift+C/V 复制粘贴 · 自动录制回放
      </span>
      <el-link type="primary" :underline="false" @click="disconnectSsh">断开连接</el-link>
    </div>

    <!--
      模式一底部操作区：与日志收集、会话输入同构（hint 行 + 36px 控件行 + 按钮组），
      深色主题只换颜色，尺寸沿用 styles/panel-composer.css 的同一套变量。
    -->
    <div v-else class="panel-composer panel-composer--dark">
      <div class="panel-composer__hint">
        <span v-if="!isConnected">请先点击『连接 SSH』按钮</span>
        <span v-else>
          Enter 执行 · Shift+Enter 换行 ·
          <el-link
            type="primary"
            :underline="false"
            :disabled="!lastExecutedCommand"
            title="把上一条已执行的命令回填到输入框（不自动执行）"
            @click="recallLastCommand"
          >上条命令</el-link>
          ·
          <el-link type="primary" :underline="false" @click="disconnectSsh">断开连接</el-link>
        </span>
      </div>
      <div class="panel-composer__body">
        <span class="panel-composer__prefix">$</span>
        <el-input
          ref="terminalInputRef"
          v-model="localInput"
          type="textarea"
          :rows="1"
          placeholder="输入命令或从命令卡片发送..."
          class="panel-composer__field"
          @keydown="handleKeydown"
          :disabled="!isConnected"
        />
        <div class="panel-composer__actions">
          <el-button
            type="primary"
            :disabled="!localInput.trim() || !isConnected"
            @click="executeCommand"
          >
            执行
          </el-button>
          <el-button :disabled="!localInput" @click="clearInput">清空</el-button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.terminal-panel {
  background: #1e1e1e;
  border-radius: 8px;
  border: 1px solid #333;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  height: 100%;
  /* 原为 min-height: 400px：矮视口下面板撑破抽屉，底部输入区/提示会被裁掉。
     flex 链路已由 min-height: 0 保证可收缩，这里不再设下限。 */
  min-height: 0;
}

.terminal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  /* 抽屉宽度可拖到很窄（默认约 400px）：不换行会把"复制本次输出"等按钮顶出抽屉被裁掉 */
  flex-wrap: wrap;
  gap: 6px 8px;
  padding: 10px 12px;
  background: #2d2d2d;
  border-bottom: 1px solid #333;
}

.header-left {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-wrap: wrap;
  justify-content: flex-end;
  margin-left: auto;
}

.header-actions :deep(.el-button) {
  color: #9cdcfe;
  /* 五个动作挤在默认 400px 抽屉里：收紧内边距与字号，保证换行后仍是一行 */
  padding: 0 6px;
  font-size: 12px;
}

/* 终端模式 / 自动执行模式下拉 */
.auto-exec-select {
  width: 172px;
}

.auto-exec-select :deep(.el-input__wrapper) {
  background: #3a3a3a;
  box-shadow: none;
  border: 1px solid #555;
}

.auto-exec-select :deep(.el-input__inner) {
  color: #e0e0e0;
  font-size: 12px;
}

.ssh-connect-area {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  flex: 1;
  padding: 24px;
}

.btn-connect-ssh {
  min-width: 160px;
  font-size: 15px;
}

.connect-error {
  color: #f56c6c;
  font-size: 12px;
  margin: 0;
}

.connect-hint {
  color: #909399;
  font-size: 12px;
  margin: 0;
}

/* 次级说明：无工单时的录像归档限制，不能与主提示同等视觉权重 */
.connect-hint--weak {
  color: #6b6f76;
  font-size: 11px;
}

.terminal-stage {
  flex: 1;
  min-height: 0;           /* 允许 flex-item 缩小到内容以下，防止撑破父容器 */
  background: #1e1e1e;
  /* 四边都留安全边距：底部 padding 曾为 0，最后一行会贴着输入区，观感就是"被输入框压住" */
  padding: 12px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.terminal-stage--interactive {
  padding: 8px 10px;
}

.terminal-canvas {
  width: 100%;
  flex: 1;                 /* 填满 terminal-stage 的剩余高度，替代不稳定的 height: 100% */
  min-height: 0;           /* flex 子元素必须显式清零，否则 min-height: auto 会阻止收缩 */
  border: 1px solid #333;
  border-radius: 6px;
  overflow: hidden;
  box-sizing: border-box;
  padding: 10px 12px;
}

.terminal-canvas--interactive {
  cursor: text;
  border-color: #3f4b5b;
}

.terminal-canvas--interactive:focus-within {
  border-color: #409eff;
}

.terminal-canvas :deep(.xterm) {
  height: 100%;
}

/* 仅模式一隐藏光标；模式二（.terminal-canvas--interactive）恢复真实终端闪烁光标 */
.terminal-canvas:not(.terminal-canvas--interactive) :deep(.xterm-cursor),
.terminal-canvas:not(.terminal-canvas--interactive) :deep(.xterm-cursor-layer) {
  display: none !important;
}

/* 模式二底部精简状态条 */
.interactive-statusbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 6px 12px;
  padding: 6px 12px;
  background: #252526;
  border-top: 1px solid #333;
  font-size: 12px;
  color: #909399;
}

.interactive-statusbar__hint {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
  min-width: 0;
}

/*
 * 底部操作区高度/字号/按钮规格统一在 styles/panel-composer.css（全局）；
 * 深色配色也在那里（.panel-composer--dark），这里不再重复定义。
 */

/* 终端滚动条：默认样式在深色背景上过亮，收窄并降饱和 */
.terminal-canvas :deep(.xterm-viewport) {
  scrollbar-width: thin;
  scrollbar-color: #4a4a4a transparent;
}

.terminal-canvas :deep(.xterm-viewport)::-webkit-scrollbar {
  width: 8px;
}

.terminal-canvas :deep(.xterm-viewport)::-webkit-scrollbar-thumb {
  background: #4a4a4a;
  border-radius: 4px;
}

.terminal-canvas :deep(.xterm-viewport)::-webkit-scrollbar-thumb:hover {
  background: #5c5c5c;
}

@media (max-width: 768px) {
  .header-actions :deep(.el-button) {
    padding-left: 4px;
    padding-right: 4px;
    font-size: 12px;
  }

  .terminal-stage {
    min-height: 0;
  }
}
</style>
