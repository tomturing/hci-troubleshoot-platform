<script setup lang="ts">
/**
 * SshConnectDialog.vue
 * SSH 连接弹框 — 状态B入口（有工单无SSH）
 *
 * 功能：
 * - 接收 bridgeStatus prop（来自 TerminalPanel 的前置检测）
 * - 根据状态显示 Bridge 引导或 SSH 表单
 * - terminal-only 模式：仅建立 SSH 连接，不采集数据
 */
import { ref, reactive, computed, watch, onBeforeUnmount } from 'vue'
import { ElMessage } from 'element-plus'
import { useChatStore } from '@/stores/chat'
import { checkBridgeBeforeOpen, createBridgeSocket, buildConnectMessage, type TerminalAuthType, type TerminalWsMessage, type SshConnectOptions } from '@/api/terminal'
import SshFormSection from './SshFormSection.vue'

const chatStore = useChatStore()

// ===== Props =====
const props = defineProps<{
  bridgeStatus: 'running' | 'not-running' | 'checking'
}>()

// ===== 视图状态 =====
type ViewState = 'bridge-guide' | 'form' | 'progress' | 'error' | 'success'
const viewState = ref<ViewState>('form')
const errorMessage = ref('')
const checkingBridge = ref(false)

// ===== SSH 表单 =====
const sshForm = reactive({
  host: '',
  port: '22',
  username: '',
  password: '',
  privateKey: '',
  passphrase: '',
})
const authType = ref<TerminalAuthType>('password')

// ===== 临时 WebSocket =====
let tempSocket: WebSocket | null = null
let authTimer: ReturnType<typeof setTimeout> | null = null
const AUTH_TIMEOUT = 15000

function closeTempSocket() {
  if (authTimer) {
    clearTimeout(authTimer)
    authTimer = null
  }
  if (tempSocket) {
    tempSocket.onopen = null
    tempSocket.onmessage = null
    tempSocket.onerror = null
    tempSocket.onclose = null
    tempSocket.close()
    tempSocket = null
  }
}

// ===== Bridge 引导 =====
async function handleRefreshBridge() {
  checkingBridge.value = true
  try {
    const status = await checkBridgeBeforeOpen()
    if (status === 'running') {
      viewState.value = 'form'
      ElMessage.success('Bridge 检测成功')
    } else {
      ElMessage.warning('Bridge 仍未检测到，请确认已启动后重试')
    }
  } finally {
    checkingBridge.value = false
  }
}

function handleDownloadBridge() {
  const url = import.meta.env.VITE_BRIDGE_DOWNLOAD_URL || '/downloads/terminal_bridge.exe'
  const a = document.createElement('a')
  a.href = url
  a.download = 'terminal_bridge.exe'
  a.style.display = 'none'
  document.body.appendChild(a)
  a.click()
  setTimeout(() => document.body.removeChild(a), 200)
}

// ===== SSH 连接（terminal-only 模式）=====
async function handleConnect() {
  // 验证表单
  if (!sshForm.host.trim()) {
    ElMessage.warning('请填写主机地址')
    return
  }
  if (!sshForm.username.trim()) {
    ElMessage.warning('请填写用户名')
    return
  }
  if (authType.value === 'password' && !sshForm.password) {
    ElMessage.warning('请填写密码')
    return
  }
  if (authType.value === 'key' && !sshForm.privateKey) {
    ElMessage.warning('请填写私钥')
    return
  }

  viewState.value = 'progress'
  errorMessage.value = ''

  try {
    const socket = createBridgeSocket()
    tempSocket = socket

    await new Promise<void>((resolve, reject) => {
      authTimer = setTimeout(() => {
        closeTempSocket()
        reject(new Error('SSH 认证超时（15秒）'))
      }, AUTH_TIMEOUT)

      socket.onopen = () => {
        const options: SshConnectOptions = {
          host: sshForm.host.trim(),
          port: Number(sshForm.port) || 22,
          username: sshForm.username.trim(),
          auth_type: authType.value,
          password: sshForm.password,
          private_key: sshForm.privateKey,
          passphrase: sshForm.passphrase,
          case_id: chatStore.sshFlowDialogCaseId || 'terminal-only',
        }
        socket.send(buildConnectMessage(options))
      }

      socket.onmessage = (e) => {
        let msg: TerminalWsMessage
        try {
          msg = JSON.parse(String(e.data || ''))
        } catch {
          return
        }

        if (msg.type === 'ssh_connected') {
          clearTimeout(authTimer!)
          authTimer = null

          // 保存 SSH 配置到 localStorage（不含密码）
          localStorage.setItem('hci_last_ssh_config', JSON.stringify({
            host: sshForm.host.trim(),
            port: sshForm.port,
            username: sshForm.username.trim(),
            lastSuccessAt: new Date().toISOString(),
          }))

          resolve()
        } else if (msg.type === 'ssh_error') {
          clearTimeout(authTimer!)
          authTimer = null
          errorMessage.value = msg.message || 'SSH 认证失败'
          reject(new Error(errorMessage.value))
        }
      }

      socket.onerror = () => {
        clearTimeout(authTimer!)
        authTimer = null
        reject(new Error('WebSocket 连接失败'))
      }
    })

    // 连接成功，关闭临时 socket，建立全局连接
    closeTempSocket()

    await chatStore.connectSSH({
      host: sshForm.host.trim(),
      port: Number(sshForm.port) || 22,
      username: sshForm.username.trim(),
      authType: authType.value,
      password: sshForm.password,
      privateKey: sshForm.privateKey,
      passphrase: sshForm.passphrase,
      caseId: chatStore.sshFlowDialogCaseId || 'terminal-only',
    })

    viewState.value = 'success'

    // 打开终端侧边栏
    chatStore.openTerminalSidebar()

    // 关闭弹框
    setTimeout(() => {
      chatStore.sshFlowDialogVisible = false
    }, 1500)

  } catch (e: any) {
    closeTempSocket()
    errorMessage.value = e.message || 'SSH 连接失败'
    viewState.value = 'error'
  }
}

// ===== 取消 =====
async function handleCancel() {
  closeTempSocket()
  await chatStore.closeSshFlowDialog()
}

// ===== 重试 =====
function handleRetry() {
  viewState.value = 'form'
  errorMessage.value = ''
}

// ===== 初始化 =====
watch(
  () => props.bridgeStatus,
  (status) => {
    if (status === 'not-running') {
      viewState.value = 'bridge-guide'
    } else if (status === 'running') {
      viewState.value = 'form'
    }
  },
  { immediate: true },
)

// ===== 弹框标题 =====
const dialogTitle = computed(() =>
  chatStore.sshFlowDialogMode === 'create-case' ? '连接 SSH 并采集环境数据' : '连接 SSH 终端'
)

// ===== 生命周期 =====
onBeforeUnmount(() => {
  closeTempSocket()
})
</script>

<template>
  <el-dialog
    v-model="chatStore.sshFlowDialogVisible"
    :title="dialogTitle"
    width="500px"
    :close-on-click-modal="false"
    align-center
    class="ssh-connect-dialog"
    @close="handleCancel"
  >
    <!-- ===== Bridge 引导 ===== -->
    <div v-if="viewState === 'bridge-guide'" class="bridge-guide-section">
      <el-alert type="warning" :closable="false" class="bridge-alert">
        <template #title>
          <strong>⚠️ SSH Bridge 未运行</strong>
        </template>
        <p class="bridge-desc">
          请下载并启动 terminal_bridge.exe，然后点击「重新检测」
        </p>
      </el-alert>

      <div class="bridge-download">
        <p>📥 下载 Bridge 工具（Windows）</p>
        <el-button type="primary" size="small" @click="handleDownloadBridge">
          ⬇ 下载 Windows 版
        </el-button>
      </div>

      <div class="bridge-refresh">
        <el-button :loading="checkingBridge" @click="handleRefreshBridge">
          🔄 已运行 Bridge 工具，点击刷新
        </el-button>
      </div>

      <div class="dialog-actions">
        <el-button @click="handleCancel">取消</el-button>
      </div>
    </div>

    <!-- ===== SSH 表单 ===== -->
    <div v-else-if="viewState === 'form'" class="form-section">
      <SshFormSection
        :ssh-form="sshForm"
        :auth-type="authType"
        @update:ssh-form="Object.assign(sshForm, $event)"
        @update:auth-type="authType = $event"
      />

      <div class="dialog-actions">
        <el-button type="primary" @click="handleConnect">连接</el-button>
        <el-button @click="handleCancel">取消</el-button>
      </div>
    </div>

    <!-- ===== 进度 ===== -->
    <div v-else-if="viewState === 'progress'" class="progress-section">
      <p class="progress-text">正在连接 SSH...</p>
      <el-icon class="is-loading spin-icon"><i class="el-icon-loading" /></el-icon>
    </div>

    <!-- ===== 错误 ===== -->
    <div v-else-if="viewState === 'error'" class="error-section">
      <el-alert type="error" :closable="false">
        <template #title>
          <strong>❌ SSH 连接失败</strong>
        </template>
        <p class="error-detail">{{ errorMessage }}</p>
      </el-alert>

      <div class="dialog-actions">
        <el-button type="primary" @click="handleRetry">重试</el-button>
        <el-button @click="handleCancel">取消</el-button>
      </div>
    </div>

    <!-- ===== 成功 ===== -->
    <div v-else-if="viewState === 'success'" class="success-section">
      <el-icon color="#67c23a" size="24"><i class="el-icon-success" /></el-icon>
      <p>✅ SSH 连接成功，终端已打开</p>
    </div>
  </el-dialog>
</template>

<style scoped>
/* ===== Bridge 引导 ===== */
.bridge-guide-section {
  padding: 16px 0;
}

.bridge-alert {
  margin-bottom: 20px;
}

.bridge-desc {
  margin-top: 8px;
  color: #6b5b00;
}

.bridge-download {
  padding: 12px 16px;
  background: #f5f7fa;
  border-radius: 8px;
  margin-bottom: 16px;
}

.bridge-download p {
  margin-bottom: 12px;
  font-weight: 500;
}

.bridge-refresh {
  text-align: center;
  margin-bottom: 16px;
}

/* ===== 表单 ===== */
.form-section {
  padding: 8px 0;
}

/* ===== 进度 ===== */
.progress-section {
  text-align: center;
  padding: 32px 0;
}

.progress-text {
  margin-bottom: 16px;
}

.spin-icon {
  animation: spin 1s linear infinite;
}

@keyframes spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

/* ===== 错误 ===== */
.error-section {
  padding: 16px 0;
}

.error-detail {
  margin-top: 8px;
}

/* ===== 成功 ===== */
.success-section {
  text-align: center;
  padding: 32px 0;
}

.success-section p {
  margin-top: 12px;
}

/* ===== 操作按钮 ===== */
.dialog-actions {
  display: flex;
  gap: 12px;
  margin-top: 16px;
}

:deep(.el-dialog__body) {
  padding: 16px 24px 24px;
}
</style>