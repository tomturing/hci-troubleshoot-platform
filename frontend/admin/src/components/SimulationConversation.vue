<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import DOMPurify from 'dompurify'
import { marked } from 'marked'
import { consumeConversationStream, parseConversationEvent } from '@hci/shared'

interface ConnectionInfo {
  host?: string
  port?: string | number
  username?: string
  password?: string
  execution_mode?: string
}

interface ConversationMessage {
  id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  kind?: 'text' | 'thinking' | 'tool' | 'command' | 'interactive'
  detail?: Record<string, unknown>
  status?: 'running' | 'passed' | 'failed' | 'blocked' | 'pending'
}

interface ChoiceOption {
  optionId: string
  name: string
}

const props = defineProps<{
  caseId: string
  testRunId: string
  clientId: string
  initialMessage: string
  connection: ConnectionInfo
}>()

const emit = defineEmits<{
  leaseConsumed: []
  sessionReady: [summary: Record<string, unknown>]
  fatal: [message: string]
}>()

const messages = ref<ConversationMessage[]>([])
const input = ref('')
const streaming = ref(false)
const bridgeState = ref<'connecting' | 'connected' | 'failed' | 'closed'>('connecting')
const conversationId = ref('')
const messageList = ref<HTMLElement | null>(null)
const socket = ref<WebSocket | null>(null)
const commandCount = ref(0)
const failedCommandCount = ref(0)
const agentOutcome = ref<'passed' | 'failed' | 'inconclusive'>('passed')
const completionEmitted = ref(false)
const pendingApproval = ref<{ execId: string; resolve: (approved: boolean) => void } | null>(null)
const execWaiters = new Map<string, { resolve: (value: Record<string, unknown>) => void; reject: (error: Error) => void; timer: number }>()

const ready = computed(() => bridgeState.value === 'connected' && !!conversationId.value)

function render(content: string) {
  return DOMPurify.sanitize(marked.parse(content || '', { async: false }) as string)
}

function addMessage(message: Omit<ConversationMessage, 'id'>) {
  const value = { id: `${message.role}-${Date.now()}-${messages.value.length}`, ...message }
  messages.value.push(value)
  nextTick(() => {
    if (messageList.value) messageList.value.scrollTop = messageList.value.scrollHeight
  })
  return value
}

async function readJson(response: Response): Promise<Record<string, unknown>> {
  const body = await response.json().catch(() => ({}))
  if (!response.ok) {
    const detail = body && typeof body === 'object' ? (body as Record<string, unknown>).detail : ''
    throw new Error(typeof detail === 'string' && detail ? detail : `HTTP ${response.status}`)
  }
  return body && typeof body === 'object' ? body as Record<string, unknown> : {}
}

async function createConversation() {
  const query = new URLSearchParams({ case_id: props.caseId, assistant_type: 'htp-agent' })
  const response = await fetch(`/api/conversations/?${query}`, {
    method: 'POST',
    headers: { 'X-Client-ID': props.clientId },
  })
  const body = await readJson(response)
  conversationId.value = String(body.conversation_id || '')
  if (!conversationId.value) throw new Error('创建会话成功但响应缺少 conversation_id')
}

function rejectExecWaiters(reason: string) {
  for (const waiter of execWaiters.values()) {
    window.clearTimeout(waiter.timer)
    waiter.reject(new Error(reason))
  }
  execWaiters.clear()
}

async function connectBridge() {
  if (!props.connection.password) throw new Error('仿真 Lease 缺失，请重新构建环境')
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const ws = new WebSocket(`${protocol}//${window.location.host}/terminal-bridge`)
  socket.value = ws
  bridgeState.value = 'connecting'
  await new Promise<void>((resolve, reject) => {
    let settled = false
    const timer = window.setTimeout(() => finish(new Error('terminal_bridge SSH 连接超时')), 30000)
    const finish = (error?: Error) => {
      if (settled) return
      settled = true
      window.clearTimeout(timer)
      error ? reject(error) : resolve()
    }
    ws.onerror = () => finish(new Error('terminal_bridge WebSocket 连接失败'))
    ws.onclose = () => {
      bridgeState.value = 'closed'
      rejectExecWaiters('terminal_bridge 已断开')
      if (!settled) finish(new Error('terminal_bridge WebSocket 意外断开'))
    }
    ws.onopen = () => {
      ws.send(JSON.stringify({
        type: 'ssh_connect',
        case_id: props.caseId,
        host: String(props.connection.host || ''),
        port: Number(props.connection.port || 2222),
        username: String(props.connection.username || 'sim'),
        auth_type: 'lease',
        password: props.connection.password,
        execution_mode: 'sim-ssh',
        test_run_id: props.testRunId,
      }))
    }
    ws.onmessage = (event) => {
      let message: Record<string, unknown>
      try { message = JSON.parse(String(event.data || '')) } catch { return }
      if (message.type === 'ssh_error') {
        finish(new Error(String(message.detail || message.message || 'SSH 握手失败')))
        return
      }
      if (message.type === 'ssh_connected') {
        bridgeState.value = 'connected'
        emit('leaseConsumed')
        finish()
        return
      }
      if (message.type === 'exec_result') {
        const execId = String(message.exec_id || '')
        const waiter = execWaiters.get(execId)
        if (!waiter) return
        window.clearTimeout(waiter.timer)
        execWaiters.delete(execId)
        if (typeof message.exit_code !== 'number' || !Number.isInteger(message.exit_code)) {
          waiter.reject(new Error('terminal_bridge exec_result 缺少有效的整数 exit_code'))
        } else {
          waiter.resolve(message)
        }
      }
    }
  })
}

function waitForExec(execId: string, timeoutSeconds: number) {
  return new Promise<Record<string, unknown>>((resolve, reject) => {
    const timer = window.setTimeout(() => {
      execWaiters.delete(execId)
      reject(new Error(`命令执行超时（${timeoutSeconds}s）`))
    }, (timeoutSeconds + 5) * 1000)
    execWaiters.set(execId, { resolve, reject, timer })
  })
}

async function postExecResult(event: Record<string, unknown>, result: Record<string, unknown>) {
  const execId = String(event.execId || event.exec_id || '')
  const exitCode = Number(result.exit_code)
  const response = await fetch(`/api/conversations/${encodeURIComponent(conversationId.value)}/exec-result`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Client-ID': props.clientId },
    body: JSON.stringify({
      exec_id: execId,
      output: String(result.output || result.stdout || ''),
      stdout: String(result.stdout || ''),
      stderr: String(result.stderr || ''),
      exit_code: Number.isInteger(exitCode) ? exitCode : -1,
      trace_id: event.traceId,
      traceparent: event.traceparent,
      timed_out: Boolean(result.timed_out),
      error_type: result.error_type,
    }),
  })
  await readJson(response)
}

async function executeAgentCommand(event: Record<string, unknown>) {
  const execId = String(event.execId || event.exec_id || '')
  const command = String(event.command || '')
  const riskLevel = Number(event.riskLevel ?? event.risk_level ?? 1)
  const timeoutSeconds = Math.min(300, Math.max(1, Number(event.timeout) || 120))
  if (!execId || !command) throw new Error('Agent 命令事件缺少 execId 或 command')

  const card = addMessage({
    role: 'assistant',
    kind: 'command',
    content: command,
    status: riskLevel >= 2 ? 'pending' : 'running',
    detail: { execId, reason: event.reason, riskLevel },
  })
  commandCount.value += 1

  if (riskLevel >= 3) {
    card.status = 'blocked'
    failedCommandCount.value += 1
    agentOutcome.value = 'failed'
    await postExecResult(event, { output: '高危操作已自动阻止', exit_code: -1, error_type: 'policy_blocked' })
    return
  }
  if (riskLevel === 2) {
    const approved = await new Promise<boolean>((resolve) => {
      pendingApproval.value = { execId, resolve }
    })
    pendingApproval.value = null
    if (!approved) {
      card.status = 'blocked'
      failedCommandCount.value += 1
      agentOutcome.value = 'failed'
      await postExecResult(event, { output: '用户拒绝执行', exit_code: -1, error_type: 'user_rejected' })
      return
    }
  }

  if (bridgeState.value !== 'connected' || !socket.value) throw new Error('SSH 未连接，不能执行 Agent 命令')
  card.status = 'running'
  const resultPromise = waitForExec(execId, timeoutSeconds)
  socket.value.send(JSON.stringify({
    type: 'ssh_exec_process',
    case_id: props.caseId,
    exec_id: execId,
    command,
    container: String(event.container || 'host'),
    node_ip: event.nodeIp,
    test_run_id: props.testRunId,
    execution_mode: 'sim-ssh',
    timeout: timeoutSeconds,
    conversation_id: conversationId.value,
    trace_id: event.traceId,
    traceparent: event.traceparent,
    tool_call_id: event.toolCallId,
  }))
  const result = await resultPromise
  const exitCode = Number(result.exit_code)
  card.status = exitCode === 0 ? 'passed' : 'failed'
  card.detail = { ...card.detail, exitCode, output: String(result.output || result.stdout || '') }
  if (exitCode !== 0) failedCommandCount.value += 1
  if (exitCode !== 0) agentOutcome.value = 'failed'
  await postExecResult(event, result)
}

function approveCommand(approved: boolean) {
  pendingApproval.value?.resolve(approved)
}

async function handleStreamEvent(type: string, data: string, assistant: ConversationMessage) {
  const event = parseConversationEvent(data)
  if (type === 'message') {
    assistant.content += String(event.content || '')
    return
  }
  if (type === 'error') throw new Error(String(event.message || 'Agent 返回错误'))
  if (type === 'metadata') {
    if (event.kind === 'choice_options' && Array.isArray(event.options)) {
      addMessage({
        role: 'assistant',
        kind: 'interactive',
        content: '请选择最符合当前现象的故障分类',
        detail: event,
        status: 'pending',
      })
    }
    return
  }
  if (type === 'agent_exec_command') {
    await executeAgentCommand(event)
    return
  }
  if (type === 'thinking' || type === 'stage_change') {
    addMessage({
      role: 'assistant',
      kind: 'thinking',
      content: String(event.message || event.label || `${event.from || ''} → ${event.to || ''}`),
      detail: event,
    })
    return
  }
  if (type === 'tool_call' || type === 'tool_result' || type === 'tool_executing') {
    addMessage({
      role: 'assistant',
      kind: 'tool',
      content: String(event.tool || event.tool_name || event.name || 'Agent 工具调用'),
      status: type === 'tool_result' ? (event.error ? 'failed' : 'passed') : 'running',
      detail: event,
    })
    return
  }
  if (type === 'interactive_request') {
    if (event.kind === 'human_escalation' && agentOutcome.value !== 'failed') {
      agentOutcome.value = 'inconclusive'
    }
    addMessage({
      role: 'assistant',
      kind: 'interactive',
      content: String(event.prompt || event.title || 'Agent 需要补充信息'),
      detail: event,
      status: 'pending',
    })
  }
}

function choiceOptions(message: ConversationMessage): ChoiceOption[] {
  if (!Array.isArray(message.detail?.options)) return []
  return message.detail.options.flatMap((value) => {
    if (!value || typeof value !== 'object') return []
    const option = value as Record<string, unknown>
    const optionId = String(option.optionId || '').trim()
    const name = String(option.name || '').trim()
    return optionId && name ? [{ optionId, name }] : []
  })
}

async function selectIntent(message: ConversationMessage, option: ChoiceOption) {
  if (message.status !== 'pending' || streaming.value) return
  const categoryMatch = option.name.match(/^([\u4e00-\u9fa5A-Za-z0-9-]+-\d+)\s+(.+)$/)
  const isNone = option.optionId === '__none__'
  const categoryCode = !isNone && !/^\d+$/.test(option.optionId) ? option.optionId : categoryMatch?.[1]
  message.status = 'passed'
  message.detail = { ...message.detail, selectedOptionId: option.optionId }
  await sendMessage(option.name, {
    kind: 'intent_selection_response',
    selectedOptionId: option.optionId,
    selectedCategoryCode: categoryCode,
    selectedCategoryName: categoryMatch?.[2],
    isNoneOfAbove: isNone,
    sourceRequestId: message.detail?.requestId,
  })
}

async function sendMessage(content?: string, metadata: Record<string, unknown> = {}) {
  const text = (content ?? input.value).trim()
  if (!text || !ready.value || streaming.value) return
  input.value = ''
  addMessage({ role: 'user', kind: 'text', content: text })
  const assistant = addMessage({ role: 'assistant', kind: 'text', content: '', status: 'running' })
  streaming.value = true
  try {
    const response = await fetch(`/api/conversations/${encodeURIComponent(conversationId.value)}/message`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Client-ID': props.clientId },
      body: JSON.stringify({
        case_id: props.caseId,
        role: 'user',
        content: text,
        assistant_type: 'htp-agent',
        metadata: { ...metadata, execution_mode: 'sim-ssh', test_run_id: props.testRunId },
      }),
    })
    await consumeConversationStream(response, (event) => handleStreamEvent(event.type, event.data, assistant))
    assistant.status = 'passed'
    // 纯文本首轮（例如 S0 分类追问）不构成仿真执行证据。至少收到并完成一个
    // Agent 命令后才能关闭 TestRun，避免把“Agent 回复了一句话”误报为测试通过。
    if (!completionEmitted.value && (commandCount.value > 0 || agentOutcome.value === 'inconclusive')) {
      completionEmitted.value = true
      emit('sessionReady', {
        case_id: props.caseId,
        conversation_id: conversationId.value,
        execution_mode: 'sim-ssh',
        command_count: commandCount.value,
        failed_command_count: failedCommandCount.value,
        agent_stream_completed: true,
        outcome: agentOutcome.value,
      })
    }
  } catch (error) {
    agentOutcome.value = 'failed'
    assistant.status = 'failed'
    assistant.content ||= error instanceof Error ? error.message : String(error)
    emit('fatal', assistant.content)
  } finally {
    streaming.value = false
  }
}

async function initialize() {
  try {
    addMessage({ role: 'system', content: '正在自动创建 Agent 会话并连接受管仿真环境…' })
    await Promise.all([createConversation(), connectBridge()])
    addMessage({ role: 'system', content: `工单 ${props.caseId} 已连接仿真环境，可以像 Custom UI 一样与 Agent 交互。` })
    await sendMessage(props.initialMessage)
  } catch (error) {
    bridgeState.value = 'failed'
    const message = error instanceof Error ? error.message : String(error)
    addMessage({ role: 'system', content: `测试初始化失败：${message}`, status: 'failed' })
    emit('fatal', message)
  }
}

onMounted(initialize)
onBeforeUnmount(() => {
  pendingApproval.value?.resolve(false)
  rejectExecWaiters('页面已离开')
  if (socket.value?.readyState === WebSocket.OPEN) {
    socket.value.send(JSON.stringify({ type: 'ssh_disconnect', case_id: props.caseId }))
  }
  socket.value?.close()
})
</script>

<template>
  <section class="conversation-shell">
    <header class="conversation-header">
      <div><strong>Agent 仿真会话</strong><span>工单 {{ caseId }}</span></div>
      <div class="session-tags">
        <el-tag :type="bridgeState === 'connected' ? 'success' : bridgeState === 'failed' ? 'danger' : 'warning'">
          SSH {{ bridgeState === 'connected' ? '已连接' : bridgeState === 'connecting' ? '连接中' : '已断开' }}
        </el-tag>
        <el-tag v-if="conversationId" type="info">会话 {{ conversationId.slice(0, 8) }}</el-tag>
      </div>
    </header>

    <div ref="messageList" class="message-list">
      <article v-for="message in messages" :key="message.id" class="message-row" :class="`is-${message.role}`">
        <div class="avatar">{{ message.role === 'user' ? '我' : message.role === 'assistant' ? 'AI' : '系' }}</div>
        <div class="message-body" :class="[`kind-${message.kind || 'text'}`, `status-${message.status || ''}`]">
          <div v-if="message.kind === 'command'" class="command-card">
            <div class="card-caption">Agent 请求执行命令 · 风险 {{ message.detail?.riskLevel }}</div>
            <pre>{{ message.content }}</pre>
            <div v-if="message.status === 'pending' && pendingApproval?.execId === message.detail?.execId" class="approval">
              <el-button size="small" @click="approveCommand(false)">拒绝</el-button>
              <el-button type="primary" size="small" @click="approveCommand(true)">允许执行</el-button>
            </div>
            <div v-if="message.detail?.output" class="command-output">{{ message.detail.output }}</div>
          </div>
          <div v-else-if="message.kind === 'interactive'" class="interactive-card">
            <strong>{{ message.content }}</strong>
            <div v-if="choiceOptions(message).length" class="choice-options">
              <el-button
                v-for="option in choiceOptions(message)"
                :key="option.optionId"
                :type="message.detail?.selectedOptionId === option.optionId ? 'primary' : 'default'"
                :disabled="message.status !== 'pending' || streaming"
                @click="selectIntent(message, option)"
              >{{ option.name }}</el-button>
            </div>
          </div>
          <div v-else-if="message.kind === 'tool'" class="event-card">
            <strong>{{ message.content }}</strong><el-tag size="small" :type="message.status === 'failed' ? 'danger' : message.status === 'passed' ? 'success' : 'info'">{{ message.status }}</el-tag>
          </div>
          <div v-else-if="message.kind === 'thinking'" class="thinking-card">{{ message.content }}</div>
          <div v-else class="markdown" v-html="render(message.content || (message.status === 'running' ? '正在思考…' : ''))" />
        </div>
      </article>
    </div>

    <footer class="composer">
      <el-input v-model="input" type="textarea" :rows="3" resize="none" placeholder="继续向 Agent 提问；Enter 发送，Shift+Enter 换行" :disabled="!ready || streaming" @keydown.enter.exact.prevent="sendMessage()" />
      <el-button type="primary" :loading="streaming" :disabled="!input.trim() || !ready || streaming" @click="sendMessage()">发送</el-button>
    </footer>
  </section>
</template>

<style scoped>
.conversation-shell { display: grid; grid-template-rows: auto minmax(420px, 1fr) auto; min-height: 640px; border: 1px solid #dcdfe6; border-radius: 10px; overflow: hidden; background: #fff; }
.conversation-header { display: flex; justify-content: space-between; align-items: center; padding: 14px 18px; border-bottom: 1px solid #ebeef5; background: #fafcff; }
.conversation-header strong { margin-right: 12px; }.conversation-header span { color: #909399; font-size: 13px; }.session-tags { display: flex; gap: 8px; }
.message-list { overflow: auto; padding: 22px max(20px, 8%); background: #f7f8fa; }
.message-row { display: flex; gap: 10px; margin-bottom: 18px; }.message-row.is-user { flex-direction: row-reverse; }
.avatar { width: 34px; height: 34px; flex: 0 0 34px; border-radius: 50%; display: grid; place-items: center; background: #409eff; color: #fff; font-size: 12px; }.is-user .avatar { background: #67c23a; }.is-system .avatar { background: #909399; }
.message-body { max-width: min(760px, 80%); padding: 11px 14px; border-radius: 4px 14px 14px; background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,.08); line-height: 1.65; overflow: hidden; }.is-user .message-body { background: #ecf5ff; border-radius: 14px 4px 14px 14px; }.is-system .message-body { color: #606266; background: #f4f4f5; }
.markdown :deep(p:last-child) { margin-bottom: 0; }.markdown :deep(pre) { overflow: auto; padding: 10px; border-radius: 6px; background: #18212b; color: #e6edf3; }
.command-card pre { margin: 8px 0; overflow: auto; padding: 10px; border-radius: 6px; background: #18212b; color: #e6edf3; }.card-caption { color: #606266; font-size: 12px; }.approval { margin-top: 10px; }.command-output { max-height: 180px; overflow: auto; white-space: pre-wrap; padding: 8px; background: #f5f7fa; font-family: monospace; font-size: 12px; }
.event-card { display: flex; gap: 12px; align-items: center; }.thinking-card { color: #606266; font-size: 13px; }.status-failed { border-left: 3px solid #f56c6c; }.status-passed.kind-command { border-left: 3px solid #67c23a; }
.interactive-card { display: grid; gap: 10px; padding: 12px; border: 1px solid #c6e2ff; border-radius: 6px; background: #ecf5ff; }
.choice-options { display: flex; flex-wrap: wrap; gap: 8px; }
.choice-options .el-button { height: auto; min-height: 34px; margin-left: 0; white-space: normal; text-align: left; }
.composer { display: grid; grid-template-columns: 1fr auto; align-items: end; gap: 12px; padding: 14px 18px; border-top: 1px solid #ebeef5; background: #fff; }
@media (max-width: 760px) { .conversation-header { align-items: flex-start; gap: 8px; flex-direction: column; }.message-list { padding: 16px 10px; }.message-body { max-width: 88%; }.composer { grid-template-columns: 1fr; } }
</style>
