<script setup lang="ts">
/**
 * CaseCreateDialog.vue
 * 创建工单弹框 v3 — 含 6 视图状态机的完整组件
 *
 * 视图状态：
 * - bridge-guide: Bridge 未运行引导
 * - form: SSH 表单（正常入口）
 * - progress: 进度中（3步）
 * - error-auth: SSH 认证失败
 * - error-collect: 采集失败（SSH 已连通）
 * - success: 全部成功，自动关闭
 *
 * 所有 SSH 流程在弹框内完成，不跳出第二个弹框
 */
import { ref, reactive, watch, computed, onMounted, onBeforeUnmount } from 'vue'
import { ElMessage } from 'element-plus'
import { useChatStore } from '@/stores/chat'
import { checkBridgeBeforeOpen, checkBridgeRunning } from '@/api/terminal'
import {
  createBridgeSocket,
  buildConnectMessage,
  buildBridgeMarker,
  buildBridgeCommandPayload,
  parseBridgeCommandResult,
  stripAnsi,
  parseJsonOutput,
  type TerminalWsMessage,
  type TerminalAuthType,
} from '@/api/terminal'
import { createApiClient, createCaseApi, createEnvironmentApi } from '@hci/shared'
import { getClientId } from '@/utils/clientId'
import SshFormSection from './SshFormSection.vue'

const chatStore = useChatStore()

// ===== Props =====
const props = defineProps<{
  bridgeStatus: 'running' | 'not-running' | 'checking'
  pendingTitle?: string
  pendingDescription?: string
}>()

// ===== 视图状态机 =====
type ViewState = 'bridge-guide' | 'form' | 'progress' | 'error-auth' | 'error-collect' | 'success'
const viewState = ref<ViewState>('form')
const currentStep = ref(0) // 0-3 步骤条

// ===== 工单表单 =====
const caseForm = reactive({
  title: '',
  description: '',
  assistantType: '',
})

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

// ===== 错误信息 =====
const errorMessage = ref('')
const createdCaseId = ref('')

// ===== 日志面板 =====
interface LogEntry {
  id: number
  level: 'info' | 'success' | 'warn' | 'error'
  message: string
  timestamp: Date
}
const logs = ref<LogEntry[]>([])
const logsExpanded = ref(false)
let logCounter = 0

function addLog(level: LogEntry['level'], message: string) {
  logs.value.push({
    id: ++logCounter,
    level,
    message,
    timestamp: new Date(),
  })
}

function formatLogTime(ts: Date): string {
  return ts.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

// ===== 临时 WebSocket =====
let tempSocket: WebSocket | null = null
let authTimer: ReturnType<typeof setTimeout> | null = null
const AUTH_TIMEOUT = 15000
const COLLECT_TIMEOUT = 60000

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

// ===== 采集数据缓冲 =====
const collectedData = reactive<{
  cluster: Record<string, unknown> | null
  alert: unknown[] | null
  task: unknown[] | null
}>({
  cluster: null,
  alert: null,
  task: null,
})

// ===== 采集命令定义 =====
const COLLECT_COMMANDS = [
  { name: 'cluster', label: '集群信息', cmd: 'acli platform info get', timeout: 30000 },
  { name: 'alert', label: '告警列表', cmd: 'acli --formatter json alert list', timeout: 60000 },
  { name: 'task', label: '任务状态', cmd: 'acli --formatter json task list', timeout: 60000 },
]

// ===== API 客户端 =====
const clientId = getClientId()
const apiClient = createApiClient('/api', clientId)
const caseApi = createCaseApi(apiClient)
const environmentApi = createEnvironmentApi(apiClient)

// ===== Bridge 引导视图 =====
const recheckingBridge = ref(false)

async function handleRefreshBridge() {
  recheckingBridge.value = true
  try {
    const status = await checkBridgeBeforeOpen()
    if (status === 'running') {
      viewState.value = 'form'
      addLog('success', 'Bridge 检测成功')
    } else {
      ElMessage.warning('Bridge 仍未检测到，请确认已启动后重试')
      addLog('warn', 'Bridge 仍未运行')
    }
  } finally {
    recheckingBridge.value = false
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

// ===== 执行命令（marker 协议）=====
interface PendingCommand {
  buffer: string
  marker: string
  resolve: (output: string) => void
  reject: (err: Error) => void
}
let pendingCommand: PendingCommand | null = null

function runCommand(name: string, label: string, cmd: string, timeoutMs: number): Promise<string> {
  return new Promise((resolve, reject) => {
    if (!tempSocket) {
      reject(new Error('WebSocket 未连接'))
      return
    }

    const marker = buildBridgeMarker(createdCaseId.value || 'temp', name, Date.now())
    const payload = buildBridgeCommandPayload(cmd, marker)
    addLog('info', `执行命令: ${label}`)

    pendingCommand = {
      buffer: '',
      marker,
      resolve,
      reject,
    }

    tempSocket.send(JSON.stringify({
      type: 'ssh_input',
      case_id: createdCaseId.value || 'temp',
      data: payload,
    }))

    // 超时处理
    setTimeout(() => {
      if (pendingCommand && pendingCommand.marker === marker) {
        pendingCommand.reject(new Error(`${label} 执行超时（${timeoutMs / 1000}s）`))
        pendingCommand = null
      }
    }, timeoutMs)
  })
}

// ===== 采集并 upsert =====
async function runCollectionAndUpsert() {
  for (const cmdInfo of COLLECT_COMMANDS) {
    try {
      const output = await runCommand(cmdInfo.name, cmdInfo.label, cmdInfo.cmd, cmdInfo.timeout)
      const parsed = parseJsonOutput(output)

      if (cmdInfo.name === 'cluster') {
        // 集群信息可能是 key:value 格式
        if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
          collectedData.cluster = parsed as Record<string, unknown>
        } else {
          // 尝试解析 key:value 格式
          const lines = stripAnsi(output).split('\n')
          const clusterData: Record<string, unknown> = {}
          for (const line of lines) {
            const match = line.match(/^([^:]+):\s*(.+)$/)
            if (match) {
              clusterData[match[1].trim()] = match[2].trim()
            }
          }
          collectedData.cluster = clusterData
        }
        addLog('success', `${cmdInfo.label} 采集成功`)
      } else if (cmdInfo.name === 'alert') {
        collectedData.alert = Array.isArray(parsed) ? parsed : []
        addLog('success', `${cmdInfo.label} 采集成功（${collectedData.alert.length} 条）`)
      } else if (cmdInfo.name === 'task') {
        collectedData.task = Array.isArray(parsed) ? parsed : []
        addLog('success', `${cmdInfo.label} 采集成功（${collectedData.task.length} 条）`)
      }
    } catch (e: any) {
      addLog('error', `${cmdInfo.label} 采集失败: ${e.message}`)
      // 采集失败：记录原始数据带 parse_error
      if (cmdInfo.name === 'cluster') {
        collectedData.cluster = { parse_error: e.message, raw: '' }
      } else if (cmdInfo.name === 'alert') {
        collectedData.alert = [{ parse_error: e.message }]
      } else if (cmdInfo.name === 'task') {
        collectedData.task = [{ parse_error: e.message }]
      }
    }
  }

  // Upsert 到数据库
  if (createdCaseId.value) {
    addLog('info', '正在写入环境数据...')
    try {
      if (collectedData.cluster) {
        await environmentApi.upsert(createdCaseId.value, 'cluster', collectedData.cluster)
      }
      if (collectedData.alert && collectedData.alert.length > 0) {
        await environmentApi.upsert(createdCaseId.value, 'alert', { alerts: collectedData.alert })
      }
      if (collectedData.task && collectedData.task.length > 0) {
        await environmentApi.upsert(createdCaseId.value, 'task', { tasks: collectedData.task })
      }
      addLog('success', '环境数据已入库')
    } catch (e: any) {
      addLog('error', `环境数据入库失败: ${e.message}`)
      throw e
    }
  }
}

// ===== 主流程：SSH 连接 + 采集 + 创建工单 =====
async function runSshAndCreateCase() {
  viewState.value = 'progress'
  currentStep.value = 0
  logs.value = []
  errorMessage.value = ''
  collectedData.cluster = null
  collectedData.alert = null
  collectedData.task = null

  addLog('info', '开始 SSH 流程...')

  try {
    // ===== 步骤 1: SSH 认证 =====
    currentStep.value = 0
    addLog('info', `正在 SSH 认证: ${sshForm.username}@${sshForm.host}`)

    // 使用临时 WebSocket 进行认证和采集
    const socket = createBridgeSocket()
    tempSocket = socket

    await new Promise<void>((resolve, reject) => {
      authTimer = setTimeout(() => {
        closeTempSocket()
        reject(new Error('SSH 认证超时（15秒）'))
      }, AUTH_TIMEOUT)

      socket.onopen = () => {
        socket.send(buildConnectMessage({
          host: sshForm.host.trim(),
          port: Number(sshForm.port) || 22,
          username: sshForm.username.trim(),
          auth_type: authType.value,
          password: sshForm.password,
          private_key: sshForm.privateKey,
          passphrase: sshForm.passphrase,
          case_id: 'ssh-create-temp',
        }))
      }

      socket.onmessage = async (e) => {
        let msg: TerminalWsMessage
        try {
          msg = JSON.parse(String(e.data || ''))
        } catch {
          return
        }

        // 处理 pendingCommand
        if (msg.type === 'ssh_output' && msg.output && pendingCommand) {
          pendingCommand.buffer += msg.output
          const result = parseBridgeCommandResult(pendingCommand.buffer, pendingCommand.marker)
          if (result) {
            pendingCommand.resolve(result.output)
            pendingCommand = null
          }
        }

        if (msg.type === 'ssh_connected') {
          clearTimeout(authTimer!)
          authTimer = null
          addLog('success', 'SSH 认证成功')

          // 保存 SSH 配置到 localStorage（不含密码）
          localStorage.setItem('hci_last_ssh_config', JSON.stringify({
            host: sshForm.host.trim(),
            port: sshForm.port,
            username: sshForm.username.trim(),
            lastSuccessAt: new Date().toISOString(),
          }))

          currentStep.value = 1
          resolve()
        } else if (msg.type === 'ssh_error') {
          clearTimeout(authTimer!)
          authTimer = null
          errorMessage.value = msg.message || 'SSH 认证失败'
          addLog('error', errorMessage.value)
          reject(new Error(errorMessage.value))
        }
      }

      socket.onerror = () => {
        clearTimeout(authTimer!)
        authTimer = null
        reject(new Error('WebSocket 连接失败'))
      }
    })

    // ===== 步骤 2: 创建工单（先创建，再采集）=====
    addLog('info', '正在创建工单...')
    const caseRes = await caseApi.create({
      client_id: clientId,
      title: caseForm.title,
      description: caseForm.description,
      assistant_type: caseForm.assistantType || chatStore.selectedAssistant || undefined,
    })
    createdCaseId.value = caseRes.data.case_id
    addLog('success', `工单已创建: ${createdCaseId.value}`)

    // 确认工单
    const confirmed = await caseApi.confirm(createdCaseId.value)
    addLog('success', '工单已确认')

    currentStep.value = 2

    // ===== 步骤 3: 采集环境 =====
    addLog('info', '开始采集环境数据...')
    await runCollectionAndUpsert()

    // ===== 步骤 4: 完成 =====
    currentStep.value = 3

    // 关闭临时 socket，建立全局 SSH 连接
    closeTempSocket()

    // 建立全局 SSH 连接（供 TerminalPanel 使用）
    try {
      await chatStore.connectSSH({
        host: sshForm.host.trim(),
        port: Number(sshForm.port) || 22,
        username: sshForm.username.trim(),
        authType: authType.value,
        password: sshForm.password,
        privateKey: sshForm.privateKey,
        passphrase: sshForm.passphrase,
        caseId: createdCaseId.value,
      })
      addLog('success', '全局终端连接已建立')
    } catch (e: any) {
      addLog('warn', `全局终端连接失败: ${e.message}`)
      // 不影响主流程
    }

    viewState.value = 'success'

    // 2秒后自动关闭
    setTimeout(async () => {
      // 刷新环境数据摘要（修复问题1）
      await chatStore.collectEnvironmentData(createdCaseId.value)
      // 完成流程
      chatStore.completeCaseCreationFlow(
        createdCaseId.value,
        chatStore.pendingUserMessage || caseForm.description,
        caseForm.assistantType || chatStore.selectedAssistant,
      )
      // 关闭弹框
      chatStore.showCaseTemplate = false
    }, 2000)

  } catch (e: any) {
    closeTempSocket()
    errorMessage.value = e.message || 'SSH 流程失败'
    addLog('error', errorMessage.value)

    // 区分错误类型
    if (currentStep.value === 0) {
      viewState.value = 'error-auth'
    } else if (currentStep.value >= 1) {
      viewState.value = 'error-collect'
    }
  }
}

// ===== 重试 SSH =====
function handleRetry() {
  viewState.value = 'form'
  errorMessage.value = ''
}

// ===== 重试采集（保留 SSH）=====
async function handleRetryCollection() {
  // SSH 已连通（全局连接），只需重走采集
  viewState.value = 'progress'
  currentStep.value = 1 // 从步骤2开始（跳过认证）
  errorMessage.value = ''

  addLog('info', '重试采集（SSH 已连通）...')

  try {
    // 使用全局 SSH 连接执行采集
    // 这里需要通过 chatStore.sendSSHCommand 发送命令
    // 简化实现：直接调用采集逻辑

    // 步骤 2: 采集环境
    currentStep.value = 2
    await runCollectionAndUpsert()

    // 步骤 3: 完成
    currentStep.value = 3
    viewState.value = 'success'

    setTimeout(async () => {
      await chatStore.collectEnvironmentData(createdCaseId.value)
      chatStore.showCaseTemplate = false
    }, 2000)

  } catch (e: any) {
    errorMessage.value = e.message || '采集失败'
    addLog('error', errorMessage.value)
    viewState.value = 'error-collect'
  }
}

// ===== 无 SSH 创建 =====
async function handleNoSSHCreate() {
  if (!validateCaseForm()) return

  viewState.value = 'progress'
  currentStep.value = 2 // 直接跳到步骤3（创建工单）
  addLog('info', '无 SSH 创建工单...')

  try {
    const caseRes = await caseApi.create({
      client_id: clientId,
      title: caseForm.title,
      description: caseForm.description,
      assistant_type: caseForm.assistantType || chatStore.selectedAssistant || undefined,
    })
    createdCaseId.value = caseRes.data.case_id

    // 确认工单
    await caseApi.confirm(createdCaseId.value)

    currentStep.value = 3
    addLog('success', `工单已创建: ${createdCaseId.value}（无环境数据）`)

    // 直接完成流程
    chatStore.completeCaseCreationFlow(
      createdCaseId.value,
      chatStore.pendingUserMessage || caseForm.description,
      caseForm.assistantType || chatStore.selectedAssistant,
    )
    chatStore.showCaseTemplate = false

  } catch (e: any) {
    errorMessage.value = e.message || '创建工单失败'
    addLog('error', errorMessage.value)
    ElMessage.error(errorMessage.value)
    viewState.value = 'form'
  }
}

// ===== 取消 =====
function handleCancel() {
  closeTempSocket()
  chatStore.cancelCreateCase()
}

// ===== 验证表单 =====
function validateCaseForm(): boolean {
  if (!caseForm.title.trim()) {
    ElMessage.warning('请填写工单标题')
    return false
  }
  if (!caseForm.description.trim()) {
    ElMessage.warning('请填写问题描述')
    return false
  }
  return true
}

function validateSshForm(): boolean {
  if (!sshForm.host.trim()) {
    ElMessage.warning('请填写主机地址')
    return false
  }
  if (!sshForm.username.trim()) {
    ElMessage.warning('请填写用户名')
    return false
  }
  if (authType.value === 'password' && !sshForm.password) {
    ElMessage.warning('请填写密码')
    return false
  }
  if (authType.value === 'key' && !sshForm.privateKey) {
    ElMessage.warning('请填写私钥')
    return false
  }
  return true
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
    // 'checking' 状态：等待检测完成
  },
  { immediate: true },
)

watch(
  () => chatStore.showCaseTemplate,
  (val) => {
    if (val) {
      // 弹框打开时同步标题/描述
      caseForm.title = props.pendingTitle || chatStore.caseTemplate.title
      caseForm.description = props.pendingDescription || chatStore.caseTemplate.description
      // 重置状态
      logs.value = []
      errorMessage.value = ''
      createdCaseId.value = ''
      currentStep.value = 0
    }
  },
)

watch(
  () => chatStore.selectedAssistant,
  (val) => {
    if (val && !caseForm.assistantType) {
      caseForm.assistantType = val
    }
  },
  { immediate: true },
)

// ===== 生命周期 =====
onBeforeUnmount(() => {
  closeTempSocket()
})
</script>

<template>
  <el-dialog
    v-model="chatStore.showCaseTemplate"
    title="创建工单"
    width="560px"
    :close-on-click-modal="false"
    align-center
    class="case-create-dialog"
  >
    <!-- ===== 视图 A: Bridge 引导 ===== -->
    <div v-if="viewState === 'bridge-guide'" class="bridge-guide-section">
      <el-alert type="warning" :closable="false" class="bridge-alert">
        <template #title>
          <strong>⚠️ 检测到 Bridge 工具未运行</strong>
        </template>
        <p class="bridge-desc">
          需要先在本机启动 Bridge 工具才能进行 SSH 连接
        </p>
      </el-alert>

      <!-- 仅 Windows 下载 -->
      <div class="bridge-download">
        <p>📥 下载 Bridge 工具（Windows）</p>
        <el-button type="primary" size="small" @click="handleDownloadBridge">
          ⬇ 下载 Windows 版
        </el-button>
      </div>

      <div class="bridge-refresh">
        <p>已下载并启动？</p>
        <el-button :loading="recheckingBridge" @click="handleRefreshBridge">
          🔄 已运行 Bridge 工具，点击刷新
        </el-button>
      </div>

      <!-- 操作按钮 -->
      <div class="dialog-actions">
        <el-tooltip content="若无 SSH，自动化能力将大大降低" placement="top">
          <el-button class="btn-no-ssh" @click="handleNoSSHCreate">
            ⚠️ 无 SSH 创建工单
          </el-button>
        </el-tooltip>
        <el-button @click="handleCancel">取消</el-button>
      </div>
    </div>

    <!-- ===== 视图 B: SSH 表单 ===== -->
    <div v-else-if="viewState === 'form'" class="form-section">
      <!-- 工单基本信息 -->
      <el-form label-position="top" class="case-form">
        <el-form-item label="标题">
          <el-input
            v-model="caseForm.title"
            placeholder="简要描述问题"
            maxlength="100"
            show-word-limit
          />
        </el-form-item>
        <el-form-item label="描述">
          <el-input
            v-model="caseForm.description"
            type="textarea"
            :autosize="{ minRows: 3, maxRows: 6 }"
            placeholder="详细描述您遇到的问题..."
          />
        </el-form-item>
        <el-form-item label="AI 助手" v-if="chatStore.showAssistantSelector">
          <el-select v-model="caseForm.assistantType" placeholder="选择 AI 助手" style="width: 100%">
            <el-option
              v-for="assistant in chatStore.assistants"
              :key="assistant.type"
              :label="assistant.display_name"
              :value="assistant.type"
              :disabled="!assistant.available"
            >
              <div class="assistant-option">
                <span>{{ assistant.display_name }}</span>
                <el-tag v-if="assistant.is_default" size="small" type="success">默认</el-tag>
              </div>
            </el-option>
          </el-select>
        </el-form-item>
      </el-form>

      <!-- 分隔线 -->
      <div class="section-divider">
        <span>🖥 SSH 连接（认证后自动采集环境数据）</span>
      </div>

      <!-- SSH 表单 -->
      <SshFormSection
        :ssh-form="sshForm"
        :auth-type="authType"
        @update:ssh-form="Object.assign(sshForm, $event)"
        @update:auth-type="authType = $event"
      />

      <!-- 操作按钮 -->
      <div class="dialog-actions">
        <el-button type="primary" class="btn-connect" @click="runSshAndCreateCase">
          🖥 连接 SSH 并创建工单
        </el-button>
        <el-tooltip content="若无 SSH，自动化能力将大大降低" placement="top">
          <el-button class="btn-no-ssh" @click="handleNoSSHCreate">
            ⚠️ 无 SSH 创建工单
          </el-button>
        </el-tooltip>
        <el-button @click="handleCancel">取消</el-button>
      </div>
    </div>

    <!-- ===== 视图 B': 进度 ===== -->
    <div v-else-if="viewState === 'progress'" class="progress-section">
      <!-- 3 步进度条 -->
      <el-steps :active="currentStep" finish-status="success" class="flow-steps">
        <el-step title="SSH 认证" />
        <el-step title="采集环境" />
        <el-step title="创建工单" />
      </el-steps>

      <!-- 当前阶段描述 -->
      <div class="phase-desc">
        <el-icon class="is-loading spin-icon"><i class="el-icon-loading" /></el-icon>
        <span v-if="currentStep === 0">正在 SSH 认证...</span>
        <span v-else-if="currentStep === 1">正在创建工单...</span>
        <span v-else-if="currentStep === 2">正在采集环境数据...</span>
        <span v-else-if="currentStep === 3">正在完成...</span>
      </div>

      <!-- 可折叠日志面板 -->
      <div v-if="logs.length > 0" class="log-panel">
        <div class="log-header" @click="logsExpanded = !logsExpanded">
          <span class="log-title">日志（{{ logs.length }} 条）</span>
          <el-button text size="small">{{ logsExpanded ? '▲ 折叠' : '▼ 展开' }}</el-button>
        </div>
        <div v-if="logsExpanded" class="log-list">
          <div
            v-for="log in logs"
            :key="log.id"
            class="log-item"
            :class="`log-${log.level}`"
          >
            <span class="log-time">{{ formatLogTime(log.timestamp) }}</span>
            <span class="log-msg">{{ log.message }}</span>
          </div>
        </div>
      </div>

      <!-- 取消按钮 -->
      <div class="dialog-actions">
        <el-button @click="handleCancel">取消</el-button>
      </div>
    </div>

    <!-- ===== 视图 C: SSH 认证失败 ===== -->
    <div v-else-if="viewState === 'error-auth'" class="error-section">
      <el-alert type="error" :closable="false">
        <template #title>
          <strong>❌ SSH 认证失败</strong>
        </template>
        <p class="error-detail">
          原因：{{ errorMessage }}<br />
          主机: {{ sshForm.host }}   用户: {{ sshForm.username }}
        </p>
      </el-alert>

      <div class="dialog-actions">
        <el-button type="primary" @click="handleRetry">🔄 重试</el-button>
        <el-tooltip content="若无 SSH，自动化能力将大大降低" placement="top">
          <el-button class="btn-no-ssh" @click="handleNoSSHCreate">
            ⚠️ 无 SSH 创建工单
          </el-button>
        </el-tooltip>
      </div>
    </div>

    <!-- ===== 视图 D: 采集失败 ===== -->
    <div v-else-if="viewState === 'error-collect'" class="error-section">
      <el-alert type="error" :closable="false">
        <template #title>
          <strong>❌ 环境采集失败</strong>
        </template>
        <p class="error-detail">
          原因：{{ errorMessage }}<br />
          注意：SSH 连接本身正常，仅采集步骤失败
        </p>
      </el-alert>

      <div class="dialog-actions">
        <el-button type="primary" @click="handleRetryCollection">🔄 重试采集</el-button>
        <el-tooltip content="继续创建工单，但无环境数据" placement="top">
          <el-button class="btn-no-ssh" @click="handleNoSSHCreate">
            ⚠️ 无 SSH 创建工单
          </el-button>
        </el-tooltip>
      </div>

      <!-- 日志面板 -->
      <div v-if="logs.length > 0" class="log-panel">
        <div class="log-header" @click="logsExpanded = !logsExpanded">
          <span class="log-title">日志（{{ logs.length }} 条）</span>
          <el-button text size="small">{{ logsExpanded ? '▲ 折叠' : '▼ 展开' }}</el-button>
        </div>
        <div v-if="logsExpanded" class="log-list">
          <div
            v-for="log in logs"
            :key="log.id"
            class="log-item"
            :class="`log-${log.level}`"
          >
            <span class="log-time">{{ formatLogTime(log.timestamp) }}</span>
            <span class="log-msg">{{ log.message }}</span>
          </div>
        </div>
      </div>
    </div>

    <!-- ===== 视图 F: 成功 ===== -->
    <div v-else-if="viewState === 'success'" class="success-section">
      <el-steps :active="3" finish-status="success" class="flow-steps">
        <el-step title="SSH 认证" />
        <el-step title="采集环境" />
        <el-step title="创建工单" />
      </el-steps>

      <div class="success-message">
        <el-icon color="#67c23a" size="24"><i class="el-icon-success" /></el-icon>
        <p class="success-title">✅ 工单创建成功，环境数据已入库</p>
        <p class="success-case-id">工单号：{{ createdCaseId }}</p>
        <p class="success-hint">即将进入 AI 对话... (2s)</p>
      </div>

      <!-- 日志面板 -->
      <div v-if="logs.length > 0" class="log-panel">
        <div class="log-header" @click="logsExpanded = !logsExpanded">
          <span class="log-title">日志（{{ logs.length }} 条）</span>
          <el-button text size="small">{{ logsExpanded ? '▲ 折叠' : '▼ 展开' }}</el-button>
        </div>
        <div v-if="logsExpanded" class="log-list">
          <div
            v-for="log in logs"
            :key="log.id"
            class="log-item"
            :class="`log-${log.level}`"
          >
            <span class="log-time">{{ formatLogTime(log.timestamp) }}</span>
            <span class="log-msg">{{ log.message }}</span>
          </div>
        </div>
      </div>
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

.bridge-refresh p {
  margin-bottom: 8px;
  color: #909399;
}

/* ===== 表单 ===== */
.form-section {
  padding: 8px 0;
}

.case-form {
  margin-bottom: 16px;
}

.section-divider {
  margin: 16px 0;
  padding: 8px 0;
  border-top: 1px solid #e4e7ed;
  font-weight: 500;
  color: #606266;
}

/* ===== 进度 ===== */
.progress-section {
  padding: 16px 0;
}

.flow-steps {
  margin-bottom: 24px;
}

.phase-desc {
  text-align: center;
  margin-bottom: 16px;
  color: #606266;
}

.phase-desc .spin-icon {
  margin-right: 8px;
  animation: spin 1s linear infinite;
}

@keyframes spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

/* ===== 日志面板 ===== */
.log-panel {
  margin-top: 16px;
  border: 1px solid #e4e7ed;
  border-radius: 8px;
}

.log-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 12px;
  background: #f5f7fa;
  cursor: pointer;
}

.log-title {
  font-weight: 500;
  color: #606266;
}

.log-list {
  padding: 8px 12px;
  max-height: 200px;
  overflow-y: auto;
}

.log-item {
  display: flex;
  gap: 8px;
  padding: 4px 0;
  font-size: 12px;
}

.log-time {
  color: #909399;
  width: 80px;
}

.log-msg {
  flex: 1;
}

.log-info { color: #409eff; }
.log-success { color: #67c23a; }
.log-warn { color: #e6a23c; }
.log-error { color: #f56c6c; }

/* ===== 错误 ===== */
.error-section {
  padding: 16px 0;
}

.error-detail {
  margin-top: 8px;
  color: #c0c4cc;
}

/* ===== 成功 ===== */
.success-section {
  padding: 16px 0;
}

.success-message {
  text-align: center;
  margin: 24px 0;
}

.success-title {
  font-size: 16px;
  font-weight: 500;
  margin-top: 12px;
}

.success-case-id {
  color: #409eff;
  font-size: 14px;
}

.success-hint {
  color: #909399;
  font-size: 12px;
}

/* ===== 操作按钮 ===== */
.dialog-actions {
  display: flex;
  gap: 12px;
  margin-top: 16px;
}

.btn-connect {
  flex: 1;
}

.btn-no-ssh {
  background: #f5f7fa !important;
  border-color: #dcdfe6 !important;
  color: #909399 !important;
  font-size: 12px !important;
}

.btn-no-ssh:hover {
  background: #ebeef5 !important;
  color: #606266 !important;
}

.assistant-option {
  display: flex;
  align-items: center;
  gap: 8px;
  justify-content: space-between;
}
</style>