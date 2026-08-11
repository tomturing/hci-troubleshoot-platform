<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import SimulationConversation from '@/components/SimulationConversation.vue'

type BuildState = 'idle' | 'building' | 'succeeded' | 'failed'
type TestState = 'idle' | 'case_binding' | 'session_running' | 'execution_failed' | 'result_submitting' | 'result_pending' | 'passed' | 'inconclusive'

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
const expanded = ref<string[]>([])
const capability = ref<Capability | null>(null)
const connection = ref<ConnectionInfo | null>(null)
const environmentContext = ref<Record<string, unknown> | null>(null)
const testRunId = ref('')
const caseId = ref('')
const leaseAvailable = ref(false)
const testDialogVisible = ref(false)
const conversationActive = ref(false)
const submitting = ref(false)
const titleInput = ref<{ focus?: () => void } | null>(null)
const pendingResultSummary = ref<Record<string, unknown> | null>(null)

const validKBD = computed(() => /^\d{1,20}$/.test(kbdId.value.trim()))
const canBuild = computed(() => validKBD.value && buildState.value !== 'building' && !conversationActive.value && (capability.value === null || capability.value.buildable))
const canStart = computed(() => buildState.value === 'succeeded'
  && !!connection.value
  && !!environmentContext.value
  && leaseAvailable.value
  && !!connection.value?.password
  && testState.value === 'idle')
const currentStep = computed(() => buildState.value !== 'succeeded' ? 0 : testState.value === 'passed' ? 2 : 1)

watch([kbdId, title, description, clientId], () => {
  localStorage.setItem('hci-sim:kbd-id', kbdId.value)
  localStorage.setItem('hci-sim:title', title.value)
  localStorage.setItem('hci-sim:description', description.value)
  localStorage.setItem('hci-sim:client-id', clientId.value)
})

watch(kbdId, (value, previous) => {
  if (previous !== undefined && value.trim() !== previous.trim()) resetEnvironment()
})

function resetEnvironment() {
  capability.value = null
  buildState.value = 'idle'
  progress.value = 0
  connection.value = null
  environmentContext.value = null
  testRunId.value = ''
  caseId.value = ''
  leaseAvailable.value = false
  testState.value = 'idle'
  conversationActive.value = false
  pendingResultSummary.value = null
}

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
  if (!capability.value.buildable) logs.value.push(`能力预检阻断：${(capability.value.capability_gap || []).join(', ') || 'Runtime 不可构建'}`)
  return capability.value
}

async function buildEnvironment() {
  if (!validKBD.value) return ElMessage.error('请输入有效 KBD_ID')
  buildState.value = 'building'
  testState.value = 'idle'
  conversationActive.value = false
  pendingResultSummary.value = null
  progress.value = 10
  logs.value = [`开始预检 KBD ${kbdId.value.trim()}`]
  connection.value = null
  leaseAvailable.value = false
  environmentContext.value = null
  caseId.value = ''
  try {
    const checked = await loadCapability()
    if (!checked?.buildable) throw new Error(responseDetail(checked, '当前 KBD 没有可构建的不可变 Bundle'))
    logs.value.push(`Bundle ${checked.bundle_digest}（${checked.authority_scope}）通过能力预检`)
    progress.value = 40
    const response = await fetch(`${endpoint}/v1/simulations/build`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': `admin-build-${kbdId.value.trim()}-${Date.now()}` },
      body: JSON.stringify({ kbd_id: kbdId.value.trim() }),
    })
    const result = await readResponse(response)
    connection.value = (result.connection || {}) as ConnectionInfo
    environmentContext.value = (result.environment_context || {}) as Record<string, unknown>
    testRunId.value = String(result.test_run_id || '')
    leaseAvailable.value = Boolean(connection.value.password)
    if (!testRunId.value || !environmentContext.value || !leaseAvailable.value) throw new Error('构建响应缺少 TestRun、环境上下文或一次性 Lease')
    progress.value = 100
    buildState.value = 'succeeded'
    logs.value.push('Runtime、不可变 Bundle 和一次性 Lease 均已就绪')
    ElMessage.success('仿真环境构建成功，现在可以进入阶段二')
  } catch (error) {
    buildState.value = 'failed'
    progress.value = 0
    logs.value.push(error instanceof Error ? error.message : String(error))
    expanded.value = ['logs']
    ElMessage.error('环境构建失败；展开日志可查看完整原因')
  }
}

function beginTest() {
  if (buildState.value !== 'succeeded') return ElMessage.warning('请先完成环境构建')
  if (!connection.value || !environmentContext.value) return ElMessage.error('环境上下文缺失，请重新构建')
  if (!leaseAvailable.value || !connection.value.password) return ElMessage.error('仿真 Lease 已失效，请重新构建环境')
  if (testState.value !== 'idle') return ElMessage.warning('当前 TestRun 已进入测试，不能重复创建工单')
  testDialogVisible.value = true
  nextTick(() => titleInput.value?.focus())
}

async function createTestSession() {
  // 处理函数必须 fail-closed，不能只依赖按钮 disabled；避免脚本调用或竞态发送空 Lease。
  if (!canStart.value || !connection.value?.password || !environmentContext.value) {
    ElMessage.error('仿真环境或 Lease 已失效，请重新构建后再开始测试')
    return
  }
  if (!title.value.trim() || !description.value.trim()) return ElMessage.warning('请填写标题和描述')
  submitting.value = true
  testState.value = 'case_binding'
  try {
    const response = await fetch(`${endpoint}/v1/simulations/test-runs`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': `admin-test-${testRunId.value}` },
      body: JSON.stringify({
        client_id: clientId.value.trim() || 'hci-sim-admin',
        kbd_id: kbdId.value.trim(),
        title: title.value.trim(),
        description: description.value.trim(),
        connection: connection.value,
        environment_context: environmentContext.value,
      }),
    })
    const result = await readResponse(response)
    caseId.value = String(result.case_id || '')
    testRunId.value = String(result.test_run_id || testRunId.value)
    if (!caseId.value || !testRunId.value) throw new Error('TestRun 绑定响应缺少 case_id 或 test_run_id')
    testDialogVisible.value = false
    conversationActive.value = true
    testState.value = 'session_running'
    logs.value.push(`TestRun 已显式绑定真实工单 ${caseId.value}，正在进入 Agent 会话`)
  } catch (error) {
    testState.value = 'idle'
    const message = error instanceof Error ? error.message : String(error)
    logs.value.push(message)
    ElMessage.error(`创建仿真测试工单失败：${message}`)
  } finally {
    submitting.value = false
  }
}

function consumeLease() {
  // Bridge 完成 SSH 握手后立即从父组件内存中删除 Lease；之后任何 Result 重试均不依赖它。
  leaseAvailable.value = false
  if (connection.value) connection.value = { ...connection.value, password: undefined }
  logs.value.push('受管 terminal_bridge 已完成 SSH 握手，一次性 Lease 已从浏览器内存清除')
}

async function submitResult(summary?: Record<string, unknown>) {
  if (summary) pendingResultSummary.value = summary
  if (!pendingResultSummary.value || !testRunId.value) return
  testState.value = 'result_submitting'
  try {
    const outcome = String(pendingResultSummary.value.outcome || '')
    if (!['passed', 'failed', 'inconclusive'].includes(outcome)) throw new Error('Agent 结果缺少有效 outcome')
    const response = await fetch(`${endpoint}/v1/simulations/test-runs/${encodeURIComponent(testRunId.value)}/result`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': `admin-result-${testRunId.value}` },
      body: JSON.stringify({
        attempt_no: 1,
        oracle_version: 'admin-agent-session-v1',
        outcome,
        report_uri: `object://hci-sim/${testRunId.value}/agent-session`,
        report_summary: pendingResultSummary.value,
      }),
    })
    await readResponse(response)
    testState.value = outcome === 'passed' ? 'passed' : outcome === 'inconclusive' ? 'inconclusive' : 'execution_failed'
    pendingResultSummary.value = null
    logs.value.push(`Agent 会话与 Run Result 已闭环（${outcome}），工单 ${caseId.value}`)
    if (outcome === 'passed') ElMessage.success(`仿真测试已通过，工单 ${caseId.value}`)
    else if (outcome === 'inconclusive') ElMessage.warning(`仿真测试证据不足，工单 ${caseId.value}`)
    else ElMessage.error(`仿真测试失败，工单 ${caseId.value}`)
  } catch (error) {
    testState.value = 'result_pending'
    const message = error instanceof Error ? error.message : String(error)
    logs.value.push(`Agent 执行已完成，但 Result 提交失败：${message}`)
    ElMessage.error('Agent 执行已完成，仅 Result 待提交；重试不会重新 SSH 或执行命令')
  }
}

function handleConversationFailure(message: string) {
  if (testState.value === 'result_submitting' || testState.value === 'passed' || testState.value === 'result_pending') return
  testState.value = 'execution_failed'
  logs.value.push(`Agent 会话失败：${message}`)
}

onMounted(() => { if (kbdId.value) logs.value = ['已恢复上次输入；Lease 不会跨页面恢复，请重新执行环境构建'] })
</script>

<template>
  <main class="simulation-page">
    <header class="page-intro">
      <div><h2>仿真测试</h2><p>先构建隔离环境，再进入与 Custom UI 一致的 Agent 诊断会话。</p></div>
      <el-tag type="info" size="large">Admin UI → K3s terminal_bridge → hci-sim</el-tag>
    </header>

    <el-steps :active="currentStep" finish-status="success" align-center class="stage-steps">
      <el-step title="环境构建" description="能力预检、Bundle、Runtime 与 Lease" />
      <el-step title="开始测试" description="工单绑定、自动 SSH 与 Agent 会话" />
    </el-steps>

    <section class="stage-card" :class="{ active: buildState !== 'succeeded' }">
      <div class="stage-heading"><span class="stage-number">1</span><div><h3>环境构建</h3><p>输入 KBD_ID，系统完成能力预检并构建一次性仿真环境。</p></div><el-tag v-if="buildState === 'succeeded'" type="success">已完成</el-tag></div>
      <el-form label-width="100px" class="build-form">
        <el-form-item label="KBD_ID"><el-input v-model="kbdId" placeholder="例如 27123" clearable :disabled="conversationActive" @blur="loadCapability().catch((error) => logs.push(error instanceof Error ? error.message : String(error)))" /></el-form-item>
        <el-form-item v-if="capability" label="能力预检">
          <el-descriptions :column="3" border size="small" class="capability">
            <el-descriptions-item label="主库 revision">{{ capability.requested_revision }}</el-descriptions-item><el-descriptions-item label="Runtime revision">{{ capability.runtime_revision }}</el-descriptions-item><el-descriptions-item label="Bundle">{{ capability.bundle_status }}</el-descriptions-item>
            <el-descriptions-item label="权威范围">{{ capability.authority_scope }}</el-descriptions-item><el-descriptions-item label="构建门禁"><el-tag :type="capability.buildable ? 'success' : 'danger'">{{ capability.buildable ? '通过' : '阻断' }}</el-tag></el-descriptions-item><el-descriptions-item label="差距">{{ capability.capability_gap?.join(', ') || '无' }}</el-descriptions-item>
          </el-descriptions>
        </el-form-item>
        <el-form-item label="构建进度"><el-progress :percentage="progress" :status="buildState === 'failed' ? 'exception' : buildState === 'succeeded' ? 'success' : undefined" /></el-form-item>
        <el-form-item><el-button type="primary" :loading="buildState === 'building'" :disabled="!canBuild" @click="buildEnvironment">{{ buildState === 'failed' ? '重新构建' : '环境构建' }}</el-button><span class="button-help">构建成功后阶段二自动解锁</span></el-form-item>
      </el-form>
      <el-collapse v-if="logs.length" v-model="expanded" class="logs"><el-collapse-item title="构建与测试详细日志" name="logs"><pre>{{ logs.join('\n') }}</pre></el-collapse-item></el-collapse>
    </section>

    <section class="stage-card test-stage" :class="{ active: buildState === 'succeeded' }">
      <div class="stage-heading"><span class="stage-number">2</span><div><h3>开始测试</h3><p>系统自动绑定工单并连接仿真 SSH；之后的输入、Agent 输出与工具调用均在同一会话中呈现。</p></div><el-tag v-if="testState === 'passed'" type="success">已通过</el-tag><el-tag v-else-if="testState === 'inconclusive'" type="warning">证据不足</el-tag><el-tag v-else-if="testState === 'execution_failed'" type="danger">测试失败</el-tag><el-tag v-else-if="testState === 'result_pending'" type="warning">结果待提交</el-tag></div>

      <div v-if="!conversationActive" class="test-entry">
        <el-empty v-if="buildState !== 'succeeded'" description="完成阶段一后即可开始测试" :image-size="80" />
        <template v-else>
          <el-alert title="环境已就绪。点击开始测试后只需填写标题和描述，系统会自动创建工单、连接 SSH 并进入 Agent 会话。" type="success" show-icon :closable="false" />
          <el-button type="success" size="large" :disabled="!canStart" @click="beginTest">开始测试</el-button>
        </template>
      </div>

      <SimulationConversation
        v-else-if="connection && caseId"
        :case-id="caseId"
        :test-run-id="testRunId"
        :client-id="clientId"
        :initial-message="description"
        :connection="connection"
        @lease-consumed="consumeLease"
        @session-ready="submitResult"
        @fatal="handleConversationFailure"
      />

      <el-alert v-if="testState === 'result_pending' || testState === 'result_submitting'" class="result-retry" type="warning" show-icon :closable="false" title="Agent 执行已经完成，仅 TestRun Result 尚未提交。">
        <template #default><el-button type="warning" :loading="testState === 'result_submitting'" @click="submitResult()">重试提交结果</el-button><span>不会重新创建工单、连接 SSH 或执行命令。</span></template>
      </el-alert>
    </section>

    <el-dialog v-model="testDialogVisible" title="开始仿真测试" width="620px" :close-on-click-modal="false">
      <el-alert title="仿真环境信息将自动填写并绑定；你只需补充与 Custom UI 创建工单相同的标题和问题描述。" type="info" show-icon :closable="false" />
      <el-form label-width="80px" class="case-form" @submit.prevent="createTestSession">
        <el-form-item label="标题"><el-input ref="titleInput" v-model="title" placeholder="例如 KBD 27123 启动虚拟机失败验证" /></el-form-item>
        <el-form-item label="描述"><el-input v-model="description" type="textarea" :rows="5" placeholder="描述希望 Agent 诊断的问题；该内容会作为会话的第一条用户消息" /></el-form-item>
      </el-form>
      <template #footer><el-button @click="testDialogVisible = false">取消</el-button><el-button type="primary" :loading="submitting" @click="createTestSession">创建工单并进入测试</el-button></template>
    </el-dialog>

    <el-collapse v-if="connection" class="connection-details"><el-collapse-item title="运行上下文（不显示 Lease）" name="context"><el-descriptions :column="3" border size="small"><el-descriptions-item label="主机">{{ connection.host }}</el-descriptions-item><el-descriptions-item label="端口">{{ connection.port || 2222 }}</el-descriptions-item><el-descriptions-item label="用户名">{{ connection.username || 'sim' }}</el-descriptions-item><el-descriptions-item label="TestRun">{{ testRunId }}</el-descriptions-item><el-descriptions-item label="工单">{{ caseId || '尚未创建' }}</el-descriptions-item><el-descriptions-item label="执行模式">sim-ssh</el-descriptions-item></el-descriptions></el-collapse-item></el-collapse>
  </main>
</template>

<style scoped>
.simulation-page { max-width: 1280px; margin: 0 auto; }.page-intro { display: flex; justify-content: space-between; align-items: center; gap: 20px; margin-bottom: 22px; }.page-intro h2 { margin: 0 0 6px; font-size: 24px; }.page-intro p,.stage-heading p { margin: 0; color: #909399; }
.stage-steps { padding: 22px 4%; margin-bottom: 20px; border-radius: 10px; background: #fff; }.stage-card { margin-bottom: 20px; padding: 22px; border: 1px solid #dcdfe6; border-radius: 10px; background: #fff; opacity: .88; }.stage-card.active { border-color: #409eff; box-shadow: 0 4px 18px rgba(64,158,255,.10); opacity: 1; }
.stage-heading { display: grid; grid-template-columns: auto 1fr auto; align-items: center; gap: 14px; margin-bottom: 20px; }.stage-heading h3 { margin: 0 0 5px; font-size: 18px; }.stage-number { width: 34px; height: 34px; display: grid; place-items: center; border-radius: 50%; background: #409eff; color: #fff; font-weight: 700; }
.build-form { max-width: 1000px; }.capability { width: 100%; }.button-help { margin-left: 12px; color: #909399; font-size: 13px; }.logs { margin-top: 8px; }.logs pre { max-height: 300px; overflow: auto; white-space: pre-wrap; padding: 13px; border-radius: 6px; background: #101820; color: #d7f9e9; }
.test-entry { display: flex; flex-direction: column; align-items: center; gap: 20px; padding: 18px 4% 28px; }.test-entry .el-alert { align-self: stretch; }.case-form { margin-top: 20px; }.result-retry { margin-top: 16px; }.result-retry span { margin-left: 12px; }.connection-details { margin-top: 10px; background: #fff; }
@media (max-width: 760px) { .page-intro { align-items: flex-start; flex-direction: column; }.stage-card { padding: 14px; }.stage-heading { grid-template-columns: auto 1fr; }.stage-heading > .el-tag { grid-column: 2; justify-self: start; } }
</style>
