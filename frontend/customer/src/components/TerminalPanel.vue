<script setup lang="ts">
import { ref, computed, watch, nextTick, onBeforeUnmount } from 'vue'
import { useChatStore } from '@/stores/chat'
import {
  createBridgeSocket,
  buildConnectMessage,
  buildInputMessage,
  buildDisconnectMessage,
  type TerminalWsMessage,
} from '@/api/terminal'

/**
 * TerminalPanel.vue  —  Bridge 模式
 *
 * SSH 连接完全在 Windows 本地发起：
 *   浏览器 → ws://localhost:9999 (terminal_bridge.exe) → HCI Linux
 *
 * 公网 Gateway 不参与任何 SSH 流量。
 */

const chatStore = useChatStore()

const terminalInputRef = ref<{ focus: () => void } | null>(null)
const localInput = ref('')
const bridgeSocket = ref<WebSocket | null>(null)
const manualDisconnect = ref(false)
const connectStage = ref<'idle' | 'bridge' | 'remote_session'>('idle')
const authTimeoutTimer = ref<number | null>(null)

// SSH 登录表单
const sshForm = ref({
  host: '',
  port: '22',
  username: '',
  password: '',
  authType: 'password' as 'password' | 'key',
  privateKey: '',
  passphrase: '',
})

// 终端输出
const terminalOutput = ref<
  Array<{ type: 'command' | 'output' | 'error' | 'info'; content: string }>
>([])
const outputContainer = ref<HTMLElement | null>(null)

// 连接状态（本地维护，不依赖 Gateway session）
const connectionState = ref<'disconnected' | 'connecting' | 'connected' | 'error'>('disconnected')
const errorMessage = ref('')
const errorDetail = ref('')

const isConnected = computed(() => connectionState.value === 'connected')
const isConnecting = computed(() => connectionState.value === 'connecting')
const isError = computed(() => connectionState.value === 'error')
const connectingLabel = computed(() => {
  if (connectStage.value === 'bridge') return '连接本地 Bridge...'
  if (connectStage.value === 'remote_session') return '建立远程 SSH 会话中...'
  return '连接中...'
})
const caseId = computed(() => chatStore.currentCase?.case_id || 'default')

// 同步状态到 store，供 header 等其他地方读取
watch(connectionState, (s) => chatStore.setSshConnectionState(s))
watch(errorMessage, (m) => {
  if (m) chatStore.setSshErrorMessage(m)
  else chatStore.clearSshErrorMessage()
})

// 监听 store 中的待发送命令（CommandBlock 触发）
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

// ── 工具函数 ─────────────────────────────────────────

function addLog(type: 'command' | 'output' | 'error' | 'info', content: string) {
  terminalOutput.value.push({ type, content })
  nextTick(() => {
    if (outputContainer.value) {
      outputContainer.value.scrollTop = outputContainer.value.scrollHeight
    }
  })
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

function closeSocket() {
  if (!bridgeSocket.value) return
  bridgeSocket.value.onopen = null
  bridgeSocket.value.onclose = null
  bridgeSocket.value.onmessage = null
  bridgeSocket.value.onerror = null
  bridgeSocket.value.close()
  bridgeSocket.value = null
}

function setErrorState(message: string, detail = '') {
  connectionState.value = 'error'
  connectStage.value = 'idle'
  errorMessage.value = message
  errorDetail.value = detail
}

function clearErrorState() {
  errorMessage.value = ''
  errorDetail.value = ''
}

function appendErrorLog(message: string, detail = '') {
  addLog('error', message)
  if (detail) {
    addLog('error', `原始 SSH 输出: ${detail}`)
  }
}

function clearAuthTimer() {
  if (authTimeoutTimer.value !== null) {
    window.clearTimeout(authTimeoutTimer.value)
    authTimeoutTimer.value = null
  }
}

function startAuthTimer() {
  clearAuthTimer()
  authTimeoutTimer.value = window.setTimeout(() => {
    if (connectionState.value !== 'connecting') return
    setErrorState('连接远程主机超时', '认证阶段在限定时间内未收到成功或失败信号')
    appendErrorLog(errorMessage.value, errorDetail.value)
    manualDisconnect.value = true
    closeSocket()
  }, 15000)
}

// ── Bridge WebSocket 处理 ────────────────────────────

function handleBridgeMessage(raw: string) {
  let msg: TerminalWsMessage
  try {
    msg = JSON.parse(raw)
  } catch {
    addLog('error', `无法解析消息: ${raw}`)
    return
  }

  if (msg.case_id && msg.case_id !== caseId.value) {
    return
  }

  if (msg.type === 'ssh_connected') {
    clearAuthTimer()
    connectionState.value = 'connected'
    connectStage.value = 'idle'
    clearErrorState()
    addLog('info', `SSH 已登录到 ${sshForm.value.host}`)
    return
  }

  if (msg.type === 'ssh_disconnected') {
    clearAuthTimer()
    connectStage.value = 'idle'
    if (connectionState.value !== 'error') {
      connectionState.value = 'disconnected'
      addLog('info', 'SSH 会话已断开')
    }
    return
  }

  if (msg.type === 'ssh_output' && msg.output) {
    addLog('output', msg.output)
    return
  }

  if (msg.type === 'ssh_error') {
    clearAuthTimer()
    setErrorState(msg.message || 'SSH 连接出错', msg.detail || '')
    appendErrorLog(errorMessage.value, errorDetail.value)
    return
  }
}

// ── SSH 操作 ─────────────────────────────────────────

async function connectSsh() {
  const err = validateForm()
  if (err) {
    errorMessage.value = err
    return
  }

  connectionState.value = 'connecting'
  connectStage.value = 'bridge'
  clearErrorState()
  manualDisconnect.value = false

  addLog('info', `正在连接 ${sshForm.value.username}@${sshForm.value.host}:${sshForm.value.port}...`)

  closeSocket()
  const socket = createBridgeSocket()
  bridgeSocket.value = socket

  socket.onopen = () => {
    connectStage.value = 'remote_session'
    addLog('info', '本地 SSH Bridge 已连接，正在建立远程 SSH 会话...')
    startAuthTimer()
    // 发送 SSH 连接请求到 Bridge
    socket.send(
      buildConnectMessage({
        host: sshForm.value.host.trim(),
        port: Number(sshForm.value.port) || 22,
        username: sshForm.value.username.trim(),
        auth_type: sshForm.value.authType,
        password: sshForm.value.authType === 'password' ? sshForm.value.password : undefined,
        private_key: sshForm.value.authType === 'key' ? sshForm.value.privateKey : undefined,
        passphrase:
          sshForm.value.authType === 'key' && sshForm.value.passphrase.trim()
            ? sshForm.value.passphrase.trim()
            : undefined,
        case_id: caseId.value,
      }),
    )
  }

  socket.onmessage = (e) => handleBridgeMessage(String(e.data || ''))

  socket.onerror = () => {
    clearAuthTimer()
    setErrorState('本地 SSH Bridge 未运行', '浏览器无法连接 ws://localhost:9999，请确认 terminal_bridge.exe 已启动')
    appendErrorLog(errorMessage.value, errorDetail.value)
    closeSocket()
  }

  socket.onclose = () => {
    clearAuthTimer()
    connectStage.value = 'idle'
    bridgeSocket.value = null
    if (!manualDisconnect.value && connectionState.value !== 'disconnected' && connectionState.value !== 'error') {
      setErrorState('本地 SSH Bridge 连接中断', 'Bridge 与浏览器之间的 WebSocket 已关闭')
      appendErrorLog(errorMessage.value, errorDetail.value)
    }
  }
}

function disconnectSsh() {
  manualDisconnect.value = true
  clearAuthTimer()
  if (bridgeSocket.value?.readyState === WebSocket.OPEN) {
    bridgeSocket.value.send(buildDisconnectMessage(caseId.value))
  }
  closeSocket()
  connectStage.value = 'idle'
  connectionState.value = 'disconnected'
  clearErrorState()
  addLog('info', '已断开 SSH 会话')
}

function executeCommand() {
  const command = localInput.value.trim()
  if (!command || !isConnected.value || bridgeSocket.value?.readyState !== WebSocket.OPEN) return

  addLog('command', command)
  bridgeSocket.value!.send(
    buildInputMessage(caseId.value, command.endsWith('\n') ? command : `${command}\n`),
  )
  localInput.value = ''
}

function handleKeydown(e: KeyboardEvent) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    executeCommand()
  }
}

function clearInput() { localInput.value = '' }
function clearOutput() { terminalOutput.value = [] }

async function copyOutput() {
  const text = terminalOutput.value.map((i) => i.content).join('\n')
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
  addLog('info', '输出已复制到剪贴板')
}

function closePanel() {
  disconnectSsh()
  chatStore.closeTerminalSidebar()
}

onBeforeUnmount(() => {
  clearAuthTimer()
  closeSocket()
})
</script>

<template>
  <div class="terminal-panel">
    <!-- 头部 -->
    <div class="terminal-header">
      <div class="header-left">
        <span class="header-title">SSH 终端</span>
        <el-tag v-if="isConnected" size="small" type="success" effect="plain">已登录</el-tag>
        <el-tag v-else-if="isConnecting" size="small" type="warning" effect="plain">{{ connectingLabel }}</el-tag>
        <el-tag v-else-if="isError" size="small" type="danger" effect="plain">连接错误</el-tag>
        <el-tag v-else size="small" type="info" effect="plain">未连接</el-tag>
      </div>
      <div class="header-actions">
        <el-button text size="small" @click="copyOutput" :disabled="terminalOutput.length === 0">复制</el-button>
        <el-button text size="small" @click="clearOutput">清空</el-button>
        <el-button text size="small" @click="closePanel">✕</el-button>
      </div>
    </div>

    <!-- 登录表单（未连接时） -->
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
          :description="errorDetail || undefined"
          :closable="true"
          @close="clearErrorState"
        />
      </el-form>
    </div>

    <!-- 终端输出区（已连接时） -->
    <div v-else ref="outputContainer" class="terminal-output">
      <div
        v-for="(item, idx) in terminalOutput"
        :key="idx"
        class="output-line"
        :class="`output-${item.type}`"
      >
        <span v-if="item.type === 'command'" class="output-prompt">$</span>
        <span class="output-content">{{ item.content }}</span>
      </div>
      <div v-if="terminalOutput.length === 0" class="output-empty">
        <p>SSH 已登录，等待命令...</p>
        <p class="empty-sub">输入命令并按 Enter 执行</p>
      </div>
    </div>

    <!-- 输入区 -->
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
        class="execute-btn"
        :disabled="!localInput.trim() || !isConnected"
        @click="executeCommand"
      >
        执行
      </el-button>
      <el-button size="small" :disabled="!localInput" @click="clearInput">清空</el-button>
    </div>

    <!-- 底部提示 -->
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

.header-left { display: flex; align-items: center; gap: 8px; }
.header-title { font-size: 14px; font-weight: 600; color: #e0e0e0; }
.header-actions { display: flex; align-items: center; gap: 4px; }
.header-actions :deep(.el-button) { color: #9cdcfe; }

.ssh-login-form {
  padding: 16px;
  background: #252526;
  border-bottom: 1px solid #333;
}

.ssh-login-form :deep(.el-form-item) { margin-bottom: 12px; }
.ssh-login-form :deep(.el-form-item__label) { color: #9cdcfe; font-size: 13px; }
.ssh-login-form :deep(.el-input__inner) { background: #1e1e1e; border-color: #444; color: #d4d4d4; }
.ssh-login-form :deep(.el-input__inner:focus) { border-color: #409eff; }
.ssh-login-form :deep(.el-textarea__inner) {
  background: #1e1e1e; border-color: #444; color: #d4d4d4;
  font-family: 'Consolas', 'Monaco', monospace;
}

.terminal-output {
  flex: 1;
  padding: 12px;
  overflow-y: auto;
  background: #1e1e1e;
  font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
  font-size: 13px;
  line-height: 1.6;
  min-height: 200px;
}

.output-line { display: flex; align-items: flex-start; margin-bottom: 4px; }
.output-prompt { color: #67c23a; font-weight: 600; margin-right: 8px; min-width: 16px; }
.output-command .output-content { color: #d4d4d4; }
.output-output .output-content { color: #9cdcfe; }
.output-error .output-content { color: #f56c6c; }
.output-info .output-content { color: #909399; font-style: italic; }

.output-empty {
  display: flex; flex-direction: column; align-items: center;
  justify-content: center; height: 100%; color: #666; text-align: center;
}
.empty-sub { font-size: 12px; color: #555; margin-top: 4px; }

.terminal-input-area {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 10px 12px;
  background: #252526;
  border-top: 1px solid #333;
}

.prompt-symbol {
  display: flex; align-items: center; height: 32px;
  color: #67c23a; font-weight: 600;
  font-family: 'Consolas', 'Monaco', monospace; font-size: 14px;
}

.terminal-input { flex: 1; }
.terminal-input :deep(.el-textarea__inner) {
  background: #1e1e1e; border-color: #444; color: #d4d4d4;
  font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
  font-size: 13px; line-height: 1.5; padding: 6px 10px;
}
.terminal-input :deep(.el-textarea__inner:focus) { border-color: #409eff; }
.terminal-input :deep(.el-textarea__inner:disabled) { opacity: 0.6; cursor: not-allowed; }
.terminal-input :deep(.el-textarea__inner::placeholder) { color: #666; }

.execute-btn { height: 32px; padding: 0 12px; }

.terminal-footer {
  display: flex; align-items: center; gap: 6px;
  padding: 6px 12px; background: #2d2d2d;
  border-top: 1px solid #333; font-size: 12px; color: #666;
}
.terminal-footer :deep(.el-link) { font-size: 12px; margin-left: 4px; }
</style>
