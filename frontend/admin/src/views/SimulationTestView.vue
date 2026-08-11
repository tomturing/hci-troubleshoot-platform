<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'

type BuildState = 'idle' | 'building' | 'succeeded' | 'failed'
type TestState = 'idle' | 'connecting' | 'running' | 'passed' | 'failed'

interface Capability {
  support_id: string
  requested_revision: number
  runtime_revision: number
  bundle_digest: string
  bundle_status: string
  authority_scope: string
  synthetic: boolean
  buildable: boolean
  capability_gap: string[]
}

interface ConnectionInfo {
  host?: string
  port?: string | number
  username?: string
  auth_type?: string
  password?: string
  execution_mode?: string
  test_run_id?: string
}

const endpoint = (import.meta.env.VITE_HCI_SIM_CONTROL_PLANE_URL || '/api/hci-sim').replace(/\/$/, '')
const kbdId = ref(localStorage.getItem('hci-sim:kbd-id') || '')
const title = ref(localStorage.getItem('hci-sim:title') || '')
const description = ref(localStorage.getItem('hci-sim:description') || '')
const clientId = ref(localStorage.getItem('hci-sim:client-id') || 'hci-sim-admin')
const buildState = ref<BuildState>('idle')
const testState = ref<TestState>('idle')
const progress = ref(0)
const logs = ref<string[]>([])
const expanded = ref<string[]>(['true'])
const capability = ref<Capability | null>(null)
const connection = ref<ConnectionInfo | null>(null)
const environmentContext = ref<Record<string, unknown> | null>(null)
const testRunId = ref('')
const caseId = ref('')
const leaseAvailable = ref(false)
const testDialogVisible = ref(false)
const submitting = ref(false)
const titleInput = ref<{ focus?: () => void } | null>(null)

const validKBD = computed(() => /^\d{1,20}$/.test(kbdId.value.trim()))
const canBuild = computed(() => validKBD.value && buildState.value !== 'building' && (capability.value === null || capability.value.buildable))
// “开始测试”只在环境构建完成后开放。Lease 缺失时也必须让用户得到可见的
// 错误，而不能因为原生 disabled 而出现“点击无反应、Network 没请求”的假象。
const canStart = computed(() => buildState.value === 'succeeded' && !!connection.value && !!environmentContext.value)
const bridgeUrl = computed(() => {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${window.location.host}/terminal-bridge`
})

watch([kbdId, title, description, clientId], () => {
  localStorage.setItem('hci-sim:kbd-id', kbdId.value)
  localStorage.setItem('hci-sim:title', title.value)
  localStorage.setItem('hci-sim:description', description.value)
  localStorage.setItem('hci-sim:client-id', clientId.value)
})

watch(kbdId, (value, previous) => {
  if (previous !== undefined && value.trim() !== previous.trim()) {
    capability.value = null
    buildState.value = 'idle'
    progress.value = 0
    connection.value = null
    environmentContext.value = null
    testRunId.value = ''
    caseId.value = ''
    leaseAvailable.value = false
    testState.value = 'idle'
  }
})

function responseDetail(body: unknown, fallback: string): string {
  if (body && typeof body === 'object') {
    const value = body as Record<string, unknown>
    const detail = value.detail
    if (typeof detail === 'string' && detail.trim()) return detail
    if (detail && typeof detail === 'object') return JSON.stringify(detail)
    const gap = value.capability_gap
    if (Array.isArray(gap) && gap.length) return `capability_gap: ${gap.join(', ')}`
  }
  return fallback
}

async function readResponse(response: Response): Promise<Record<string, unknown>> {
  const body = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(responseDetail(body, `控制面 HTTP ${response.status}`))
  return body && typeof body === 'object' ? body as Record<string, unknown> : {}
}

async function loadCapability(): Promise<Capability | null> {
  if (!validKBD.value) {
    capability.value = null
    return null
  }
  const response = await fetch(`${endpoint}/v1/simulations/capabilities/${encodeURIComponent(kbdId.value.trim())}`)
  const body = await readResponse(response)
  capability.value = body as unknown as Capability
  if (!capability.value.buildable) {
    logs.value.push(`能力预检阻断：${(capability.value.capability_gap || []).join(', ') || 'Runtime 不可构建'}`)
  }
  return capability.value
}

async function buildEnvironment() {
  if (!validKBD.value) return ElMessage.error('请输入有效 KBD_ID')
  buildState.value = 'building'
  testState.value = 'idle'
  progress.value = 10
  logs.value = [`开始预检 KBD ${kbdId.value.trim()}`]
  connection.value = null
  leaseAvailable.value = false
  environmentContext.value = null
  try {
    const checked = await loadCapability()
    if (!checked?.buildable) throw new Error(responseDetail(checked, '当前 KBD 没有可构建的不可变 Bundle'))
    logs.value.push(`Bundle ${checked.bundle_digest}（${checked.authority_scope}）通过能力预检`)
    const response = await fetch(`${endpoint}/v1/simulations/build`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': `admin-build-${kbdId.value.trim()}-${Date.now()}` },
      body: JSON.stringify({ kbd_id: kbdId.value.trim() }),
    })
    const result = await readResponse(response)
    progress.value = 100
    buildState.value = 'succeeded'
    connection.value = (result.connection || {}) as ConnectionInfo
    leaseAvailable.value = Boolean(connection.value.password)
    environmentContext.value = (result.environment_context || {}) as Record<string, unknown>
    testRunId.value = String(result.test_run_id || '')
    logs.value.push('Lease 已签发，环境上下文已冻结；请点击“开始测试”填写工单信息')
    expanded.value = ['true']
    ElMessage.success('仿真环境构建成功')
  } catch (error) {
    buildState.value = 'failed'
    progress.value = 0
    logs.value.push(error instanceof Error ? error.message : String(error))
    ElMessage.error('环境构建失败；可直接重试，表单内容会保留')
  }
}

function beginTest() {
  if (buildState.value !== 'succeeded') {
    ElMessage.warning('请先完成环境构建')
    return
  }
  if (!connection.value || !environmentContext.value) {
    ElMessage.error('环境上下文缺失，请重新构建')
    return
  }
  if (!leaseAvailable.value || !connection.value.password) {
    ElMessage.error('仿真 Lease 已失效，请重新构建环境后再开始测试')
    return
  }
  testDialogVisible.value = true
  nextTick(() => titleInput.value?.focus())
}

function closeSocket(socket: WebSocket) {
  if (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING) socket.close()
}

async function runBridgeSmoke(info: ConnectionInfo, currentCaseId: string, currentTestRunId: string) {
  const commands = [
    `acli --formatter json task get -k '启动虚拟机' -s failed -l 1`,
    'acli system lsof',
    'acli system ps -p 9527 -o cmd=',
  ]
  const results: string[] = []
  const socket = new WebSocket(bridgeUrl.value)
  testState.value = 'connecting'
  await new Promise<void>((resolve, reject) => {
    let index = 0
    let settled = false
    let timer: number | undefined
    const finish = (error?: Error) => {
      if (settled) return
      settled = true
      if (timer) window.clearTimeout(timer)
      closeSocket(socket)
      error ? reject(error) : resolve()
    }
    const armTimeout = () => {
      if (timer) window.clearTimeout(timer)
      timer = window.setTimeout(() => finish(new Error('terminal_bridge 执行超时')), 150000)
    }
    const sendCommand = () => {
      if (index >= commands.length) {
        socket.send(JSON.stringify({ type: 'ssh_disconnect', case_id: currentCaseId }))
        finish()
        return
      }
      testState.value = 'running'
      const execId = `kbd27123-${Date.now()}-${index}`
      socket.send(JSON.stringify({
        type: 'ssh_exec_process', case_id: currentCaseId, exec_id: execId, command: commands[index],
        container: 'host', test_run_id: currentTestRunId, execution_mode: 'sim-ssh', timeout: 120,
      }))
      armTimeout()
    }
    socket.onerror = () => finish(new Error('terminal_bridge WebSocket 连接失败'))
    socket.onclose = () => {
      if (!settled) finish(new Error('terminal_bridge WebSocket 意外断开'))
    }
    socket.onopen = () => {
      const host = String(info.host || '')
      const port = Number(info.port || 2222)
      socket.send(JSON.stringify({
        type: 'ssh_connect', case_id: currentCaseId, host, port,
        username: String(info.username || 'sim'), auth_type: 'lease', password: String(info.password || ''),
        execution_mode: 'sim-ssh', test_run_id: currentTestRunId,
      }))
      armTimeout()
    }
    socket.onmessage = (event) => {
      let message: Record<string, unknown>
      try { message = JSON.parse(String(event.data || '')) } catch { return }
      if (message.type === 'ssh_error') {
        finish(new Error(String(message.detail || message.message || 'SSH 握手失败')))
        return
      }
      if (message.type === 'ssh_connected') {
        logs.value.push('terminal_bridge 已连接 hci-sim SSH 2222')
        sendCommand()
        return
      }
      if (message.type === 'exec_result' && String(message.case_id || '') === currentCaseId) {
        if (timer) window.clearTimeout(timer)
        const exitCode = message.exit_code
        if (typeof exitCode !== 'number' || !Number.isInteger(exitCode)) {
          finish(new Error('terminal_bridge exec_result 缺少有效的整数 exit_code'))
          return
        }
        const command = commands[index]
        if (exitCode !== 0) {
          finish(new Error(`${command} 执行失败（exit=${exitCode}）：${String(message.stderr || message.output || '')}`))
          return
        }
        results.push(`${command} => exit=0`)
        logs.value.push(`关键信号 ${index + 1}/3 已通过`)
        index += 1
        sendCommand()
      }
    }
  })
  return results
}

async function digest(value: string): Promise<string> {
  const bytes = new TextEncoder().encode(value)
  const hash = await crypto.subtle.digest('SHA-256', bytes)
  return `sha256:${Array.from(new Uint8Array(hash)).map((item) => item.toString(16).padStart(2, '0')).join('')}`
}

async function createTestRun() {
  if (!canStart.value) return
  if (!title.value.trim() || !description.value.trim()) return ElMessage.warning('请填写标题和描述')
  submitting.value = true
  testState.value = 'connecting'
  try {
    const response = await fetch(`${endpoint}/v1/simulations/test-runs`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': `admin-test-${testRunId.value}` },
      body: JSON.stringify({
        client_id: clientId.value.trim() || 'hci-sim-admin', kbd_id: kbdId.value.trim(),
        title: title.value.trim(), description: description.value.trim(),
        case_id: caseId.value || undefined, connection: connection.value,
        environment_context: environmentContext.value,
      }),
    })
    const result = await readResponse(response)
    caseId.value = String(result.case_id || '')
    testRunId.value = String(result.test_run_id || testRunId.value)
    const smokeResults = await runBridgeSmoke(connection.value || {}, caseId.value, testRunId.value)
    const report = smokeResults.join('\n')
    const resultResponse = await fetch(`${endpoint}/v1/simulations/test-runs/${encodeURIComponent(testRunId.value)}/result`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': `admin-result-${testRunId.value}` },
      body: JSON.stringify({ attempt_no: 1, oracle_version: 'admin-kbd27123-basic-v1', outcome: 'passed', report_uri: `object://hci-sim/${testRunId.value}/kbd27123-basic`, report_digest: await digest(report) }),
    })
    await readResponse(resultResponse)
    testState.value = 'passed'
    testDialogVisible.value = false
    logs.value.push(`真实工单 ${caseId.value} 创建成功；27123 三个关键信号和 Run Result 已通过`)
    ElMessage.success(`仿真测试通过，工单 ${caseId.value}`)
  } catch (error) {
    testState.value = 'failed'
    logs.value.push(error instanceof Error ? error.message : String(error))
    ElMessage.error('仿真测试失败；可重新点击开始测试，已填写信息会保留')
  } finally {
    submitting.value = false
    // Lease 仅在本次浏览器内存流程使用，完成或失败后都不再保留密码。
    leaseAvailable.value = false
    if (connection.value) connection.value = { ...connection.value, password: undefined }
  }
}

onMounted(() => { if (kbdId.value) logs.value = ['已恢复上次输入；请点击环境构建'] })
</script>

<template>
  <el-card class="simulation-card">
    <template #header><div class="card-header"><span>仿真测试</span><el-tag type="info">K3s terminal_bridge · hci_sim</el-tag></div></template>
    <el-alert title="开始测试只负责进入工单步骤；提交后才创建真实平台工单、连接受管 Bridge 并执行 27123 smoke。失败重试会保留表单。" type="info" show-icon :closable="false" />
    <el-form label-width="110px" class="sim-form">
      <el-form-item label="KBD_ID"><el-input v-model="kbdId" placeholder="例如 27123" clearable @blur="loadCapability().catch((error) => logs.push(error instanceof Error ? error.message : String(error)))" /></el-form-item>
      <el-form-item v-if="capability" label="能力预检"><el-descriptions :column="2" border size="small"><el-descriptions-item label="主库 revision">{{ capability.requested_revision }}</el-descriptions-item><el-descriptions-item label="Runtime revision">{{ capability.runtime_revision }}</el-descriptions-item><el-descriptions-item label="Bundle">{{ capability.bundle_status }}</el-descriptions-item><el-descriptions-item label="范围">{{ capability.authority_scope }}</el-descriptions-item><el-descriptions-item label="结果"><el-tag :type="capability.buildable ? 'success' : 'danger'">{{ capability.buildable ? '可构建' : '阻断' }}</el-tag></el-descriptions-item><el-descriptions-item v-if="capability.capability_gap?.length" label="差距">{{ capability.capability_gap.join(', ') }}</el-descriptions-item></el-descriptions></el-form-item>
      <el-form-item label="构建状态"><el-progress :percentage="progress" :status="buildState === 'failed' ? 'exception' : buildState === 'succeeded' ? 'success' : undefined" /><el-tag v-if="buildState === 'building'" type="warning">构建中</el-tag><el-tag v-else-if="buildState === 'succeeded'" type="success">已就绪</el-tag><el-tag v-else-if="buildState === 'failed'" type="danger">失败</el-tag></el-form-item>
      <el-form-item><el-button type="primary" :loading="buildState === 'building'" :disabled="!canBuild" @click="buildEnvironment">环境构建</el-button><el-button type="success" :disabled="buildState !== 'succeeded' || submitting" @click="beginTest">开始测试</el-button><el-tag v-if="testState === 'passed'" type="success" class="test-status">全链路通过</el-tag><el-tag v-else-if="testState === 'failed'" type="danger" class="test-status">测试失败，可重试</el-tag></el-form-item>
    </el-form>
    <el-collapse v-if="logs.length" v-model="expanded" class="logs"><el-collapse-item title="构建与测试详细日志" name="true"><pre>{{ logs.join('\n') }}</pre></el-collapse-item></el-collapse>
    <el-dialog v-model="testDialogVisible" title="创建仿真测试工单" width="620px" :close-on-click-modal="false">
      <el-alert title="环境已绑定到本次 TestRun；只需补充标题和描述。提交后会自动连接 SSH 并执行 KBD 27123 三个确定性关键信号。" type="success" show-icon :closable="false" />
      <el-form label-width="90px" class="case-form" @submit.prevent="createTestRun"><el-form-item label="标题"><el-input ref="titleInput" v-model="title" placeholder="例如 KBD 27123 仿真验证" /></el-form-item><el-form-item label="描述"><el-input v-model="description" type="textarea" :rows="4" placeholder="例如验证启动虚拟机失败关键信号链" /></el-form-item><el-form-item label="客户端"><el-input v-model="clientId" /></el-form-item></el-form>
      <template #footer><el-button @click="testDialogVisible = false">取消</el-button><el-button type="primary" :loading="submitting" @click="createTestRun">连接 SSH 并创建工单</el-button></template>
    </el-dialog>
    <el-descriptions v-if="connection" title="仿真 SSH 信息（不会显示 Lease）" :column="2" border size="small" class="connection"><el-descriptions-item label="主机">{{ connection.host }}</el-descriptions-item><el-descriptions-item label="端口">{{ connection.port || 2222 }}</el-descriptions-item><el-descriptions-item label="用户名">{{ connection.username || 'sim' }}</el-descriptions-item><el-descriptions-item label="TestRun">{{ testRunId }}</el-descriptions-item><el-descriptions-item v-if="caseId" label="真实工单">{{ caseId }}</el-descriptions-item></el-descriptions>
  </el-card>
</template>

<style scoped>
.simulation-card { max-width: 960px; margin: 0 auto; }
.card-header { display: flex; justify-content: space-between; align-items: center; font-weight: 600; }
.sim-form { margin-top: 20px; }
.case-form { margin-top: 20px; }
.test-status { margin-left: 10px; }
.connection { margin-top: 20px; }
.logs pre { max-height: 320px; overflow: auto; white-space: pre-wrap; background: #101820; color: #d7f9e9; padding: 12px; border-radius: 4px; }
</style>
