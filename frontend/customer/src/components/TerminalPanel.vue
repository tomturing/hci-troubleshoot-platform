<script setup lang="ts">
/**
 * TerminalPanel.vue
 * 三态按钮实现：
 * - 状态A：无工单 + 无SSH → "连接SSH并创建工单" → 打开 CaseCreateDialog
 * - 状态B：有工单 + 无SSH → "连接SSH" → 打开 SshConnectDialog
 * - 状态C：已连接 → 显示 xterm 终端
 *
 * Task 42: 终端操作录制功能
 * - 命令输入和输出回显自动录制并上传数据库
 * - 用于排障复盘和审计回放
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
  stripAnsi,
} from '@/api/terminal-recording'

const chatStore = useChatStore()
const emit = defineEmits<{ 'open-terminal-history': [] }>()

// ===== 终端操作录制状态 =====
const recordingState = createRecordingState()

const terminalInputRef = ref<{ focus: () => void } | null>(null)
const terminalContainer = ref<HTMLElement | null>(null)

const localInput = ref('')
const manualDisconnect = ref(false)

// ===== 三态判断 =====
const terminalState = computed(() => {
  if (chatStore.sshConnectionState === 'connected') return 'C'  // 已连接
  if (chatStore.currentCase) return 'B'                         // 有工单，未连接
  return 'A'                                                      // 无工单，无连接
})

// ===== 前置检测 loading =====
const checkingBridge = ref(false)

async function handleConnectAndCreate() {
  // 状态A：打开 CaseCreateDialog
  checkingBridge.value = true
  try {
    const status = await checkBridgeBeforeOpen()
    chatStore.caseCreateDialogBridgeStatus = status
    chatStore.showCaseTemplate = true
  } finally {
    checkingBridge.value = false
  }
}

async function handleOpenSshDialog() {
  // 状态B：打开 SshConnectDialog
  checkingBridge.value = true
  try {
    const status = await checkBridgeBeforeOpen()
    chatStore.sshConnectDialogBridgeStatus = status
    chatStore.openSshFlowDialog(chatStore.currentCase?.case_id || null, 'terminal-only')
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
const caseId = computed(() => chatStore.currentCase?.case_id || 'default')

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

// 监听全局 SSH 输出事件
watch(
  () => chatStore.sshTerminalOutputEvent,
  (output) => {
    if (output && chatStore.sshCommandConsumer === 'terminal') {
      writeTerminal(output)
    }
  },
)

// 监听 bridge 提示（如"输入无法路由到会话"）：写进终端，避免出现"点了毫无反应"的观感。
// 与 ssh_error 严格区分——这里只是提示，不改变连接状态。
watch(
  () => chatStore.sshTerminalNotice,
  (notice) => {
    if (!notice?.text) return
    writeTerminal(`\n[Bridge] ${notice.text}\n`)
  },
)

// 监听命令注入
watch(
  () => chatStore.terminalInputCommand,
  (cmd) => {
    if (cmd) {
      localInput.value = cmd
      nextTick(() => {
        chatStore.clearTerminalInput()
        terminalInputRef.value?.focus()
      })
    }
  },
  { immediate: true },
)

// 监听连接状态变化
watch(isConnected, async (connected) => {
  if (!connected) {
    // 断开连接 → 停止录制 + 销毁 xterm 实例
    await stopRecording(recordingState)
    disposeTerminal()
    return
  }
  // 连接成功 → 开始录制
  if (chatStore.currentCase?.case_id) {
    startRecording(recordingState, chatStore.currentCase.case_id, {
      conversationId: chatStore.conversationId ?? undefined,
    })
  }
  await nextTick()
  initTerminal()
  // double rAF：等待浏览器完成布局后再 fit，避免 Drawer 动画期间取到错误尺寸
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      fitTerminal()
    })
  })
})

function initTerminal() {
  if (!terminalContainer.value || xterm) return

  xterm = new Terminal({
    convertEol: true,
    disableStdin: true,
    // 等宽字体栈：优先现代终端字体，缺失时逐级回退，避免不同机器行高漂移
    fontFamily: "'JetBrains Mono', 'Cascadia Mono', Consolas, Monaco, 'Courier New', monospace",
    fontSize: 13,
    lineHeight: 1.3,
    cursorBlink: false,
    theme: {
      background: '#1e1e1e',
      foreground: '#d4d4d4',
      cursor: '#d4d4d4',
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
  fitTerminal()

  if (fullOutputText.value) {
    xterm.write(normalizeTerminalText(fullOutputText.value))
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
 * 计算并应用终端的列数与行数。
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
  if (!host || !screen || xterm.rows <= 2) return
  if (screen.offsetHeight > host.clientHeight) {
    xterm.resize(xterm.cols, xterm.rows - 1)
  }
}

function disposeTerminal() {
  if (fitTimer !== null) {
    window.clearTimeout(fitTimer)
    fitTimer = null
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

function writeTerminal(text: string) {
  if (!text) return
  fullOutputText.value += text
  if (hasExecutedCommand.value) {
    currentCommandOutput.value += text
  }
  if (xterm) {
    xterm.write(normalizeTerminalText(text))
  }

  // Task 42: 录制输出（异步上传，不影响实时渲染）
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
  localInput.value = ''
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
  if (xterm) xterm.clear()
}

async function copyLatestOutput() {
  const text = latestOutputText()
  if (!text) return
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text)
  } else {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.style.cssText = 'position:fixed;opacity:0'
    document.body.appendChild(ta)
    ta.select()
    document.execCommand('copy')
    document.body.removeChild(ta)
  }
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
  // 如果 SSH 已连接（如 CaseCreateDialog 流程：SSH 先连接再打开 Drawer），
  // watch(isConnected) 不会触发（因为没有 transition），需在 onMounted 手动启动录制
  if (isConnected.value && chatStore.currentCase?.case_id) {
    startRecording(recordingState, chatStore.currentCase.case_id, {
      conversationId: chatStore.conversationId ?? undefined,
    })
  }
  // Drawer 可能正处于开启动画中，此时容器尺寸未稳定。
  // 用 double rAF 确保浏览器完成布局后再执行 fit。
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      initTerminal()
      if (fitAddon) fitAddon.fit()
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
        <!-- 自动执行模式开关（仅 SSH 已连接时显示） -->
        <el-select
          v-if="isConnected"
          :model-value="chatStore.autoExecuteMode"
          size="small"
          class="auto-exec-select"
          placeholder="自动执行"
          @change="chatStore.setAutoExecuteMode($event)"
        >
          <el-option value="off" label="关闭自动执行" />
          <el-option value="safe-only" label="安全模式" />
          <el-option value="aggressive" label="激进模式（有风险）" />
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

    <!-- ===== 三态连接区域 ===== -->
    <!-- 状态A：无工单 + 无SSH -->
    <div v-if="terminalState === 'A'" class="ssh-connect-area">
      <el-button
        type="primary"
        size="large"
        :loading="checkingBridge"
        class="btn-connect-ssh"
        @click="handleConnectAndCreate"
      >
        <el-icon><Monitor /></el-icon>
        {{ checkingBridge ? '正在检测 Bridge...' : '连接 SSH 并创建工单' }}
      </el-button>
      <p class="connect-hint">点击后检测 Bridge 并打开工单创建弹框</p>
    </div>

    <!-- 状态B：有工单 + 无SSH -->
    <div v-if="terminalState === 'B'" class="ssh-connect-area">
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
      <p class="connect-hint">点击后检测 Bridge 并打开 SSH 连接弹框</p>
    </div>

    <!-- 状态C：已连接 → 显示终端 -->
    <div v-if="terminalState === 'C'" class="terminal-stage">
      <div ref="terminalContainer" class="terminal-canvas" />
    </div>

    <!--
      底部操作区：与日志收集、会话输入同构（hint 行 + 36px 控件行 + 按钮组），
      深色主题只换颜色，尺寸沿用 styles/panel-composer.css 的同一套变量。
    -->
    <div class="panel-composer panel-composer--dark">
      <div class="panel-composer__hint">
        <span v-if="!isConnected">请先点击『连接 SSH』按钮</span>
        <span v-else>
          Enter 执行 · Shift+Enter 换行 ·
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

/* 自动执行模式下拉 */
.auto-exec-select {
  width: 148px;
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

.terminal-canvas :deep(.xterm) {
  height: 100%;
}

.terminal-canvas :deep(.xterm-cursor),
.terminal-canvas :deep(.xterm-cursor-layer) {
  display: none !important;
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