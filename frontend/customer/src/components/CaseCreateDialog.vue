<script setup lang="ts">
import { ref, reactive, computed, watch, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { useChatStore } from '@/stores/chat'
import { checkBridgeRunning, type BridgeStatus } from '@/api/terminal'

const chatStore = useChatStore()

// Bridge 下载地址
const BRIDGE_DOWNLOAD_URL =
  import.meta.env.VITE_BRIDGE_DOWNLOAD_URL || '/downloads/terminal_bridge.exe'

// Bridge 检测状态（需要将 boolean 映射为 BridgeStatus）
const bridgeDetected = ref<BridgeStatus>('checking')

// 本地编辑副本
const form = reactive({
  title: '',
  description: '',
  assistantType: '',
  sshHost: '',
  sshPort: '22',
  sshUsername: '',
  sshPassword: '',
})

// SSH 连接阶段（使用 acli_* 而非 acll_*）
const sshPhase = ref<'idle' | 'connecting' | 'connected' | 'acli_check' | 'collecting' | 'done' | 'error' | 'acli_not_found'>('idle')
const sshErrorMessage = ref('')
const sshErrorDetail = ref('')

// 是否显示无 SSH 确认弹框
const showNoSSHConfirm = ref(false)

// 自动填充上次成功的 SSH 信息
const lastSSHConfig = localStorage.getItem('hci_last_ssh_config')
if (lastSSHConfig) {
  try {
    const config = JSON.parse(lastSSHConfig)
    form.sshHost = config.host || ''
    form.sshPort = config.port || '22'
    form.sshUsername = config.username || ''
  } catch {
    // 解析失败，忽略
  }
}

// 默认助手
watch(
  () => chatStore.selectedAssistant,
  (val) => {
    if (val && !form.assistantType) {
      form.assistantType = val
    }
  },
  { immediate: true },
)

// Bridge 检测函数（将 boolean 映射为 BridgeStatus）
async function detectBridge(): Promise<BridgeStatus> {
  const running = await checkBridgeRunning()
  return running ? 'running' : 'not_running'
}

// 弹框打开时检测 Bridge
onMounted(async () => {
  bridgeDetected.value = await detectBridge()
})

// 计算属性
const canConnectSSH = computed(() => {
  return (
    bridgeDetected.value === 'running' &&
    form.sshHost.trim() &&
    form.sshUsername.trim() &&
    form.sshPassword.trim()
  )
})

const isConnecting = computed(() => {
  return sshPhase.value === 'connecting' || sshPhase.value === 'acli_check' || sshPhase.value === 'collecting'
})

// 校验表单（使用 ElMessage 显示错误）
function validateForm(): boolean {
  if (!form.title.trim()) {
    ElMessage.warning('请填写工单标题')
    return false
  }
  if (!form.description.trim()) {
    ElMessage.warning('请填写问题描述')
    return false
  }
  return true
}

// 连接 SSH 并创建工单
async function handleConnectAndCreate() {
  if (!validateForm()) return
  if (!canConnectSSH.value) return

  sshPhase.value = 'connecting'
  sshErrorMessage.value = ''
  sshErrorDetail.value = ''

  try {
    // 调用 Store 的连接方法
    await chatStore.connectSSHAndCreateCase(
      form.title,
      form.description,
      {
        host: form.sshHost.trim(),
        port: Number(form.sshPort) || 22,
        username: form.sshUsername.trim(),
        password: form.sshPassword,
      },
      form.assistantType,
      chatStore.pendingUserMessage, // 传递用户原始消息
    )

    // 连接成功，保存 SSH 配置到 localStorage（不含密码）
    localStorage.setItem(
      'hci_last_ssh_config',
      JSON.stringify({
        host: form.sshHost.trim(),
        port: form.sshPort,
        username: form.sshUsername.trim(),
        lastSuccessAt: new Date().toISOString(),
      }),
    )

    sshPhase.value = 'done'

    // 终端面板自动打开
    chatStore.openTerminalSidebar()

    // 关闭弹框
    chatStore.showCaseTemplate = false
  } catch (e: any) {
    sshPhase.value = 'error'
    sshErrorMessage.value = e.message || 'SSH 连接失败'
    sshErrorDetail.value = e.detail || ''
  }
}

// 无 SSH 创建工单
function handleNoSSHCreate() {
  showNoSSHConfirm.value = true
}

function handleConfirmNoSSHCreate() {
  if (!validateForm()) return

  // 调用 Store 的无 SSH 创建方法（正确的签名）
  chatStore.createCaseWithoutSSH({
    title: form.title,
    description: form.description,
  }, form.assistantType)

  showNoSSHConfirm.value = false
  chatStore.showCaseTemplate = false
}

// 取消
function handleCancel() {
  chatStore.cancelCreateCase()
  // 如果正在连接，取消 SSH 流程
  if (sshPhase.value !== 'idle' && sshPhase.value !== 'done' && sshPhase.value !== 'error') {
    chatStore.cancelSSHCreation?.()
  }
}

// 下载 Bridge
function handleDownloadBridge() {
  const a = document.createElement('a')
  a.href = BRIDGE_DOWNLOAD_URL
  a.download = 'terminal_bridge.exe'
  a.style.display = 'none'
  document.body.appendChild(a)
  a.click()
  setTimeout(() => document.body.removeChild(a), 200)
}

// 监听弹框打开，重置状态
watch(
  () => chatStore.showCaseTemplate,
  async (val) => {
    if (val) {
      // 重置 SSH 连接状态
      sshPhase.value = 'idle'
      sshErrorMessage.value = ''
      sshErrorDetail.value = ''
      showNoSSHConfirm.value = false

      // 同步标题和描述
      form.title = chatStore.caseTemplate.title
      form.description = chatStore.caseTemplate.description

      // 检测 Bridge（使用 detectBridge 映射 boolean）
      bridgeDetected.value = 'checking'
      bridgeDetected.value = await detectBridge()
    }
  },
)
</script>

<template>
  <!-- 主弹框 -->
  <el-dialog
    v-model="chatStore.showCaseTemplate"
    title="创建工单"
    width="580px"
    :close-on-click-modal="false"
    align-center
    class="case-create-dialog"
  >
    <!-- 基本信息 -->
    <div class="form-section">
      <div class="section-title">基本信息</div>
      <el-form label-position="top">
        <el-form-item label="标题">
          <el-input
            v-model="form.title"
            placeholder="简要描述问题"
            maxlength="100"
            show-word-limit
          />
        </el-form-item>
        <el-form-item label="描述">
          <el-input
            v-model="form.description"
            type="textarea"
            :autosize="{ minRows: 2, maxRows: 6 }"
            placeholder="详细描述您遇到的问题..."
          />
        </el-form-item>
        <el-form-item label="AI 助手" v-if="chatStore.showAssistantSelector">
          <el-select v-model="form.assistantType" placeholder="选择 AI 助手" style="width: 100%">
            <el-option
              v-for="assistant in chatStore.assistants"
              :key="assistant.type"
              :label="assistant.display_name"
              :value="assistant.type"
              :disabled="!assistant.available"
            >
              <div class="assistant-option-compact">
                <span>{{ assistant.display_name }}</span>
                <el-tag v-if="assistant.is_default" size="small" type="success">默认</el-tag>
              </div>
            </el-option>
          </el-select>
        </el-form-item>
      </el-form>
    </div>

    <!-- SSH 连接信息 -->
    <div class="form-section ssh-section">
      <div class="section-title">SSH 连接信息</div>

      <!-- Bridge 未运行提示 -->
      <div v-if="bridgeDetected === 'not_running'" class="bridge-warning">
        <el-alert type="warning" :closable="false">
          <template #title>
            <span>⚠️ SSH Bridge 未运行</span>
          </template>
          <p class="bridge-warning-detail">
            浏览器无法连接 ws://localhost:9999<br />
            请先启动 terminal_bridge.exe
          </p>
          <el-button type="primary" size="small" class="bridge-download-link" @click="handleDownloadBridge">
            下载 SSH Bridge
          </el-button>
        </el-alert>
      </div>

      <!-- SSH 输入表单 -->
      <el-form label-position="top" v-else>
        <div class="ssh-row">
          <el-form-item label="主机地址" class="ssh-host-item">
            <el-input v-model="form.sshHost" placeholder="192.168.1.100" />
          </el-form-item>
          <el-form-item label="端口" class="ssh-port-item">
            <el-input v-model="form.sshPort" placeholder="22" />
          </el-form-item>
        </div>
        <div class="ssh-row">
          <el-form-item label="用户名" class="ssh-username-item">
            <el-input v-model="form.sshUsername" placeholder="root" />
          </el-form-item>
          <el-form-item label="密码" class="ssh-password-item">
            <el-input v-model="form.sshPassword" type="password" placeholder="请输入密码" show-password />
          </el-form-item>
        </div>
        <p class="ssh-hint" v-if="form.sshHost && form.sshUsername">
          💡 上次成功连接的信息已自动填充
        </p>
      </el-form>

      <!-- SSH 连接状态 -->
      <div v-if="sshPhase !== 'idle'" class="ssh-status">
        <div v-if="sshPhase === 'connecting'" class="status-connecting">
          <el-icon class="is-loading"><i class="el-icon-loading" /></el-icon>
          <span>正在连接 SSH...</span>
          <p class="status-detail">目标: {{ form.sshUsername }}@{{ form.sshHost }}:{{ form.sshPort }}</p>
        </div>

        <div v-if="sshPhase === 'acli_check'" class="status-checking">
          <el-icon class="is-loading"><i class="el-icon-loading" /></el-icon>
          <span>检查 acli 工具...</span>
        </div>

        <div v-if="sshPhase === 'collecting'" class="status-collecting">
          <el-icon class="is-loading"><i class="el-icon-loading" /></el-icon>
          <span>正在采集环境数据...</span>
          <ul class="collect-items">
            <li>集群信息...</li>
            <li>告警列表...</li>
            <li>任务状态...</li>
          </ul>
        </div>

        <div v-if="sshPhase === 'done'" class="status-done">
          <el-icon color="#67c23a"><i class="el-icon-success" /></el-icon>
          <span>连接成功，环境数据已采集</span>
        </div>

        <div v-if="sshPhase === 'acli_not_found'" class="status-acli-error">
          <el-icon color="#e6a23c"><i class="el-icon-warning" /></el-icon>
          <span>acli 工具未安装</span>
          <p class="status-detail">环境数据采集将跳过，您可以手动描述环境信息</p>
        </div>

        <div v-if="sshPhase === 'error'" class="status-error">
          <el-icon color="#f56c6c"><i class="el-icon-error" /></el-icon>
          <span>连接失败: {{ sshErrorMessage }}</span>
          <p class="status-detail" v-if="sshErrorDetail">{{ sshErrorDetail }}</p>
          <div class="error-checklist">
            <p>请检查：</p>
            <ul>
              <li>主机地址和端口是否正确</li>
              <li>用户名是否有 SSH 登录权限</li>
              <li>密码是否正确</li>
              <li>目标主机 SSH 服务是否运行</li>
            </ul>
          </div>
        </div>
      </div>
    </div>

    <!-- 按钮区域 -->
    <div class="button-area">
      <el-button
        type="primary"
        :disabled="!canConnectSSH || isConnecting"
        :loading="isConnecting"
        class="btn-connect"
        @click="handleConnectAndCreate"
      >
        连接并创建工单
      </el-button>

      <el-button class="btn-cancel" @click="handleCancel">
        取消
      </el-button>

      <el-tooltip
        content="若无 SSH，自动化能力将大大降低"
        placement="top"
        effect="dark"
      >
        <el-button
          class="btn-no-ssh"
          :disabled="!form.title.trim() || !form.description.trim()"
          @click="handleNoSSHCreate"
        >
          ⚠️ 无 SSH 创建工单
        </el-button>
      </el-tooltip>
    </div>
  </el-dialog>

  <!-- 无 SSH 确认弹框 -->
  <el-dialog
    v-model="showNoSSHConfirm"
    title="提示"
    width="420px"
    :close-on-click-modal="false"
    align-center
  >
    <div class="no-ssh-warning">
      <el-icon color="#e6a23c" size="32"><i class="el-icon-warning" /></el-icon>
      <p class="warning-title">无 SSH 创建工单</p>
      <p class="warning-desc">自动化能力将大大降低：</p>
      <ul class="warning-list">
        <li>无法自动采集环境数据</li>
        <li>无法执行诊断命令</li>
        <li>AI 需要您手动提供更多信息</li>
      </ul>
      <p class="warning-hint">工单创建后，您可以随时通过终端面板连接 SSH。</p>
    </div>
    <template #footer>
      <el-button @click="showNoSSHConfirm = false">返回填写 SSH</el-button>
      <el-button class="btn-no-ssh-confirm" @click="handleConfirmNoSSHCreate">继续创建</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.case-create-dialog :deep(.el-dialog__body) {
  padding: 16px 20px;
}

.form-section {
  margin-bottom: 20px;
}

.section-title {
  font-size: 14px;
  font-weight: 600;
  color: #303133;
  margin-bottom: 12px;
  padding-bottom: 8px;
  border-bottom: 1px solid #e4e7ed;
}

.ssh-section {
  background: #f5f7fa;
  padding: 16px;
  border-radius: 8px;
}

.ssh-row {
  display: flex;
  gap: 12px;
}

.ssh-host-item {
  flex: 1;
}

.ssh-port-item {
  width: 100px;
}

.ssh-username-item {
  flex: 1;
}

.ssh-password-item {
  flex: 1;
}

.ssh-hint {
  font-size: 12px;
  color: #909399;
  margin-top: 8px;
}

.bridge-warning {
  margin-bottom: 16px;
}

.bridge-warning-detail {
  font-size: 13px;
  margin-top: 8px;
  line-height: 1.6;
}

.bridge-download-link {
  margin-top: 12px;
}

.ssh-status {
  margin-top: 16px;
  padding: 12px;
  background: #fff;
  border-radius: 6px;
  border: 1px solid #e4e7ed;
}

.status-connecting,
.status-checking,
.status-collecting {
  display: flex;
  align-items: center;
  gap: 8px;
  color: #409eff;
}

.status-done {
  display: flex;
  align-items: center;
  gap: 8px;
  color: #67c23a;
}

.status-acli-error {
  display: flex;
  align-items: center;
  gap: 8px;
  color: #e6a23c;
}

.status-error {
  color: #f56c6c;
}

.status-detail {
  font-size: 12px;
  color: #606266;
  margin-top: 4px;
}

.collect-items {
  margin-top: 8px;
  padding-left: 20px;
  font-size: 12px;
  color: #909399;
}

.error-checklist {
  margin-top: 12px;
  font-size: 13px;
}

.error-checklist ul {
  padding-left: 20px;
  color: #606266;
}

.button-area {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-top: 20px;
}

.btn-connect {
  width: 100%;
  height: 40px;
}

.btn-cancel {
  width: 100%;
  height: 36px;
  background: #fff;
  border: 1px solid #dcdfe6;
  color: #606266;
}

.btn-no-ssh {
  width: 100%;
  height: 36px;
  background: #f5f5f5;
  border: 1px solid #e4e7ed;
  color: #909399;
}

.assistant-option-compact {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
}

/* 无 SSH 确认弹框 */
.no-ssh-warning {
  text-align: center;
}

.warning-title {
  font-size: 16px;
  font-weight: 600;
  color: #303133;
  margin: 12px 0 8px;
}

.warning-desc {
  font-size: 14px;
  color: #606266;
  margin-bottom: 8px;
}

.warning-list {
  padding-left: 20px;
  text-align: left;
  color: #909399;
  margin-bottom: 12px;
}

.warning-hint {
  font-size: 12px;
  color: #c0c4cc;
}

.btn-no-ssh-confirm {
  background: #f5f5f5;
  border: 1px solid #e4e7ed;
  color: #909399;
}
</style>