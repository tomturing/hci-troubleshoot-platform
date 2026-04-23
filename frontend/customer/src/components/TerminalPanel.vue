<script setup lang="ts">
/**
 * TerminalPanel.vue
 * 三态按钮实现：
 * - 状态A：无工单 + 无SSH → "连接SSH并创建工单" → 打开 CaseCreateDialog
 * - 状态B：有工单 + 无SSH → "连接SSH" → 打开 SshConnectDialog
 * - 状态C：已连接 → 显示 xterm 终端
 */
import { ref, computed, watch, nextTick, onMounted, onBeforeUnmount } from 'vue'
import { Terminal } from 'xterm'
import { FitAddon } from 'xterm-addon-fit'
import 'xterm/css/xterm.css'

import { useChatStore } from '@/stores/chat'
import { checkBridgeBeforeOpen } from '@/api/terminal'

const chatStore = useChatStore()

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
  if (!connected) return
  await nextTick()
  initTerminal()
  if (fitAddon) fitAddon.fit()
})

function initTerminal() {
  if (!terminalContainer.value || xterm) return

  xterm = new Terminal({
    convertEol: true,
    disableStdin: true,
    fontFamily: 'Consolas, Monaco, monospace',
    fontSize: 13,
    lineHeight: 1.35,
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
    scrollback: 2000,
  })
  fitAddon = new FitAddon()
  xterm.loadAddon(fitAddon)
  xterm.open(terminalContainer.value)
  fitAddon.fit()

  if (fullOutputText.value) {
    xterm.write(normalizeTerminalText(fullOutputText.value))
  }

  resizeObserver = new ResizeObserver(() => {
    if (fitAddon) fitAddon.fit()
  })
  resizeObserver.observe(terminalContainer.value)
}

function disposeTerminal() {
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
  chatStore.disconnectSSH()
  writeInfoLine('已断开 SSH 会话')
}

function executeCommand() {
  const command = localInput.value.trim()
  if (!command || !isConnected.value) return

  if (currentCommandOutput.value.trim()) {
    lastCommandOutput.value = currentCommandOutput.value
  }
  currentCommandOutput.value = ''
  hasExecutedCommand.value = true

  writeTerminal(`\n$ ${command}\n`)
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
  initTerminal()
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
        <span class="header-title">SSH 终端</span>
        <el-tag v-if="isConnected" size="small" type="success" effect="plain">已连接</el-tag>
        <el-tag v-else-if="isConnecting" size="small" type="warning" effect="plain">{{ connectingLabel }}</el-tag>
        <el-tag v-else-if="isError" size="small" type="danger" effect="plain">连接错误</el-tag>
        <el-tag v-else size="small" type="info" effect="plain">未连接</el-tag>
      </div>
      <div class="header-actions">
        <el-button text size="small" @click="copyLatestOutput">复制本次输出</el-button>
        <el-button text size="small" @click="sendLatestOutputToAssistant">发送到助手</el-button>
        <el-button text size="small" @click="clearOutput">清空</el-button>
        <el-button text size="small" @click="closePanel">✕</el-button>
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
        {{ checkingBridge ? '正在检测 Bridge...' : '🖥 连接 SSH 并创建工单' }}
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
        {{ checkingBridge ? '正在检测 Bridge...' : (isConnecting ? '正在连接...' : '🖥 连接 SSH') }}
      </el-button>
      <p v-if="isError" class="connect-error">{{ errorMessage }}</p>
      <p class="connect-hint">点击后检测 Bridge 并打开 SSH 连接弹框</p>
    </div>

    <!-- 状态C：已连接 → 显示终端 -->
    <div v-if="terminalState === 'C'" class="terminal-stage">
      <div ref="terminalContainer" class="terminal-canvas" />
    </div>

    <div class="terminal-input-area">
      <span class="prompt-symbol">$</span>
      <el-input
        ref="terminalInputRef"
        v-model="localInput"
        type="textarea"
        :autosize="{ minRows: 1, maxRows: 4 }"
        placeholder="输入命令或从命令卡片发送..."
        class="terminal-input"
        @keydown="handleKeydown"
        :disabled="!isConnected"
      />
      <el-button
        type="primary"
        size="small"
        class="execute-btn terminal-action-btn"
        :disabled="!localInput.trim() || !isConnected"
        @click="executeCommand"
      >
        执行
      </el-button>
      <el-button size="small" class="terminal-action-btn" :disabled="!localInput" @click="clearInput">清空</el-button>
    </div>

    <div class="terminal-footer">
      <span v-if="!isConnected">请先点击『连接 SSH』按钮</span>
      <span v-else>
        Enter 执行 · Shift+Enter 换行 ·
        <el-link type="primary" :underline="false" @click="disconnectSsh">断开连接</el-link>
      </span>
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
  min-height: 400px;
}

.terminal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 12px;
  background: #2d2d2d;
  border-bottom: 1px solid #333;
}

.header-left {
  display: flex;
  align-items: center;
  gap: 8px;
}

.header-title {
  font-size: 14px;
  font-weight: 600;
  color: #e0e0e0;
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 4px;
}

.header-actions :deep(.el-button) {
  color: #9cdcfe;
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
  min-height: 240px;
  background: #1e1e1e;
  padding: 10px 10px 0 10px;
}

.terminal-canvas {
  width: 100%;
  height: 100%;
  min-height: 230px;
  border: 1px solid #333;
  border-radius: 6px;
  overflow: hidden;
}

.terminal-canvas :deep(.xterm) {
  height: 100%;
  padding: 8px;
}

.terminal-canvas :deep(.xterm-cursor),
.terminal-canvas :deep(.xterm-cursor-layer) {
  display: none !important;
}

.terminal-input-area {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 10px 12px;
  background: #252526;
  border-top: 1px solid #333;
}

.prompt-symbol {
  display: flex;
  align-items: center;
  height: 32px;
  color: #67c23a;
  font-weight: 600;
  font-family: Consolas, Monaco, monospace;
  font-size: 14px;
}

.terminal-input {
  flex: 1;
}

.terminal-input :deep(.el-textarea__inner) {
  background: #1e1e1e;
  border-color: #444;
  color: #d4d4d4;
  font-family: Consolas, Monaco, monospace;
}

.execute-btn {
  height: 32px;
}

.terminal-action-btn {
  width: 64px;
  height: 32px;
  padding: 0;
}

.terminal-footer {
  padding: 6px 12px;
  border-top: 1px solid #333;
  background: #1f1f1f;
  color: #909399;
  font-size: 12px;
}

@media (max-width: 768px) {
  .header-actions :deep(.el-button) {
    padding-left: 4px;
    padding-right: 4px;
    font-size: 12px;
  }

  .terminal-stage {
    min-height: 200px;
  }
}
</style>