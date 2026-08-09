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
import { checkBridgeBeforeOpen, parseSimulationConnectionJson, type TerminalAuthType } from '@/api/terminal'
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
  executionMode: undefined as 'sim-ssh' | undefined,
  testRunId: '',
})
const authType = ref<TerminalAuthType>('password')
const connectionJsonText = ref('')
const recommendedCommand = ref('')
let autoCloseTimer: ReturnType<typeof setTimeout> | null = null

/**
 * 将 two-step-acceptance.sh 生成的 connection.json 一次性填入表单。
 * JSON 只保存在组件内存中；不会写入 localStorage、日志或 API。
 */
function applyConnectionJson() {
  try {
    const connection = parseSimulationConnectionJson(connectionJsonText.value)
    sshForm.host = connection.host
    sshForm.port = String(connection.port)
    sshForm.username = connection.username
    sshForm.password = connection.password
    sshForm.executionMode = 'sim-ssh'
    sshForm.testRunId = connection.testRunId
    authType.value = 'lease'
    recommendedCommand.value = connection.recommendedCommand
    ElMessage.success(`已载入 KBD ${connection.supportId} 的仿真租约`)
  } catch (error: any) {
    ElMessage.error(`connection.json 无法载入：${error?.message || 'JSON 格式错误'}`)
  }
}

function clearAutoCloseTimer() {
  if (autoCloseTimer) {
    clearTimeout(autoCloseTimer)
    autoCloseTimer = null
  }
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
  if ((authType.value === 'password' || authType.value === 'lease') && !sshForm.password) {
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
    await chatStore.connectSSH({
      host: sshForm.host.trim(),
      port: Number(sshForm.port) || 22,
      username: sshForm.username.trim(),
      authType: authType.value,
      password: sshForm.password,
      privateKey: sshForm.privateKey,
      passphrase: sshForm.passphrase,
      executionMode: authType.value === 'lease' ? 'sim-ssh' : undefined,
      testRunId: authType.value === 'lease' ? sshForm.testRunId : undefined,
      caseId: chatStore.sshFlowDialogCaseId || 'terminal-only',
    })

    // 保存 SSH 配置到 localStorage（不含密码）
    localStorage.setItem('hci_last_ssh_config', JSON.stringify({
      host: sshForm.host.trim(),
      port: sshForm.port,
      username: sshForm.username.trim(),
      lastSuccessAt: new Date().toISOString(),
    }))
    connectionJsonText.value = ''
    sshForm.password = ''

    viewState.value = 'success'

    // 打开终端侧边栏
    chatStore.openTerminalSidebar()

    // 关闭弹框
    clearAutoCloseTimer()
    autoCloseTimer = setTimeout(() => {
      chatStore.sshFlowDialogVisible = false
    }, 1500)

  } catch (e: any) {
    errorMessage.value = e.message || 'SSH 连接失败'
    viewState.value = 'error'
  }
}

// ===== 取消 =====
async function handleCancel() {
  if (viewState.value !== 'success') {
    chatStore.disconnectSSH()
  }
  await chatStore.closeSshFlowDialog()
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

// ===== 重试 =====
function handleRetry() {
  clearAutoCloseTimer()
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

watch(
  () => chatStore.sshConnectionState,
  (state) => {
    if (viewState.value !== 'success') return
    if (state === 'connected') return
    clearAutoCloseTimer()
    errorMessage.value = chatStore.sshErrorMessage || 'SSH 连接建立后立即断开，请检查 Bridge 或远端 Shell'
    viewState.value = 'error'
  },
)

// ===== 弹框标题 =====
const dialogTitle = computed(() =>
  chatStore.sshFlowDialogMode === 'create-case' ? '连接 SSH 并采集环境数据' : '连接 SSH 终端'
)

// ===== 生命周期 =====
onBeforeUnmount(() => {
  clearAutoCloseTimer()
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
        :allow-lease="true"
        @update:ssh-form="Object.assign(sshForm, $event)"
        @update:auth-type="authType = $event"
      />

      <div v-if="authType === 'lease'" class="connection-json-import">
        <el-form-item label="connection.json（可选，一次性填充）">
          <el-input
            v-model="connectionJsonText"
            type="textarea"
            :autosize="{ minRows: 3, maxRows: 7 }"
            placeholder="粘贴第一步生成的 connection.json 内容"
          />
        </el-form-item>
        <el-button size="small" @click="applyConnectionJson">载入连接文件</el-button>
        <el-alert v-if="recommendedCommand" type="success" :closable="false" class="recommended-command">
          连接后在终端执行：<code>{{ recommendedCommand }}</code>
        </el-alert>
      </div>

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

.connection-json-import {
  margin-top: 8px;
  padding: 12px;
  border: 1px solid #dcdfe6;
  border-radius: 6px;
  background: #fafafa;
}

.recommended-command {
  margin-top: 12px;
  overflow-wrap: anywhere;
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
