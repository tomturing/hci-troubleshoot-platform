<script setup lang="ts">
/**
 * SshConnectSection.vue
 * SSH 连接区（唯一模板）
 *
 * 由 SshConnectDialog 抽取而来，供两处复用：
 * - SshConnectDialog：mode="dialog"（行为与抽取前一致，含 1.5s 自动关闭）
 * - LogCollectCaseDialog：mode="inline"（内嵌，成功后折叠为状态条）
 *
 * 连接状态机、bridge 引导、错误处理只在这里实现一次；字段仍由 SshFormSection 渲染。
 */
import { ref, reactive, computed, watch, onBeforeUnmount } from 'vue'
import { ElMessage } from 'element-plus'
import { CircleCheck, CircleCheckFilled, Download, Loading, Refresh } from '@element-plus/icons-vue'
import { useChatStore } from '@/stores/chat'
import { checkBridgeBeforeOpen, type TerminalAuthType } from '@/api/terminal'
import SshFormSection from './SshFormSection.vue'

type ViewState = 'bridge-guide' | 'form' | 'progress' | 'error' | 'success' | 'connected'
type BridgeStatus = 'running' | 'not-running' | 'checking'

const props = withDefaults(
  defineProps<{
    bridgeStatus: BridgeStatus
    /** dialog：弹框内使用（自动关闭）；inline：内嵌（成功后折叠） */
    mode?: 'dialog' | 'inline'
    /** connectSSH 的 caseId；缺省沿用 sshFlowDialogCaseId */
    caseId?: string | null
    /** 连接成功后是否自动收起（仅 dialog 模式使用） */
    autoClose?: boolean
  }>(),
  { mode: 'inline', caseId: null, autoClose: false },
)

const emit = defineEmits<{
  connected: [host: string]
  cancelled: []
  'state-change': [state: ViewState]
}>()

const chatStore = useChatStore()

const viewState = ref<ViewState>('form')
const errorMessage = ref('')
const checkingBridge = ref(false)
const connectedHost = ref('')

const sshForm = reactive({
  host: '',
  port: '22',
  username: '',
  password: '',
  privateKey: '',
  passphrase: '',
})
const authType = ref<TerminalAuthType>('password')
let autoCloseTimer: ReturnType<typeof setTimeout> | null = null

function clearAutoCloseTimer() {
  if (autoCloseTimer) {
    clearTimeout(autoCloseTimer)
    autoCloseTimer = null
  }
}

function emitState() {
  emit('state-change', viewState.value)
}

function lastSshConfig(): { host?: string; port?: string; username?: string } {
  try {
    return JSON.parse(localStorage.getItem('hci_last_ssh_config') || '{}')
  } catch {
    return {}
  }
}

function hydrateLastConfig() {
  const last = lastSshConfig()
  sshForm.host = last.host || ''
  sshForm.port = last.port || '22'
  sshForm.username = last.username || ''
}

// 内嵌模式下，若会话已连接则直接展示已连接状态（不重复要求登录）
if (props.mode === 'inline' && chatStore.sshConnectionState === 'connected') {
  const last = lastSshConfig()
  connectedHost.value = last.host || ''
  viewState.value = 'connected'
}

// ===== SSH 连接 =====
async function handleConnect() {
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
  emitState()
  errorMessage.value = ''

  try {
    const host = sshForm.host.trim()
    await chatStore.connectSSH({
      host,
      port: Number(sshForm.port) || 22,
      username: sshForm.username.trim(),
      authType: authType.value,
      password: sshForm.password,
      privateKey: sshForm.privateKey,
      passphrase: sshForm.passphrase,
      caseId: props.caseId || chatStore.sshFlowDialogCaseId || 'terminal-only',
    })

    // 只保存非敏感配置（不含密码/私钥）
    localStorage.setItem(
      'hci_last_ssh_config',
      JSON.stringify({
        host,
        port: sshForm.port,
        username: sshForm.username.trim(),
        lastSuccessAt: new Date().toISOString(),
      }),
    )

    connectedHost.value = host
    chatStore.setLastSshHost(host)
    viewState.value = props.mode === 'dialog' ? 'success' : 'connected'
    emitState()
    emit('connected', host)

    // 与抽取前一致：连接成功后打开终端侧边栏
    chatStore.openTerminalSidebar()

    if (props.mode === 'dialog' && props.autoClose) {
      clearAutoCloseTimer()
      autoCloseTimer = setTimeout(() => {
        chatStore.sshFlowDialogVisible = false
      }, 1500)
    }
  } catch (e: any) {
    errorMessage.value = e.message || 'SSH 连接失败'
    viewState.value = 'error'
    emitState()
  }
}

function handleCancel() {
  clearAutoCloseTimer()
  emit('cancelled')
}

async function handleRefreshBridge() {
  checkingBridge.value = true
  try {
    const status = await checkBridgeBeforeOpen()
    if (status === 'running') {
      viewState.value = 'form'
      emitState()
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

function handleRetry() {
  clearAutoCloseTimer()
  viewState.value = 'form'
  emitState()
  errorMessage.value = ''
}

/** inline 模式：已连接后点「重新登录」，预填上次配置（不含密码） */
function handleRelogin() {
  hydrateLastConfig()
  sshForm.password = ''
  viewState.value = 'form'
  emitState()
}

watch(
  () => props.bridgeStatus,
  (status) => {
    if (viewState.value === 'connected' || viewState.value === 'success') return
    if (status === 'not-running') {
      viewState.value = 'bridge-guide'
    } else if (status === 'running' && viewState.value === 'bridge-guide') {
      viewState.value = 'form'
    }
    emitState()
  },
  { immediate: true },
)

// 连接建立后若断开（bridge 关闭 / 远端 shell 退出），回到表单并提示
watch(
  () => chatStore.sshConnectionState,
  (state) => {
    if (viewState.value !== 'connected' && viewState.value !== 'success') return
    if (state === 'connected') return
    clearAutoCloseTimer()
    errorMessage.value =
      chatStore.sshErrorMessage || 'SSH 连接已断开，请检查 Bridge 或远端 Shell 后重新登录'
    viewState.value = 'form'
    emitState()
  },
)

onBeforeUnmount(() => {
  clearAutoCloseTimer()
})

const isDialog = computed(() => props.mode === 'dialog')
</script>

<template>
  <div class="ssh-connect-section" :class="{ 'is-inline': !isDialog }">
    <!-- ===== 已连接（inline 折叠态）===== -->
    <div v-if="viewState === 'connected'" class="ssh-connected-bar">
      <span class="dot" />
      <span class="text">已连接 {{ connectedHost }}</span>
      <el-button size="small" text type="primary" @click="handleRelogin">重新登录</el-button>
    </div>

    <!-- ===== Bridge 引导 ===== -->
    <div v-else-if="viewState === 'bridge-guide'" class="bridge-guide-section">
      <el-alert type="warning" :closable="false" class="bridge-alert">
        <template #title>
          <strong>SSH Bridge 未运行</strong>
        </template>
        <p class="bridge-desc">请下载并启动 terminal_bridge.exe，然后点击「重新检测」</p>
      </el-alert>

      <div class="bridge-download">
        <p class="bridge-download-title"><el-icon><Download /></el-icon>下载 Bridge 工具（Windows）</p>
        <el-button type="primary" size="small" @click="handleDownloadBridge">
          <el-icon><Download /></el-icon>
          下载 Windows 版
        </el-button>
      </div>

      <div class="bridge-refresh">
        <el-button :loading="checkingBridge" @click="handleRefreshBridge">
          <el-icon><Refresh /></el-icon>
          已运行 Bridge 工具，点击刷新
        </el-button>
      </div>

      <div v-if="isDialog" class="dialog-actions">
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
        <el-button type="primary" :loading="false" @click="handleConnect">连接</el-button>
        <el-button v-if="isDialog" @click="handleCancel">取消</el-button>
      </div>
    </div>

    <!-- ===== 进度 ===== -->
    <div v-else-if="viewState === 'progress'" class="progress-section">
      <p class="progress-text">正在连接 SSH...</p>
      <el-icon class="is-loading spin-icon"><Loading /></el-icon>
    </div>

    <!-- ===== 错误 ===== -->
    <div v-else-if="viewState === 'error'" class="error-section">
      <el-alert type="error" :closable="false">
        <template #title>
          <strong>SSH 连接失败</strong>
        </template>
        <p class="error-detail">{{ errorMessage }}</p>
      </el-alert>

      <div class="dialog-actions">
        <el-button type="primary" @click="handleRetry">重试</el-button>
        <el-button v-if="isDialog" @click="handleCancel">取消</el-button>
      </div>
    </div>

    <!-- ===== 成功（dialog 自动收起前）===== -->
    <div v-else-if="viewState === 'success'" class="success-section">
      <el-icon color="#67c23a" :size="24"><CircleCheckFilled /></el-icon>
      <p class="ssh-success-text"><el-icon><CircleCheck /></el-icon>SSH 连接成功，终端已打开</p>
    </div>
  </div>
</template>

<style scoped>
.ssh-connected-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border: 1px solid #d1f0d1;
  border-radius: 6px;
  background: #f0f9eb;
  font-size: 13px;
}
.ssh-connected-bar .dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #67c23a;
}
.ssh-connected-bar .text {
  flex: 1;
  color: #3f6b2f;
}

.bridge-guide-section {
  padding: 8px 0;
}
.bridge-alert {
  margin-bottom: 16px;
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
  margin-bottom: 8px;
}

.form-section {
  padding: 4px 0;
}

.progress-section {
  text-align: center;
  padding: 24px 0;
}
.progress-text {
  margin-bottom: 16px;
}
.spin-icon {
  animation: spin 1s linear infinite;
}
@keyframes spin {
  from {
    transform: rotate(0deg);
  }
  to {
    transform: rotate(360deg);
  }
}

.error-section {
  padding: 8px 0;
}
.error-detail {
  margin-top: 8px;
  color: #f56c6c;
  word-break: break-all;
}

.success-section {
  text-align: center;
  padding: 24px 0;
}

.dialog-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 16px;
}
.bridge-download-title,
.ssh-success-text {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
</style>
