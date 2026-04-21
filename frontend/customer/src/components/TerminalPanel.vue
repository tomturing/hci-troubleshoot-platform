<script setup lang="ts">
import { ref, computed, watch, nextTick, onMounted, onBeforeUnmount } from 'vue'
import { Terminal } from 'xterm'
import { FitAddon } from 'xterm-addon-fit'
import 'xterm/css/xterm.css'

import { useChatStore } from '@/stores/chat'

const chatStore = useChatStore()

const terminalInputRef = ref<{ focus: () => void } | null>(null)
const terminalContainer = ref<HTMLElement | null>(null)

const localInput = ref('')
const manualDisconnect = ref(false)

const sshForm = ref({
  host: '',
  port: '22',
  username: '',
  password: '',
  authType: 'password' as 'password' | 'key',
  privateKey: '',
  passphrase: '',
})

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

function validateForm(): string | null {
  if (!sshForm.value.host.trim()) return '请填写主机地址'
  if (!sshForm.value.username.trim()) return '请填写用户名'
  const port = Number(sshForm.value.port)
  if (!Number.isInteger(port) || port < 1 || port > 65535) return '端口范围应为 1-65535'
  if (sshForm.value.authType === 'password' && !sshForm.value.password) return '请填写密码'
  if (sshForm.value.authType === 'key' && !sshForm.value.privateKey.trim()) return '请填写私钥'
  return null
}

// 使用全局 SSH 连接
async function connectSsh() {
  const err = validateForm()
  if (err) {
    // 直接显示错误，不再用 errorMessage ref
    return
  }

  manualDisconnect.value = false
  fullOutputText.value = ''
  currentCommandOutput.value = ''
  lastCommandOutput.value = ''
  hasExecutedCommand.value = false
  if (xterm) xterm.clear()

  writeInfoLine(`正在连接 ${sshForm.value.username}@${sshForm.value.host}:${sshForm.value.port}...`)

  try {
    await chatStore.connectSSH({
      host: sshForm.value.host.trim(),
      port: Number(sshForm.value.port) || 22,
      username: sshForm.value.username.trim(),
      authType: sshForm.value.authType,
      password: sshForm.value.authType === 'password' ? sshForm.value.password : undefined,
      privateKey: sshForm.value.authType === 'key' ? sshForm.value.privateKey : undefined,
      passphrase: sshForm.value.authType === 'key' && sshForm.value.passphrase.trim()
        ? sshForm.value.passphrase.trim()
        : undefined,
      caseId: caseId.value,
    })

    writeInfoLine(`SSH 已登录到 ${sshForm.value.host}`)
  } catch (e: any) {
    writeInfoLine(`错误: ${e.message}`)
  }
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

    <div v-if="!isConnected" class="ssh-login-form">
      <el-form :model="sshForm" label-width="70px" size="small">
        <el-form-item label="主机">
          <el-input v-model="sshForm.host" placeholder="192.168.1.100" />
        </el-form-item>
        <el-form-item label="端口">
          <el-input v-model="sshForm.port" placeholder="22" style="width: 100px" />
        </el-form-item>
        <el-form-item label="用户名">
          <el-input v-model="sshForm.username" placeholder="root" />
        </el-form-item>
        <el-form-item label="认证方式">
          <el-radio-group v-model="sshForm.authType" size="small">
            <el-radio-button label="password">密码</el-radio-button>
            <el-radio-button label="key">密钥</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item v-if="sshForm.authType === 'password'" label="密码">
          <el-input v-model="sshForm.password" type="password" placeholder="请输入密码" show-password />
        </el-form-item>
        <template v-else>
          <el-form-item label="私钥">
            <el-input
              v-model="sshForm.privateKey"
              type="textarea"
              :rows="4"
              placeholder="-----BEGIN RSA PRIVATE KEY-----&#10;..."
            />
          </el-form-item>
          <el-form-item label="密钥密码">
            <el-input v-model="sshForm.passphrase" type="password" placeholder="可选" show-password />
          </el-form-item>
        </template>
        <el-form-item>
          <el-button type="primary" size="small" @click="connectSsh" :loading="isConnecting">连接</el-button>
          <el-button size="small" @click="() => Object.assign(sshForm, { host: '', port: '22', username: '', password: '', privateKey: '', passphrase: '' })">重置</el-button>
        </el-form-item>
        <el-alert
          v-if="errorMessage"
          type="error"
          :title="errorMessage"
          :closable="true"
        />
      </el-form>
    </div>

    <div v-else class="terminal-stage">
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
      <span v-if="!isConnected">请先填写 SSH 信息并连接</span>
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

.ssh-login-form {
  padding: 16px;
  background: #252526;
  border-bottom: 1px solid #333;
}

.ssh-login-form :deep(.el-form-item) {
  margin-bottom: 12px;
}

.ssh-login-form :deep(.el-form-item__label) {
  color: #9cdcfe;
  font-size: 13px;
}

.ssh-login-form :deep(.el-input__inner) {
  background: #1e1e1e;
  border-color: #444;
  color: #d4d4d4;
}

.ssh-login-form :deep(.el-input__inner:focus) {
  border-color: #409eff;
}

.ssh-login-form :deep(.el-textarea__inner) {
  background: #1e1e1e;
  border-color: #444;
  color: #d4d4d4;
  font-family: Consolas, Monaco, monospace;
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
