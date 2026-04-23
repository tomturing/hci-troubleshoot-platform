<script setup lang="ts">
/**
 * SshFlowPanel.vue
 * SSH 连接流程面板 - 统一两个入口（创建工单 / SSH终端按钮）的 SSH 体验
 *
 * 模式：
 *   create-case   → SSH 成功后调用 completeCaseCreationFlow，创建对话
 *   terminal-only → 仅建立全局 SSH 连接，用于有工单时重新连接
 *
 * 内部流程：
 *   checkBridge → SSH 表单 → connecting → acli-check → collecting → done
 */
import { ref, reactive, onMounted } from 'vue'
import { getClientId } from '@/utils/clientId'
import { createApiClient, createEnvironmentApi } from '@hci/shared'
import {
  checkBridgeRunning,
  createBridgeSocket,
  buildConnectMessage,
  buildInputMessage,
  buildBridgeMarker,
  buildBridgeCommandPayload,
  parseBridgeCommandResult,
  stripAnsi,
  parseJsonOutput,
  type TerminalWsMessage,
} from '@/api/terminal'
import { useChatStore } from '@/stores/chat'

// ─── Props & Emits ─────────────────────────────────────────────────────────
interface Props {
  mode: 'create-case' | 'terminal-only'
  caseId: string | null
}

const props = defineProps<Props>()

const emit = defineEmits<{
  success: []
  cancel: []
}>()

// ─── 常量 ───────────────────────────────────────────────────────────────────
const BRIDGE_DOWNLOAD_URL =
  import.meta.env.VITE_BRIDGE_DOWNLOAD_URL || '/downloads/terminal_bridge.exe'

const COLLECT_COMMANDS = [
  { name: 'cluster', label: '采集集群信息', cmd: 'acli platform info get', timeoutMs: 30000 },
  { name: 'alert',   label: '采集告警列表', cmd: 'acli --formatter json alert list', timeoutMs: 60000 },
  { name: 'task',    label: '采集任务列表', cmd: 'acli --formatter json task list',  timeoutMs: 60000 },
] as const

type Phase =
  | 'idle'
  | 'checking-bridge'
  | 'bridge-not-running'
  | 'ready'
  | 'connecting'
  | 'acli-check'
  | 'collecting'
  | 'done'
  | 'acli-not-found'
  | 'error'

interface LogEntry {
  id: number
  level: 'info' | 'warn' | 'error' | 'success'
  message: string
  timestamp: string
}

// ─── 状态 ───────────────────────────────────────────────────────────────────
const chatStore = useChatStore()
const clientId = getClientId()
const environmentApi = createEnvironmentApi(createApiClient('/api', clientId))

const phase = ref<Phase>('idle')
const errorMessage = ref('')
const logsExpanded = ref(false)  // 日志默认折叠
const logs = ref<LogEntry[]>([])
let logCounter = 0

const sshForm = reactive({
  host: '',
  port: '22',
  username: '',
  password: '',
})

// 当前步骤（0=连接, 1=检测acli, 2=采集, 3=完成）
const currentStep = ref(0)

// 临时 WebSocket（仅用于命令执行，采集完成后断开）
let tempSocket: WebSocket | null = null

// ─── 工具函数 ───────────────────────────────────────────────────────────────
function addLog(level: LogEntry['level'], message: string) {
  logs.value.push({
    id: ++logCounter,
    level,
    message,
    timestamp: new Date().toLocaleTimeString('zh-CN', { hour12: false }),
  })
  console.log(`[SshFlowPanel][${level.toUpperCase()}] ${message}`)
}

function formatLogTime(ts: string) {
  return ts
}

function closeTempSocket() {
  if (tempSocket) {
    try { tempSocket.close() } catch { /* ignore */ }
    tempSocket = null
  }
}

// ─── Bridge 检测 ────────────────────────────────────────────────────────────
async function detectBridge() {
  phase.value = 'checking-bridge'
  addLog('info', '正在检测本地 SSH Bridge...')
  const running = await checkBridgeRunning()
  if (running) {
    addLog('success', 'Bridge 检测成功，可以连接 SSH')
    phase.value = 'ready'
  } else {
    addLog('warn', 'Bridge 未运行，请下载并启动 terminal_bridge.exe')
    phase.value = 'bridge-not-running'
  }
}

// ─── 自动填充 localStorage SSH 配置 ─────────────────────────────────────────
function loadSavedSshConfig() {
  try {
    const saved = localStorage.getItem('hci_last_ssh_config')
    if (saved) {
      const config = JSON.parse(saved)
      sshForm.host = config.host || ''
      sshForm.port = String(config.port || 22)
      sshForm.username = config.username || ''
    }
  } catch { /* ignore */ }
}

// ─── 采集数据提交（upsert） ──────────────────────────────────────────────────
async function submitBuffer(buffer: Record<string, string>) {
  const caseId = props.caseId
  if (!caseId) {
    addLog('warn', '无工单 ID，跳过采集数据提交')
    return
  }

  console.log('[SshFlowPanel][submitCollectedData] 开始提交', { caseId })

  // 提交 cluster
  if (buffer.cluster) {
    try {
      const cleaned = stripAnsi(buffer.cluster)
      // 尝试 JSON 解析，回退到 key:value 解析
      const jsonData = parseJsonOutput(cleaned)
      let clusterData: Record<string, unknown>
      if (jsonData && typeof jsonData === 'object' && !Array.isArray(jsonData)) {
        clusterData = jsonData as Record<string, unknown>
      } else {
        clusterData = {}
        for (const line of cleaned.split('\n')) {
          if (line.includes(':')) {
            const idx = line.indexOf(':')
            const k = line.slice(0, idx).trim()
            const v = line.slice(idx + 1).trim()
            if (k && v) clusterData[k.toLowerCase().replace(/\s+/g, '_')] = v
          }
        }
      }
      await environmentApi.upsert(caseId, 'cluster', clusterData)
      addLog('success', '集群信息 upsert 成功')
      console.log('[SshFlowPanel][cluster] upsert 成功', clusterData)
    } catch (e) {
      addLog('warn', `集群信息提交失败: ${e}`)
      console.error('[SshFlowPanel][cluster] upsert 失败', e)
    }
  }

  // 提交 alert
  if (buffer.alert) {
    try {
      const cleaned = stripAnsi(buffer.alert)
      const parsed = parseJsonOutput(cleaned)
      let alertList: unknown[] | null = null
      if (Array.isArray(parsed)) {
        alertList = parsed
      } else if (parsed && typeof parsed === 'object') {
        const obj = parsed as Record<string, unknown>
        const candidate = obj['entities'] ?? obj['alerts'] ?? obj['data'] ?? null
        if (Array.isArray(candidate)) alertList = candidate
      }
      if (alertList !== null) {
        await environmentApi.upsert(caseId, 'alert', { alerts: alertList })
        addLog('success', `告警列表 upsert 成功（${alertList.length} 条）`)
      } else {
        addLog('warn', '告警数据无法解析为列表，以原始格式提交')
        await environmentApi.upsert(caseId, 'alert', { raw_output: cleaned, parse_error: '无法提取 alert 列表' })
      }
      console.log('[SshFlowPanel][alert] upsert 成功')
    } catch (e) {
      addLog('warn', `告警列表提交失败: ${e}`)
      console.error('[SshFlowPanel][alert] upsert 失败', e)
    }
  }

  // 提交 task
  if (buffer.task) {
    try {
      const cleaned = stripAnsi(buffer.task)
      const parsed = parseJsonOutput(cleaned)
      let taskList: unknown[] | null = null
      if (Array.isArray(parsed)) {
        taskList = parsed
      } else if (parsed && typeof parsed === 'object') {
        const obj = parsed as Record<string, unknown>
        const candidate = obj['entities'] ?? obj['tasks'] ?? obj['data'] ?? null
        if (Array.isArray(candidate)) taskList = candidate
      }
      if (taskList !== null) {
        await environmentApi.upsert(caseId, 'task', { tasks: taskList })
        addLog('success', `任务列表 upsert 成功（${taskList.length} 条）`)
      } else {
        addLog('warn', '任务数据无法解析为列表，以原始格式提交')
        await environmentApi.upsert(caseId, 'task', { raw_output: cleaned, parse_error: '无法提取 task 列表' })
      }
      console.log('[SshFlowPanel][task] upsert 成功')
    } catch (e) {
      addLog('warn', `任务列表提交失败: ${e}`)
      console.error('[SshFlowPanel][task] upsert 失败', e)
    }
  }

  console.log('[SshFlowPanel][submitCollectedData] 提交完成', { caseId })
}

// ─── 主 SSH 连接流程 ─────────────────────────────────────────────────────────
async function startSshFlow() {
  if (!sshForm.host.trim() || !sshForm.username.trim() || !sshForm.password.trim()) {
    errorMessage.value = '请填写主机地址、用户名和密码'
    return
  }

  phase.value = 'connecting'
  currentStep.value = 0
  errorMessage.value = ''
  addLog('info', `正在连接 SSH: ${sshForm.username}@${sshForm.host}:${sshForm.port}`)

  try {
    await new Promise<void>((resolve, reject) => {
      (async () => {
        // ── 步骤1：SSH 连接 ──────────────────────────────────────────────
        const socket = createBridgeSocket()
        tempSocket = socket

        const caseId = props.caseId || 'flow'
        const commandBuffer: Record<string, string> = {}
        let pendingCommand: { name: string; marker: string; buffer: string; timeout: ReturnType<typeof setTimeout> } | null = null
        let acliAvailable = false
        let flowPhase: 'connecting' | 'acli' | 'collecting' | 'done' = 'connecting'
        let collectionQueue: typeof COLLECT_COMMANDS[number][] = []

        // 15 秒 SSH 认证超时
        const authTimer = setTimeout(() => {
          if (flowPhase === 'connecting') {
            reject(new Error('SSH 认证超时（15秒）'))
          }
        }, 15000)

        // 执行单条命令（marker 协议）
        function runCommand(name: string, label: string, cmd: string, timeoutMs: number): Promise<string> {
          return new Promise((res, rej) => {
            const marker = buildBridgeMarker(caseId, name, Date.now())
            let buf = ''
            addLog('info', `执行: ${label}`)
            console.log(`[SshFlowPanel][cmd] ${label}: ${cmd}`)

            const timer = setTimeout(() => {
              pendingCommand = null
              rej(new Error(`${label} 超时（${Math.ceil(timeoutMs / 1000)} 秒）`))
            }, timeoutMs)

            pendingCommand = {
              name,
              marker,
              buffer: '',
              timeout: timer,
            }

            socket.send(buildInputMessage(caseId, buildBridgeCommandPayload(cmd, marker)))

            // 使用轮询检查（pendingCommand.buffer 由 onmessage 填充）
            const poll = setInterval(() => {
              if (!pendingCommand) {
                clearInterval(poll)
                return
              }
              const result = parseBridgeCommandResult(pendingCommand.buffer, marker)
              if (result) {
                clearInterval(poll)
                clearTimeout(pendingCommand.timeout)
                pendingCommand = null
                buf = result.output
                if (result.exitCode !== 0) {
                  rej(new Error(`${label} 失败（exit=${result.exitCode}）`))
                } else {
                  res(buf)
                }
              }
            }, 100)

            // 把 poll 也存入，方便清理
            ;(pendingCommand as any).__poll = poll
          })
        }

        // 执行采集队列
        async function runCollection() {
          phase.value = 'collecting'
          currentStep.value = 2
          addLog('info', '开始采集环境数据...')

          for (const cmd of collectionQueue) {
            try {
              const output = await runCommand(cmd.name, cmd.label, cmd.cmd, cmd.timeoutMs)
              commandBuffer[cmd.name] = output
              addLog('success', `${cmd.label} 完成`)
            } catch (e: any) {
              addLog('warn', `${cmd.label} 失败（${e.message}），跳过`)
              console.warn(`[SshFlowPanel][collection] ${cmd.label} 失败`, e)
            }
          }

          // upsert 采集数据
          await submitBuffer(commandBuffer)
        }

        socket.onerror = () => {
          clearTimeout(authTimer)
          if (pendingCommand) {
            clearTimeout(pendingCommand.timeout)
            clearInterval((pendingCommand as any).__poll)
          }
          reject(new Error('Bridge 未运行（ws://localhost:9999）'))
        }

        socket.onopen = () => {
          addLog('info', 'WebSocket 已连接，正在 SSH 认证...')
          socket.send(buildConnectMessage({
            host: sshForm.host.trim(),
            port: Number(sshForm.port) || 22,
            username: sshForm.username.trim(),
            auth_type: 'password',
            password: sshForm.password,
            case_id: caseId,
          }))
        }

        socket.onmessage = async (e) => {
          let msg: TerminalWsMessage
          try {
            msg = JSON.parse(String(e.data || ''))
          } catch {
            return
          }

          // 累积 pendingCommand buffer
          if (msg.type === 'ssh_output' && msg.output && pendingCommand) {
            pendingCommand.buffer += msg.output
          }

          if (msg.type === 'ssh_connected') {
            clearTimeout(authTimer)
            addLog('success', `SSH 认证成功：${sshForm.username}@${sshForm.host}`)
            currentStep.value = 1

            // 保存 SSH 配置到 localStorage（不含密码）
            try {
              localStorage.setItem('hci_last_ssh_config', JSON.stringify({
                host: sshForm.host.trim(),
                port: sshForm.port,
                username: sshForm.username.trim(),
                lastSuccessAt: new Date().toISOString(),
              }))
            } catch { /* ignore */ }

            // ── 步骤2：acli 检测 ──────────────────────────────────────────
            flowPhase = 'acli'
            phase.value = 'acli-check'
            addLog('info', '正在检测 acli 工具...')

            try {
              await runCommand('acli_check', '检测 acli', 'acli version 2>&1 || acli --version 2>&1 || which acli', 10000)
              acliAvailable = true
              addLog('success', 'acli 工具可用，准备采集数据')
            } catch {
              // 更宽松的检测：如果 exit != 0 但有 acli 关键字也算可用
              acliAvailable = false
              addLog('warn', 'acli 工具不可用，跳过采集')
              phase.value = 'acli-not-found'
              currentStep.value = 3
            }

            // ── 步骤3：采集（仅当有 caseId 且 acli 可用）──────────────────
            if (acliAvailable && props.caseId) {
              flowPhase = 'collecting'
              collectionQueue = [...COLLECT_COMMANDS]
              await runCollection()
            } else if (!props.caseId) {
              addLog('info', '无工单 ID，跳过采集')
            }

            // ── 步骤4：完成 ──────────────────────────────────────────────
            flowPhase = 'done'
            phase.value = 'done'
            currentStep.value = 3
            addLog('success', '流程完成，正在建立全局终端连接...')

            // 断开临时 socket
            closeTempSocket()

            // 建立全局 SSH 连接（供 TerminalPanel 使用）
            await chatStore.connectSSH({
              host: sshForm.host.trim(),
              port: Number(sshForm.port) || 22,
              username: sshForm.username.trim(),
              authType: 'password',
              password: sshForm.password,
              caseId: props.caseId || 'default',
            })
            addLog('success', '全局终端连接已建立')

            // create-case 模式：启动对话
            if (props.mode === 'create-case' && props.caseId) {
              addLog('info', '正在启动对话...')
              await chatStore.completeCaseCreationFlow(
                props.caseId,
                chatStore.pendingUserMessage,
                chatStore.sshFlowDialogAssistantType,
              )
            }

            resolve()
          } else if (msg.type === 'ssh_error') {
            clearTimeout(authTimer)
            reject(new Error(msg.message || 'SSH 连接出错'))
          } else if (msg.type === 'ssh_disconnected') {
            clearTimeout(authTimer)
            if (flowPhase !== 'done') {
              reject(new Error('SSH 连接意外断开'))
            }
          }
        }

        socket.onclose = () => {
          clearTimeout(authTimer)
          if (flowPhase !== 'done') {
            reject(new Error('WebSocket 连接已关闭'))
          }
        }
      })().catch(reject)
    })

    // 流程成功完成
    chatStore.openTerminalSidebar()
    emit('success')
  } catch (e: any) {
    closeTempSocket()
    phase.value = 'error'
    errorMessage.value = e.message || 'SSH 流程失败'
    addLog('error', `流程失败: ${e.message}`)
    console.error('[SshFlowPanel] 流程失败', e)
  }
}

// ─── 重试 ─────────────────────────────────────────────────────────────────
function retryFlow() {
  phase.value = 'ready'
  errorMessage.value = ''
  currentStep.value = 0
}

// ─── 下载 Bridge ───────────────────────────────────────────────────────────
function handleDownloadBridge() {
  const a = document.createElement('a')
  a.href = BRIDGE_DOWNLOAD_URL
  a.download = 'terminal_bridge.exe'
  a.style.display = 'none'
  document.body.appendChild(a)
  a.click()
  setTimeout(() => document.body.removeChild(a), 200)
}

// ─── 重新检测 Bridge ──────────────────────────────────────────────────────
async function handleRefreshBridge() {
  await detectBridge()
}

// ─── 取消 ─────────────────────────────────────────────────────────────────
function handleCancel() {
  closeTempSocket()
  emit('cancel')
}

// ─── 无 SSH 创建（仅 create-case 模式） ───────────────────────────────────
async function handleNoSshCreate() {
  closeTempSocket()
  chatStore.closeSshFlowDialog()  // 关闭弹框，会触发 fallback 创建对话
}

// ─── 生命周期 ─────────────────────────────────────────────────────────────
onMounted(async () => {
  loadSavedSshConfig()
  await detectBridge()
})
</script>

<template>
  <div class="ssh-flow-panel">
    <!-- Bridge 检测中 -->
    <div v-if="phase === 'checking-bridge'" class="phase-center">
      <el-icon class="is-loading spin-icon"><i class="el-icon-loading" /></el-icon>
      <p>正在检测 SSH Bridge...</p>
    </div>

    <!-- Bridge 未运行 -->
    <div v-else-if="phase === 'bridge-not-running'" class="bridge-warning-section">
      <el-alert type="warning" :closable="false" class="bridge-alert">
        <template #title>
          <strong>⚠️ SSH Bridge 未运行</strong>
        </template>
        <p class="bridge-desc">
          浏览器无法连接 ws://localhost:9999<br />
          请下载并启动 terminal_bridge.exe，然后点击「重新检测」
        </p>
        <div class="bridge-actions">
          <el-button type="primary" size="small" @click="handleDownloadBridge">
            ⬇ 下载 SSH Bridge
          </el-button>
          <el-button size="small" @click="handleRefreshBridge">
            🔄 重新检测
          </el-button>
        </div>
      </el-alert>

      <!-- 无 SSH 创建按钮（仅 create-case 模式） -->
      <div v-if="mode === 'create-case'" class="no-ssh-area">
        <el-tooltip content="若无 SSH，自动化能力将大大降低，AI 需要您手动提供更多信息" placement="top">
          <el-button class="btn-no-ssh" @click="handleNoSshCreate">
            ⚠️ 无 SSH 创建工单
          </el-button>
        </el-tooltip>
      </div>
    </div>

    <!-- SSH 表单（ready 或 error 状态） -->
    <div v-else-if="phase === 'ready' || phase === 'error'" class="ssh-form-section">
      <!-- 错误提示 -->
      <el-alert
        v-if="phase === 'error'"
        type="error"
        :closable="false"
        class="error-alert"
      >
        <template #title>
          <strong>❌ {{ errorMessage }}</strong>
        </template>
        <div class="error-checklist">
          <p>请检查：</p>
          <ul>
            <li>主机地址和端口是否正确</li>
            <li>用户名是否有 SSH 登录权限</li>
            <li>密码是否正确</li>
            <li>目标主机 SSH 服务是否运行</li>
          </ul>
        </div>
      </el-alert>

      <!-- SSH 输入表单 -->
      <el-form label-position="top" size="small" class="ssh-form">
        <div class="form-row">
          <el-form-item label="主机地址" class="form-host">
            <el-input v-model="sshForm.host" placeholder="192.168.1.100" />
          </el-form-item>
          <el-form-item label="端口" class="form-port">
            <el-input v-model="sshForm.port" placeholder="22" />
          </el-form-item>
        </div>
        <div class="form-row">
          <el-form-item label="用户名" class="form-half">
            <el-input v-model="sshForm.username" placeholder="root" />
          </el-form-item>
          <el-form-item label="密码" class="form-half">
            <el-input v-model="sshForm.password" type="password" placeholder="请输入密码" show-password />
          </el-form-item>
        </div>
        <p v-if="sshForm.host && sshForm.username" class="ssh-autofill-hint">
          💡 上次成功连接的信息已自动填充
        </p>
      </el-form>

      <!-- 操作按钮 -->
      <div class="action-area">
        <el-button
          type="primary"
          class="btn-connect"
          @click="startSshFlow"
        >
          {{ phase === 'error' ? '重新连接' : '连接' }}
        </el-button>
        <el-button @click="handleCancel">取消</el-button>
      </div>

      <!-- 无 SSH 创建（仅 create-case 模式） -->
      <div v-if="mode === 'create-case'" class="no-ssh-area">
        <el-tooltip content="若无 SSH，自动化能力将大大降低，AI 需要您手动提供更多信息" placement="top">
          <el-button class="btn-no-ssh" @click="handleNoSshCreate">
            ⚠️ 无 SSH 创建工单
          </el-button>
        </el-tooltip>
      </div>
    </div>

    <!-- 进行中（connecting / acli-check / collecting / acli-not-found） -->
    <div
      v-else-if="['connecting', 'acli-check', 'collecting', 'acli-not-found'].includes(phase)"
      class="progress-section"
    >
      <!-- 步骤条 -->
      <el-steps :active="currentStep" finish-status="success" class="flow-steps">
        <el-step title="SSH 连接" />
        <el-step title="检测 acli" />
        <el-step title="采集数据" />
        <el-step title="完成" />
      </el-steps>

      <!-- 当前阶段描述 -->
      <div class="phase-desc">
        <el-icon v-if="phase !== 'acli-not-found'" class="is-loading spin-icon">
          <i class="el-icon-loading" />
        </el-icon>
        <el-icon v-else color="#e6a23c"><i class="el-icon-warning" /></el-icon>
        <span v-if="phase === 'connecting'">正在 SSH 认证...</span>
        <span v-else-if="phase === 'acli-check'">正在检测 acli 工具...</span>
        <span v-else-if="phase === 'collecting'">正在采集环境数据...</span>
        <span v-else-if="phase === 'acli-not-found'">acli 未安装，跳过采集...</span>
      </div>
    </div>

    <!-- 完成 -->
    <div v-else-if="phase === 'done'" class="done-section">
      <el-steps :active="4" finish-status="success" class="flow-steps">
        <el-step title="SSH 连接" />
        <el-step title="检测 acli" />
        <el-step title="采集数据" />
        <el-step title="完成" />
      </el-steps>
      <div class="done-desc">
        <el-icon color="#67c23a"><i class="el-icon-success" /></el-icon>
        <span>SSH 连接并采集完成，正在打开终端...</span>
      </div>
    </div>

    <!-- 可折叠日志面板 -->
    <div v-if="logs.length > 0" class="log-panel">
      <div class="log-header" @click="logsExpanded = !logsExpanded">
        <span class="log-title">过程日志（{{ logs.length }} 条）</span>
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
</template>

<style scoped>
.ssh-flow-panel {
  padding: 8px 0;
}

/* 居中状态 */
.phase-center {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
  padding: 24px;
  color: #606266;
}

.spin-icon {
  font-size: 24px;
  color: #409eff;
}

/* Bridge 警告 */
.bridge-warning-section {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.bridge-alert :deep(.el-alert__content) {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.bridge-desc {
  color: #606266;
  font-size: 13px;
  line-height: 1.8;
  margin: 4px 0;
}

.bridge-actions {
  display: flex;
  gap: 8px;
  margin-top: 8px;
}

/* SSH 表单 */
.ssh-form-section {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.error-alert {
  margin-bottom: 8px;
}

.error-checklist ul {
  margin: 8px 0 0 16px;
  color: #f56c6c;
  font-size: 12px;
  line-height: 1.8;
}

.ssh-form {
  margin: 0;
}

.form-row {
  display: flex;
  gap: 12px;
}

.form-host {
  flex: 3;
}

.form-port {
  flex: 1;
}

.form-half {
  flex: 1;
}

.ssh-autofill-hint {
  font-size: 12px;
  color: #909399;
  margin: 0;
}

.action-area {
  display: flex;
  gap: 8px;
}

.btn-connect {
  flex: 1;
}

/* 无 SSH 创建 */
.no-ssh-area {
  margin-top: 4px;
}

.btn-no-ssh {
  width: 100%;
  background: #f5f7fa !important;
  border-color: #dcdfe6 !important;
  color: #909399 !important;
  font-size: 12px !important;
}

.btn-no-ssh:hover {
  background: #ebeef5 !important;
  color: #606266 !important;
}

/* 进度 */
.progress-section {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.flow-steps {
  padding: 8px 0;
}

.phase-desc {
  display: flex;
  align-items: center;
  gap: 8px;
  color: #606266;
  font-size: 14px;
  padding: 8px 0;
}

/* 完成 */
.done-section {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.done-desc {
  display: flex;
  align-items: center;
  gap: 8px;
  color: #67c23a;
  font-size: 14px;
  padding: 8px 0;
}

/* 日志面板 */
.log-panel {
  margin-top: 12px;
  border: 1px solid #ebeef5;
  border-radius: 4px;
  overflow: hidden;
}

.log-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 6px 12px;
  background: #f5f7fa;
  cursor: pointer;
  user-select: none;
}

.log-title {
  font-size: 12px;
  color: #606266;
  font-weight: 500;
}

.log-list {
  max-height: 180px;
  overflow-y: auto;
  padding: 4px 0;
  background: #1e1e1e;
}

.log-item {
  display: flex;
  gap: 8px;
  padding: 3px 12px;
  font-size: 11px;
  font-family: 'Consolas', 'Monaco', monospace;
  line-height: 1.6;
}

.log-time {
  color: #6a9955;
  flex-shrink: 0;
}

.log-info .log-msg { color: #d4d4d4; }
.log-warn .log-msg { color: #e6a23c; }
.log-error .log-msg { color: #f56c6c; }
.log-success .log-msg { color: #67c23a; }
</style>
