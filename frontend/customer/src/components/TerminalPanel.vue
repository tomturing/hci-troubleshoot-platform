<script setup lang="ts">
import { ref, computed, watch, nextTick, onBeforeUnmount } from 'vue'
import { useChatStore } from '@/stores/chat'
import { buildTerminalWsUrl, type TerminalSessionCreateRequest, type TerminalWsMessage } from '@/api/terminal'
import { getClientId } from '@/utils/clientId'

/**
 * 终端面板组件
 * 提供 SSH 登录、WebSocket 交互终端、命令输入与输出展示
 */

const chatStore = useChatStore()
const clientId = getClientId()

const terminalInput = ref<{ focus: () => void } | null>(null)
const localInput = ref('')
const terminalSocket = ref<WebSocket | null>(null)
const manualDisconnect = ref(false)

// SSH 登录表单状态
const sshForm = ref({
  host: '',
  port: '22',
  username: '',
  password: '',
  authType: 'password' as 'password' | 'key',
  privateKey: '',
  passphrase: '',
})

// 终端输出内容
const terminalOutput = ref<Array<{ type: 'command' | 'output' | 'error' | 'info'; content: string; timestamp: Date }>>([])

// 自动滚动控制
const outputContainer = ref<HTMLElement | null>(null)
const autoScroll = ref(true)

// 计算属性：SSH 连接状态（用于模板）
const isSshDisconnected = computed(() => chatStore.sshConnectionState === 'disconnected')
const isSshConnecting = computed(() => chatStore.sshConnectionState === 'connecting')
const isSshConnected = computed(() => chatStore.sshConnectionState === 'connected')
const isSshError = computed(() => chatStore.sshConnectionState === 'error')
const isSocketReady = computed(() => terminalSocket.value?.readyState === WebSocket.OPEN)

// 监听 store 中的待发送命令
watch(
  () => chatStore.terminalInputCommand,
  (newCommand) => {
    if (newCommand) {
      localInput.value = newCommand
      nextTick(() => {
        chatStore.clearTerminalInput()
        terminalInput.value?.focus()
      })
    }
  },
  { immediate: true },
)

// 监听连接状态变化，记录日志
watch(
  () => chatStore.sshConnectionState,
  (newState) => {
    const stateMap = {
      disconnected: '已断开',
      connecting: '连接中...',
      connected: '已连接',
      error: '连接错误',
    }
    addTerminalMessage('info', `SSH 连接状态：${stateMap[newState]}`)
  },
)

/**
 * 添加终端消息
 */
function addTerminalMessage(type: 'command' | 'output' | 'error' | 'info', content: string) {
  terminalOutput.value.push({
    type,
    content,
    timestamp: new Date(),
  })

  nextTick(() => {
    if (autoScroll.value && outputContainer.value) {
      outputContainer.value.scrollTop = outputContainer.value.scrollHeight
    }
  })
}

/**
 * 提取错误消息
 */
function extractErrorMessage(error: unknown): string {
  const maybeError = error as {
    response?: { data?: { detail?: string } }
    message?: string
  }
  return maybeError?.response?.data?.detail || maybeError?.message || '请求失败'
}

/**
 * 构建会话创建请求
 */
function buildCreateRequest(): Omit<TerminalSessionCreateRequest, 'client_id' | 'case_id'> {
  const payload: Omit<TerminalSessionCreateRequest, 'client_id' | 'case_id'> = {
    host: sshForm.value.host.trim(),
    port: Number(sshForm.value.port) || 22,
    username: sshForm.value.username.trim(),
    auth_type: sshForm.value.authType,
  }

  if (sshForm.value.authType === 'password') {
    payload.password = sshForm.value.password
  } else {
    payload.private_key = sshForm.value.privateKey
    if (sshForm.value.passphrase.trim()) {
      payload.passphrase = sshForm.value.passphrase.trim()
    }
  }

  return payload
}

/**
 * 校验登录参数
 */
function validateForm(): string | null {
  if (!sshForm.value.host.trim()) return '请填写主机地址'
  if (!sshForm.value.username.trim()) return '请填写用户名'

  const port = Number(sshForm.value.port)
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    return '端口范围应为 1-65535'
  }

  if (sshForm.value.authType === 'password' && !sshForm.value.password) {
    return '密码认证方式必须填写密码'
  }

  if (sshForm.value.authType === 'key' && !sshForm.value.privateKey.trim()) {
    return '密钥认证方式必须填写私钥'
  }

  return null
}

/**
 * 关闭当前 WebSocket 连接
 */
function closeSocket() {
  if (!terminalSocket.value) return
  terminalSocket.value.onopen = null
  terminalSocket.value.onclose = null
  terminalSocket.value.onmessage = null
  terminalSocket.value.onerror = null
  terminalSocket.value.close()
  terminalSocket.value = null
}

/**
 * 处理 WebSocket 消息
 */
function handleSocketMessage(rawMessage: string) {
  let message: TerminalWsMessage
  try {
    message = JSON.parse(rawMessage) as TerminalWsMessage
  } catch {
    addTerminalMessage('error', `无法解析服务端消息: ${rawMessage}`)
    return
  }

  if (message.type === 'stdout' && message.data) {
    addTerminalMessage('output', message.data)
    return
  }

  if (message.type === 'stderr' && message.data) {
    addTerminalMessage('error', message.data)
    return
  }

  if (message.type === 'error') {
    addTerminalMessage('error', message.data || message.message || '终端执行失败')
    return
  }

  if (message.type === 'status') {
    if (message.message) {
      addTerminalMessage('info', message.message)
    }

    if (message.state === 'connected') {
      chatStore.setSshConnectionState('connected')
    }

    if (message.state === 'disconnected') {
      chatStore.setSshConnectionState('disconnected')
      chatStore.setSshSessionId(null)
    }

    if (message.state === 'error') {
      chatStore.setSshConnectionState('error')
      chatStore.setSshErrorMessage(message.message || '终端状态异常')
    }
  }
}

/**
 * 连接终端 WebSocket
 */
function connectWebSocket(sessionId: string): Promise<void> {
  return new Promise((resolve, reject) => {
    closeSocket()

    const socket = new WebSocket(buildTerminalWsUrl(sessionId, clientId))
    terminalSocket.value = socket

    socket.onopen = () => {
      addTerminalMessage('info', '终端 WebSocket 已连接')
      resolve()
    }

    socket.onmessage = (event) => {
      handleSocketMessage(String(event.data || ''))
    }

    socket.onerror = () => {
      reject(new Error('终端 WebSocket 连接失败'))
    }

    socket.onclose = () => {
      terminalSocket.value = null
      if (!manualDisconnect.value && chatStore.sshConnectionState !== 'disconnected') {
        chatStore.setSshConnectionState('error')
        chatStore.setSshErrorMessage('终端连接已断开，请重新连接')
        addTerminalMessage('error', '终端连接已断开')
      }
    }
  })
}

/**
 * 连接 SSH
 */
async function connectSsh() {
  const validationError = validateForm()
  if (validationError) {
    chatStore.setSshErrorMessage(validationError)
    return
  }

  chatStore.setSshConnectionState('connecting')
  chatStore.clearSshErrorMessage()

  const payload = buildCreateRequest()
  addTerminalMessage('info', `正在连接到 ${payload.username}@${payload.host}:${payload.port}...`)

  try {
    manualDisconnect.value = false
    const { data } = await chatStore.createTerminalSession(payload)
    chatStore.setSshSessionId(data.session_id)
    await connectWebSocket(data.session_id)
    chatStore.setSshConnectionState('connected')
    addTerminalMessage('info', data.message || `SSH 连接成功：${data.username}@${data.host}:${data.port}`)
  } catch (error) {
    chatStore.setSshConnectionState('error')
    const errorMessage = extractErrorMessage(error)
    chatStore.setSshErrorMessage(errorMessage)
    addTerminalMessage('error', `SSH 连接失败: ${errorMessage}`)
    closeSocket()
  }
}

/**
 * 断开 SSH 连接
 */
async function disconnectSsh() {
  manualDisconnect.value = true

  const sessionId = chatStore.sshSessionId
  closeSocket()

  if (sessionId) {
    try {
      await chatStore.closeTerminalSession(sessionId)
    } catch (error) {
      addTerminalMessage('error', `关闭会话失败: ${extractErrorMessage(error)}`)
    }
  }

  chatStore.setSshSessionId(null)
  chatStore.setSshConnectionState('disconnected')
  chatStore.clearSshErrorMessage()
  addTerminalMessage('info', 'SSH 连接已断开')
}

/**
 * 执行命令
 */
function executeCommand() {
  const command = localInput.value.trim()
  if (!command) return

  if (!isSshConnected.value || !isSocketReady.value || !terminalSocket.value) {
    addTerminalMessage('error', 'SSH 未连接，请先登录')
    return
  }

  addTerminalMessage('command', command)

  terminalSocket.value.send(
    JSON.stringify({
      type: 'stdin',
      data: command.endsWith('\n') ? command : `${command}\n`,
    }),
  )

  localInput.value = ''
}

/**
 * 键盘事件处理
 */
function handleKeydown(e: KeyboardEvent) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    executeCommand()
  }
}

/**
 * 清空输入
 */
function clearInput() {
  localInput.value = ''
}

/**
 * 清空输出
 */
function clearOutput() {
  terminalOutput.value = []
}

/**
 * 复制输出内容
 */
async function copyOutput() {
  const text = terminalOutput.value.map((item) => item.content).join('\n')
  if (!text) return

  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text)
  } else {
    const textarea = document.createElement('textarea')
    textarea.value = text
    textarea.style.position = 'fixed'
    textarea.style.opacity = '0'
    document.body.appendChild(textarea)
    textarea.select()
    document.execCommand('copy')
    document.body.removeChild(textarea)
  }

  addTerminalMessage('info', '输出内容已复制到剪贴板')
}

/**
 * 关闭面板
 */
async function closePanel() {
  await disconnectSsh()
  chatStore.closeTerminalSidebar()
}

onBeforeUnmount(() => {
  // 避免组件销毁后遗留 socket 连接
  closeSocket()
})
</script>

<template>
  <div class="terminal-panel">
    <!-- 面板头部 -->
    <div class="terminal-header">
      <div class="header-left">
        <el-icon class="header-icon"><i class="el-icon-monitor" /></el-icon>
        <span class="header-title">SSH 终端</span>
        <el-tag
          v-if="isSshConnected"
          size="small"
          type="success"
          effect="plain"
        >
          已连接
        </el-tag>
        <el-tag
          v-else-if="isSshConnecting"
          size="small"
          type="warning"
          effect="plain"
        >
          连接中...
        </el-tag>
        <el-tag
          v-else-if="isSshError"
          size="small"
          type="danger"
          effect="plain"
        >
          连接错误
        </el-tag>
        <el-tag
          v-else
          size="small"
          type="info"
          effect="plain"
        >
          未连接
        </el-tag>
      </div>
      <div class="header-actions">
        <el-button
          text
          size="small"
          @click="copyOutput"
          :disabled="terminalOutput.length === 0"
        >
          <el-icon><i class="el-icon-document-copy" /></el-icon>
          复制
        </el-button>
        <el-button
          text
          size="small"
          @click="clearOutput"
        >
          <el-icon><i class="el-icon-delete" /></el-icon>
          清空
        </el-button>
        <el-button
          text
          size="small"
          @click="closePanel"
        >
          <el-icon><i class="el-icon-close" /></el-icon>
        </el-button>
      </div>
    </div>

    <!-- SSH 登录表单（未连接时显示） -->
    <div v-if="!isSshConnected" class="ssh-login-form">
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
          <el-input
            v-model="sshForm.password"
            type="password"
            placeholder="请输入密码"
            show-password
          />
        </el-form-item>
        <el-form-item v-else label="私钥">
          <el-input
            v-model="sshForm.privateKey"
            type="textarea"
            :rows="4"
            placeholder="-----BEGIN RSA PRIVATE KEY-----&#10;..."
          />
        </el-form-item>
        <el-form-item v-if="sshForm.authType === 'key'" label="密钥密码">
          <el-input
            v-model="sshForm.passphrase"
            type="password"
            placeholder="可选"
            show-password
          />
        </el-form-item>
        <el-form-item>
          <el-button
            type="primary"
            size="small"
            @click="connectSsh"
            :loading="isSshConnecting"
          >
            连接
          </el-button>
          <el-button
            size="small"
            @click="() => {
              sshForm.host = ''
              sshForm.port = '22'
              sshForm.username = ''
              sshForm.password = ''
              sshForm.privateKey = ''
              sshForm.passphrase = ''
            }"
          >
            重置
          </el-button>
        </el-form-item>
        <!-- 错误提示 -->
        <el-alert
          v-if="chatStore.sshErrorMessage"
          type="error"
          :title="chatStore.sshErrorMessage"
          :closable="true"
          @close="chatStore.clearSshErrorMessage()"
        />
      </el-form>
    </div>

    <!-- 终端输出区域（已连接时显示） -->
    <div v-else ref="outputContainer" class="terminal-output">
      <div
        v-for="(item, index) in terminalOutput"
        :key="index"
        class="output-line"
        :class="`output-${item.type}`"
      >
        <span class="output-prompt" v-if="item.type === 'command'">$</span>
        <span class="output-content">{{ item.content }}</span>
      </div>
      <!-- 空状态提示 -->
      <div v-if="terminalOutput.length === 0" class="output-empty">
        <el-icon class="empty-icon"><i class="el-icon-monitor" /></el-icon>
        <p>暂无输出</p>
        <p class="empty-sub">输入命令并按 Enter 执行</p>
      </div>
    </div>

    <!-- 终端输入区域 -->
    <div class="terminal-input-area">
      <div class="input-prompt">
        <span class="prompt-symbol">$</span>
      </div>
      <el-input
        ref="terminalInput"
        v-model="localInput"
        type="textarea"
        :autosize="{ minRows: 1, maxRows: 4 }"
        placeholder="输入命令或从命令卡片发送..."
        class="terminal-input"
        @keydown="handleKeydown"
        :disabled="!isSshConnected"
      />
      <el-button
        type="primary"
        size="small"
        class="execute-btn"
        :disabled="!localInput.trim() || !isSshConnected || !isSocketReady"
        @click="executeCommand"
      >
        <el-icon><i class="el-icon-right" /></el-icon>
        执行
      </el-button>
      <el-button
        size="small"
        class="clear-btn"
        :disabled="!localInput"
        @click="clearInput"
      >
        清空输入
      </el-button>
    </div>

    <!-- 底部提示 -->
    <div class="terminal-footer">
      <el-icon class="footer-icon"><i class="el-icon-info-filled" /></el-icon>
      <span v-if="!isSshConnected">
        请先填写 SSH 信息并连接
      </span>
      <span v-else>
        按 Enter 执行命令，Shift+Enter 换行 ·
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

/* 头部 */
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

.header-icon {
  font-size: 16px;
  color: #67c23a;
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

/* SSH 登录表单 */
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
  font-family: 'Consolas', 'Monaco', monospace;
}

.ssh-login-form :deep(.el-radio-group) {
  display: flex;
  gap: 8px;
}

/* 终端输出区域 */
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

.output-line {
  display: flex;
  align-items: flex-start;
  margin-bottom: 4px;
}

.output-prompt {
  color: #67c23a;
  font-weight: 600;
  margin-right: 8px;
  min-width: 16px;
}

.output-command .output-content {
  color: #d4d4d4;
}

.output-output .output-content {
  color: #9cdcfe;
}

.output-error .output-content {
  color: #f56c6c;
}

.output-info .output-content {
  color: #909399;
  font-style: italic;
}

.output-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
  color: #666;
  text-align: center;
}

.empty-icon {
  font-size: 32px;
  margin-bottom: 12px;
  color: #555;
}

.empty-sub {
  font-size: 12px;
  color: #555;
  margin-top: 4px;
}

/* 输入区域 */
.terminal-input-area {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 10px 12px;
  background: #252526;
  border-top: 1px solid #333;
}

.input-prompt {
  display: flex;
  align-items: center;
  height: 32px;
  color: #67c23a;
  font-weight: 600;
  font-family: 'Consolas', 'Monaco', monospace;
}

.prompt-symbol {
  font-size: 14px;
}

.terminal-input {
  flex: 1;
}

.terminal-input :deep(.el-textarea__inner) {
  background: #1e1e1e;
  border-color: #444;
  color: #d4d4d4;
  font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
  font-size: 13px;
  line-height: 1.5;
  padding: 6px 10px;
}

.terminal-input :deep(.el-textarea__inner:focus) {
  border-color: #409eff;
}

.terminal-input :deep(.el-textarea__inner:disabled) {
  opacity: 0.6;
  cursor: not-allowed;
}

.terminal-input :deep(.el-textarea__inner::placeholder) {
  color: #666;
}

.execute-btn,
.clear-btn {
  height: 32px;
  padding: 0 12px;
}

.execute-btn :deep(.el-icon) {
  margin-right: 4px;
}

/* 底部提示 */
.terminal-footer {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  background: #2d2d2d;
  border-top: 1px solid #333;
  font-size: 12px;
  color: #666;
}

.footer-icon {
  font-size: 14px;
  color: #909399;
}

.terminal-footer :deep(.el-link) {
  font-size: 12px;
  margin-left: 4px;
}
</style>
