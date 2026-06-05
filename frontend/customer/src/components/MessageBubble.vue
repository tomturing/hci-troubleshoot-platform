<script setup lang="ts">
import { computed, ref, watch, onBeforeUnmount } from 'vue'
import type { ChatMessage } from '@/stores/chat'
import { useChatStore } from '@/stores/chat'
import { renderMarkdown, isCommandLanguage } from '@/utils/markdown'
import { inferRiskLevel } from '@/utils/commandRisk'
import CommandBlock from './CommandBlock.vue'
import InteractiveOptions from './InteractiveOptions.vue'

const props = defineProps<{ message: ChatMessage }>()

const chatStore = useChatStore()

const isUser = computed(() => props.message.role === 'user')
const isSystem = computed(() => props.message.role === 'system')
const isAssistant = computed(() => props.message.role === 'assistant')
const isDivider = computed(() => isSystem.value && props.message.content.includes('────'))

/** 已复制状态 */
const copiedMessage = ref(false)
async function copyContent() {
  const text = props.message.content
  const copied = await copyToClipboard(text)
  if (!copied) {
    return
  }
  copiedMessage.value = true
  setTimeout(() => (copiedMessage.value = false), 2000)
}

/** 复制到剪贴板（兼容非安全上下文） */
async function copyToClipboard(text: string): Promise<boolean> {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch (e) {
      console.warn('复制失败:', e)
      return false
    }
  } else {
    // 非安全上下文降级：使用 textarea + execCommand
    const textarea = document.createElement('textarea')
    textarea.value = text
    textarea.style.position = 'fixed'
    textarea.style.opacity = '0'
    document.body.appendChild(textarea)
    textarea.select()
    try {
      document.execCommand('copy')
      return true
    } catch (e) {
      console.warn('复制失败:', e)
      return false
    }
    finally {
      document.body.removeChild(textarea)
    }
  }
}

/** 格式化时间 */
function formatTime(d: Date) {
  return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
}

/**
 * 提取消息中的命令块并拆分内容
 * 返回片段数组，每个片段可以是普通文本或命令块
 */
interface ContentSegment {
  id: string
  type: 'text' | 'command'
  content: string
  language?: string
  commandIndex?: number
  /** 命令风险等级（由 inferRiskLevel 计算） */
  riskLevel?: 'none' | 'readonly' | 'caution' | 'danger'
  /** 是否为消息中第一个命令块 */
  isFirstBlock?: boolean
}

import { marked } from 'marked'

const contentSegments = computed<ContentSegment[]>(() => {
  if (!props.message.content) return []

  // 使用成熟的 Markdown Lexer 提取 AST（完美处理未闭合标签或代码块等情况）
  const tokens = marked.lexer(props.message.content)
  const segments: ContentSegment[] = []
  
  let textBuffer = ''
  let commandIndex = 0

  function flushText() {
    if (textBuffer) {
      segments.push({
        id: `text-${segments.length}`,
        type: 'text',
        content: textBuffer,
      })
      textBuffer = ''
    }
  }

  for (const token of Object.values(tokens)) {
    // 判断是否为支持的命令块（需处理 parser 解析出的未定义语言以及各种边界）
    if (token.type === 'code' && isCommandLanguage(token.lang || '')) {
      // 遇到命令块，先把之前累积的普通文本作为一个 segment 冲刷掉
      flushText()
      
      const commands = splitCommands(token.text)
      for (const cmd of commands) {
        segments.push({
          id: `command-${segments.length}`,
          type: 'command',
          content: cmd,
          language: token.lang,
          commandIndex,
          riskLevel: inferRiskLevel(cmd),
          isFirstBlock: commandIndex === 0,
        })
        commandIndex += 1
      }
    } else {
      // 不是命令块，把 raw 内容累积起来，后面统一交给 renderMarkdown 渲染
      // raw 是最原始未更改的 markdown 源码，从而 100% 保持 markdown 上下文不断裂
      textBuffer += ('raw' in token) ? (token.raw as string) : ''
    }
  }
  
  // 补上剩余未冲刷的文本
  flushText()

  return segments
})

/**
 * 拆分命令块
 * 为避免误拆分多行脚本，默认保持整块发送
 * @param code 原始代码内容
 * @returns 命令数组
 */
function splitCommands(code: string): string[] {
  const normalized = code.trimEnd()
  return normalized ? [normalized] : []
}

/**
 * 生成命令描述
 * 基于命令内容生成简单说明
 * @param command 命令内容
 * @returns 命令描述
 */
function generateDescription(command: string): string {
  const lowerCmd = command.toLowerCase().trim()
  const firstWord = lowerCmd.split(/\s+/)[0]

  // 常见命令描述映射
  const descriptions: Record<string, string> = {
    'ls': '列出目录内容',
    'cat': '查看文件内容',
    'ps': '查看进程状态',
    'top': '实时查看系统进程',
    'df': '查看磁盘空间使用情况',
    'du': '查看目录/文件大小',
    'free': '查看内存使用情况',
    'uptime': '查看系统运行时间',
    'dmesg': '查看内核消息',
    'journalctl': '查看系统日志',
    'systemctl': '管理系统服务',
    'service': '管理系统服务',
    'docker': '管理 Docker 容器/镜像',
    'kubectl': '管理 Kubernetes 资源',
    'ping': '测试网络连通性',
    'curl': '发送 HTTP 请求',
    'wget': '下载文件',
    'ssh': '远程登录',
    'scp': '安全复制文件',
    'tar': '打包/解包文件',
    'grep': '文本搜索',
    'awk': '文本处理',
    'sed': '流编辑器',
    'find': '查找文件',
    'chmod': '修改文件权限',
    'chown': '修改文件所有者',
    'rm': '删除文件/目录',
    'cp': '复制文件/目录',
    'mv': '移动/重命名文件',
    'mkdir': '创建目录',
    'rmdir': '删除空目录',
    'touch': '创建空文件/更新时间戳',
    'head': '查看文件开头',
    'tail': '查看文件末尾',
    'less': '分页查看文件',
    'more': '分页查看文件',
    'netstat': '查看网络连接',
    'ss': '查看 socket 统计',
    'lsof': '列出打开的文件',
    'iostat': '查看 IO 统计',
    'vmstat': '查看虚拟内存统计',
    'mpstat': '查看 CPU 统计',
    'sar': '系统活动报告',
  }

  return descriptions[firstWord] || ''
}

/**
 * 渲染普通文本片段
 * 将 Markdown 转换为 HTML
 * @param content 文本内容
 * @returns 渲染后的 HTML
 */
function renderTextSegment(content: string): string {
  return renderMarkdown(content)
}

// ============================================================
// 选项选择渲染（S0 候选故障 / S6 等需要用户点选的阶段）
// ============================================================

/** 圆圈数字字符集（①-⑨，覆盖常见多候选场景） */
const CIRCLED_DIGITS = ['①', '②', '③', '④', '⑤', '⑥', '⑦', '⑧', '⑨']

interface ChoiceItem {
  label: string   // 圆圈数字，如 "①"
  title: string   // 候选项首行简短标题
}

const choiceOptions = computed<ChoiceItem[]>(() => {
  const metadata = props.message.metadata as any
  if (metadata?.kind === 'choice_options') {
    return (metadata.options || []).map((opt: any) => {
      const id = String(opt.optionId)
      const labelMap: Record<string, string> = {
        '1': '①', '2': '②', '3': '③', '4': '④', '5': '⑤',
        '6': '⑥', '7': '⑦', '8': '⑧', '9': '⑨'
      }
      return {
        label: labelMap[id] || id,
        title: opt.name,
      }
    })
  }
  return []
})

/** 已选选项（防止重复发送） */
const selectedChoice = ref<string | null>(null)

/**
 * 判断该消息的选项是否已经被交互过（页面刷新后仍有效）。
 * 判断逻辑：在 chatStore.messages 中找到当前消息之后是否存在用户的圆圈数字回复消息。
 */
const hasBeenInteracted = computed(() => {
  if (!choiceOptions.value.length) return false
  const msgIndex = chatStore.messages.findIndex(m => m.id === props.message.id)
  if (msgIndex === -1) return false
  const laterMessages = chatStore.messages.slice(msgIndex + 1)
  return laterMessages.some(
    m => m.role === 'user' && CIRCLED_DIGITS.some(d => m.content.startsWith(d))
  )
})

/** 历史交互中已选中的选项左签（用于刷新后继续高亮展示） */
const interactedChoice = computed(() => {
  if (!choiceOptions.value.length) return null
  const msgIndex = chatStore.messages.findIndex(m => m.id === props.message.id)
  if (msgIndex === -1) return null
  const laterMessages = chatStore.messages.slice(msgIndex + 1)
  const found = laterMessages.find(
    m => m.role === 'user' && CIRCLED_DIGITS.some(d => m.content.startsWith(d))
  )
  if (!found) return null
  return CIRCLED_DIGITS.find(d => found.content.startsWith(d)) ?? null
})

/** 判断是否为“以上都不是”类型的选项（点击后需要正文输入） */
function isNoneOfAbove(choice: ChoiceItem): boolean {
  return choice.title.includes('以上都不是') || choice.title.includes('补充症状') || choice.title.includes('补充描述')
}

/** 待输入的选项（以上都不是流程） */
const pendingInputChoice = ref<ChoiceItem | null>(null)
/** 补充信息输入框内容 */
const freeInputText = ref('')

/** 点击选项：将选项标签作为用户消息发送 */
async function handleChoiceSelect(choice: ChoiceItem) {
  if (selectedChoice.value || hasBeenInteracted.value || chatStore.isLoading) return

  // 如果是“以上都不是”类型，展开输入框而不立即发送
  if (isNoneOfAbove(choice)) {
    pendingInputChoice.value = choice
    return
  }

  selectedChoice.value = choice.label
  await chatStore.sendMessage(choice.label)
}

/** 提交“以上都不是”的补充描述 */
async function handleFreeInputSubmit() {
  if (!pendingInputChoice.value || !freeInputText.value.trim()) return
  const choice = pendingInputChoice.value
  const text = freeInputText.value.trim()
  selectedChoice.value = choice.label
  freeInputText.value = ''
  pendingInputChoice.value = null
  await chatStore.sendMessage(`${choice.label} ${text}`)
}

/** 取消补充输入 */
function cancelFreeInput() {
  pendingInputChoice.value = null
  freeInputText.value = ''
}

// ============================================================
// interactive_request 气泡渲染（ops-agent SOP 操作卡 / 信息确认卡）
// ============================================================

/** 提取 interactive_request metadata 中的 event 结构 */
const interactiveEvent = computed(() => {
  if (props.message.metadata?.kind !== 'interactive_request') return null
  return props.message.metadata.event as {
    requestId: string
    acpSessionId: string
    kind: string
    title: string
    prompt: string
    options: Array<{ optionId: string; name: string }>
    customInput: boolean
    metadata: Record<string, unknown>
  } | null
})

/** 当前 interactive 气泡之后是否已有用户响应（防止重复提交） */
const interactiveSubmitted = computed(() => {
  if (!interactiveEvent.value) return false
  const msgIndex = chatStore.messages.findIndex(m => m.id === props.message.id)
  if (msgIndex === -1) return false
  return chatStore.messages.slice(msgIndex + 1).some(
    m => m.role === 'user' && m.metadata?.kind === 'interactive_response'
  )
})

/** 已选中的选项 ID（从响应消息 metadata 读取，支持刷新后恢复高亮） */
const selectedInteractiveOptionId = computed<string | null>(() => {
  if (!interactiveEvent.value) return null
  const msgIndex = chatStore.messages.findIndex(m => m.id === props.message.id)
  if (msgIndex === -1) return null
  const resp = chatStore.messages.slice(msgIndex + 1).find(
    m => m.role === 'user' && m.metadata?.kind === 'interactive_response'
  )
  return (resp?.metadata?.selectedOptionId as string) ?? null
})

const interactiveSubmitting = ref(false)
const interactiveFreeText = ref('')

/** 点击选项提交 */
async function handleInteractiveOption(optionId: string, optionName: string) {
  if (interactiveSubmitting.value || interactiveSubmitted.value) return
  const ev = interactiveEvent.value
  if (!ev) return

  // ── 如果是 triage-agent 意图分类卡片（S0） ─────────────────────────
  if (ev.kind === 'intent_selection') {
    // 映射为圆圈数字：1 -> ①, 2 -> ②, 3 -> ③ 等
    const idx = parseInt(optionId)
    const label = CIRCLED_DIGITS[idx - 1] || optionId
    
    // 直接发送圆圈数字，并带上 interactive_response 类型的 metadata，满足卡片已选高亮逻辑
    await chatStore.sendMessage(label, {
      kind: 'interactive_response',
      selectedOptionId: optionId,
    })
    return
  }

  // ── 以下为 ops-agent 原有逻辑 ──
  interactiveSubmitting.value = true
  try {
    const convId = chatStore.conversationId
    if (!convId) return
    const resp = await fetch(`/api/conversations/${convId}/interactive-response`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        request_id: ev.requestId,
        acp_session_id: ev.acpSessionId,
        outcome: { outcome: 'selected', optionId, optionLabel: optionName },
      }),
    })
    if (resp.ok) {
      // 追加用户响应气泡（selectedOptionId 用于恢复已选高亮）
      chatStore.messages.push({
        id: `ir-resp-${Date.now()}`,
        role: 'user',
        content: `[操作选择] ${optionName}`,
        timestamp: new Date(),
        metadata: { kind: 'interactive_response', selectedOptionId: optionId },
      })
      chatStore.clearInteractiveRequest()
      // 立即接收 ops-agent 对该 interactive_response 的续写内容
      chatStore.resumeOpsAgentStream()
    } else {
      console.warn('[interactive] 提交失败:', resp.status)
    }
  } finally {
    interactiveSubmitting.value = false
  }
}

/** 提交自由文本 */
async function handleInteractiveFreeText() {
  const text = interactiveFreeText.value.trim()
  if (!text || interactiveSubmitting.value || interactiveSubmitted.value) return
  const ev = interactiveEvent.value
  if (!ev) return
  interactiveSubmitting.value = true
  try {
    const convId = chatStore.conversationId
    if (!convId) return
    const resp = await fetch(`/api/conversations/${convId}/interactive-response`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        request_id: ev.requestId,
        acp_session_id: ev.acpSessionId,
        outcome: { outcome: 'free_text', text },
      }),
    })
    if (resp.ok) {
      chatStore.messages.push({
        id: `ir-resp-${Date.now()}`,
        role: 'user',
        content: `[补充输入] ${text}`,
        timestamp: new Date(),
        metadata: { kind: 'interactive_response' },
      })
      interactiveFreeText.value = ''
      chatStore.clearInteractiveRequest()
      // 立即接收 ops-agent 对该 interactive_response 的续写内容
      chatStore.resumeOpsAgentStream()
    } else {
      console.warn('[interactive] 自由文本提交失败:', resp.status)
    }
  } finally {
    interactiveSubmitting.value = false
  }
}

// ============================================================
// T-TOOL-18: exec_confirm 气泡渲染（命令执行确认卡片）
// ============================================================

/** 提取 exec_confirm metadata 中的 event 结构 */
const execConfirmEvent = computed(() => {
  if (props.message.metadata?.kind !== 'exec_confirm') return null
  return props.message.metadata.event as {
    execId: string
    command: string
    reason: string
    riskLevel: 1 | 2 | 3
    nodeIp: string
    caseId: string
    timestamp: number
  } | null
})

/** 当前 exec_confirm 气泡之后是否已有用户响应（防止重复提交） */
const execConfirmSubmitted = computed(() => {
  if (!execConfirmEvent.value) return false
  const msgIndex = chatStore.messages.findIndex(m => m.id === props.message.id)
  if (msgIndex === -1) return false
  return chatStore.messages.slice(msgIndex + 1).some(
    m => m.role === 'user' && (m.metadata?.kind === 'exec_confirmed' || m.metadata?.kind === 'exec_rejected' || m.metadata?.kind === 'exec_timeout')
  )
})

/** 已确认的执行结果（从响应消息 metadata 读取） */
const execConfirmResult = computed<'exec_confirmed' | 'exec_rejected' | 'exec_timeout' | null>(() => {
  if (!execConfirmEvent.value) return null
  const msgIndex = chatStore.messages.findIndex(m => m.id === props.message.id)
  if (msgIndex === -1) return null
  const resp = chatStore.messages.slice(msgIndex + 1).find(
    m => m.role === 'user' && (m.metadata?.kind === 'exec_confirmed' || m.metadata?.kind === 'exec_rejected' || m.metadata?.kind === 'exec_timeout')
  )
  if (!resp) return null
  return (resp.metadata?.kind as 'exec_confirmed' | 'exec_rejected' | 'exec_timeout') ?? null
})

const execConfirmSubmitting = ref(false)
const execConfirmCountdown = ref(600) // 10 分钟 = 600 秒
let execConfirmTimer: ReturnType<typeof setInterval> | null = null

/** 启动倒计时（组件挂载时） */
function startExecConfirmTimer() {
  if (!execConfirmEvent.value || execConfirmSubmitted.value) return

  const eventTimestamp = execConfirmEvent.value.timestamp
  const elapsedSeconds = Math.floor((Date.now() - eventTimestamp) / 1000)
  execConfirmCountdown.value = Math.max(0, 600 - elapsedSeconds)

  // 如果已经超时，自动拒绝
  if (execConfirmCountdown.value <= 0) {
    handleExecConfirmTimeout()
    return
  }

  execConfirmTimer = setInterval(() => {
    execConfirmCountdown.value--
    if (execConfirmCountdown.value <= 0) {
      handleExecConfirmTimeout()
    }
  }, 1000)
}

/** 停止倒计时 */
function stopExecConfirmTimer() {
  if (execConfirmTimer !== null) {
    clearInterval(execConfirmTimer)
    execConfirmTimer = null
  }
}

/** 用户确认执行 */
async function handleExecConfirmApprove() {
  if (execConfirmSubmitting.value || execConfirmSubmitted.value) return
  const ev = execConfirmEvent.value
  if (!ev) return

  stopExecConfirmTimer()
  execConfirmSubmitting.value = true

  try {
    // 追加用户响应气泡
    chatStore.messages.push({
      id: `exec-resp-${Date.now()}`,
      role: 'user',
      content: `[确认执行] \`${ev.command}\``,
      timestamp: new Date(),
      metadata: { kind: 'exec_confirmed', execId: ev.execId, caseId: ev.caseId },
    })
    chatStore.clearExecConfirm()

    // TODO: 通过 terminal_bridge 执行命令（T-TOOL-04）
    // 目前仅记录用户确认，实际执行由 chat.ts 中的 SSE 监听处理

    // 触发 AI 继续处理（模拟 resume）
    chatStore.resumeOpsAgentStream()
  } finally {
    execConfirmSubmitting.value = false
  }
}

/** 用户拒绝执行 */
async function handleExecConfirmReject() {
  if (execConfirmSubmitting.value || execConfirmSubmitted.value) return
  const ev = execConfirmEvent.value
  if (!ev) return

  stopExecConfirmTimer()
  execConfirmSubmitting.value = true

  try {
    const convId = chatStore.conversationId
    if (convId) {
      // POST 执行结果（拒绝）
      await fetch(`/api/conversations/${convId}/exec-result`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          exec_id: ev.execId,
          output: '',
          exit_code: -1,
          status: 'user_rejected',
        }),
      })
    }

    // 追加用户响应气泡
    chatStore.messages.push({
      id: `exec-resp-${Date.now()}`,
      role: 'user',
      content: `[拒绝执行] \`${ev.command}\``,
      timestamp: new Date(),
      metadata: { kind: 'exec_rejected', execId: ev.execId, caseId: ev.caseId },
    })
    chatStore.clearExecConfirm()

    // 触发 AI 继续处理
    chatStore.resumeOpsAgentStream()
  } finally {
    execConfirmSubmitting.value = false
  }
}

/** 超时自动拒绝 */
async function handleExecConfirmTimeout() {
  if (execConfirmSubmitted.value) return
  const ev = execConfirmEvent.value
  if (!ev) return

  stopExecConfirmTimer()
  execConfirmSubmitting.value = true

  try {
    const convId = chatStore.conversationId
    if (convId) {
      // POST 执行结果（超时）
      await fetch(`/api/conversations/${convId}/exec-result`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          exec_id: ev.execId,
          output: '',
          exit_code: -1,
          status: 'timeout',
        }),
      })
    }

    // 追加系统响应气泡
    chatStore.messages.push({
      id: `exec-resp-${Date.now()}`,
      role: 'user',
      content: `[超时自动拒绝] \`${ev.command}\``,
      timestamp: new Date(),
      metadata: { kind: 'exec_timeout', execId: ev.execId, caseId: ev.caseId },
    })
    chatStore.clearExecConfirm()

    // 触发 AI 继续处理
    chatStore.resumeOpsAgentStream()
  } finally {
    execConfirmSubmitting.value = false
  }
}

/** 组件挂载时启动倒计时 */
watch(execConfirmEvent, (ev) => {
  if (ev && !execConfirmSubmitted.value) {
    startExecConfirmTimer()
  } else {
    stopExecConfirmTimer()
  }
}, { immediate: true })

/** 组件卸载时清理定时器 */
onBeforeUnmount(() => {
  stopExecConfirmTimer()
})
</script>

<template>
  <div
    class="message-bubble"
    :class="{
      'is-user': isUser,
      'is-system': isSystem,
      'is-assistant': isAssistant,
      'is-streaming': message.isStreaming,
      'is-divider': isDivider,
    }"
  >
    <!-- 系统消息：居中小字 -->
    <div v-if="isSystem" class="system-msg">
      {{ message.content }}
    </div>

    <!-- 用户/AI消息 -->
    <template v-else>
      <div class="avatar">
        <span v-if="isUser">我</span>
        <span v-else>AI</span>
      </div>
      <div class="bubble-content">
        <div
          class="bubble-body"
          :class="{ 'is-command-available': !message.isStreaming && contentSegments.some(s => s.type === 'command') }"
        >
          <!-- interactive_request 气泡：不渲染普通 content，下方有专用区域 -->
          <template v-if="message.metadata?.kind === 'interactive_request'" />

          <!-- exec_confirm 气泡：不渲染普通 content，下方有专用区域 -->
          <template v-if="message.metadata?.kind === 'exec_confirm'" />

          <!-- 阶段1：Thinking 状态（流式中且内容为空） -->
          <template v-else-if="message.isStreaming && !message.content">
            <div class="thinking-indicator">
              <span class="thinking-dot" />
              <span class="thinking-dot" />
              <span class="thinking-dot" />
              <span class="thinking-label">思考中</span>
            </div>
          </template>

          <!-- 阶段2+3：统一渲染管道。流式与完成态完全共享相同的 DOM 结构与拆分策略 -->
          <template v-else-if="message.content">
            <div
              v-for="segment in contentSegments"
              :key="segment.id"
              class="segment-item"
            >
              <!-- 命令块 -->
              <template v-if="segment.type === 'command'">
                <!-- 流式输出期间，展示带有代码格式的普通语法高亮框（无交互功能占位） -->
                <div v-if="message.isStreaming" class="streaming-command-placeholder stage3-item">
                  <pre><code :class="['language-' + (segment.language || 'bash')]">{{ segment.content }}</code></pre>
                </div>
                <!-- 输出完成后，平滑升级为带有交互能力的 CommandBlock -->
                <CommandBlock
                  v-else
                  class="stage3-item"
                  :command="segment.content"
                  :language="segment.language || 'bash'"
                  :description="generateDescription(segment.content)"
                  :risk-level="segment.riskLevel || 'none'"
                  :index="segment.commandIndex || 0"
                  :is-first-block="segment.isFirstBlock || false"
                  :block-id="segment.id"
                />
              </template>

              <!-- 普通文本使用 Markdown 渲染 -->
              <div
                v-else
                class="text-segment stage3-item"
                v-html="renderTextSegment(segment.content)"
              />
            </div>
          </template>
        </div>

        <!-- interactive_request 气泡（ops-agent SOP 操作卡 / 信息确认卡） -->
        <div v-if="interactiveEvent" class="interactive-bubble">
          <!-- 标题 -->
          <div class="interactive-title">
            {{ interactiveEvent.title || (interactiveEvent.kind === 'sop_step' ? '📋 SOP 操作步骤确认' : '❓ 信息确认') }}
          </div>

          <!-- SOP 卡：展示路径/目标/预期/操作指引 -->
          <template v-if="interactiveEvent.kind === 'sop_step'">
            <div v-if="interactiveEvent.metadata?.route" class="ir-field">
              <span class="ir-label">当前路径</span>
              <code class="ir-route">{{ interactiveEvent.metadata.route }}</code>
            </div>
            <div class="ir-grid">
              <div v-if="interactiveEvent.metadata?.operationGoal" class="ir-field">
                <span class="ir-label">操作目标</span>
                <p>{{ interactiveEvent.metadata.operationGoal }}</p>
              </div>
              <div v-if="interactiveEvent.metadata?.expectedResult" class="ir-field">
                <span class="ir-label">预期结果</span>
                <p>{{ interactiveEvent.metadata.expectedResult }}</p>
              </div>
            </div>
            <div v-if="interactiveEvent.metadata?.executionGuidance" class="ir-field">
              <span class="ir-label">操作指引</span>
              <p>{{ interactiveEvent.metadata.executionGuidance }}</p>
            </div>
            <div v-if="interactiveEvent.metadata?.feedbackRequest || interactiveEvent.prompt" class="ir-field">
              <span class="ir-label">请反馈</span>
              <p>{{ interactiveEvent.metadata?.feedbackRequest || interactiveEvent.prompt }}</p>
            </div>
          </template>

          <!-- 变量输入卡：变量描述 -->
          <template v-else-if="['variable_input', 'variable_confirm'].includes(interactiveEvent.kind)">
            <div class="ir-field">
              <span class="ir-label">变量描述</span>
              <p class="ir-question">{{ interactiveEvent.prompt }}</p>
            </div>
          </template>

          <!-- 信息确认卡：核心问题 + 背景说明 -->
          <template v-else>
            <div class="ir-field">
              <span class="ir-label">核心问题</span>
              <p class="ir-question">{{ (interactiveEvent.metadata?.question as string) || interactiveEvent.prompt }}</p>
            </div>
            <div v-if="interactiveEvent.metadata?.context" class="ir-field">
              <span class="ir-label">背景说明</span>
              <p>{{ interactiveEvent.metadata.context }}</p>
            </div>
          </template>

          <!-- 选项按钮（共用 InteractiveOptions 组件：提交后保留显示，已选蓝色，其余置灰） -->
          <InteractiveOptions
            v-if="interactiveEvent.options?.length"
            :options="interactiveEvent.options"
            :selected-option-id="selectedInteractiveOptionId"
            :force-disabled="interactiveSubmitted"
            :submitting="interactiveSubmitting"
            @select="handleInteractiveOption"
          />

          <!-- 自由文本输入（提交后隐藏） -->
          <div v-if="interactiveEvent.customInput && !interactiveSubmitted" class="ir-free-input">
            <span class="ir-label">
              {{ ['variable_input', 'variable_confirm'].includes(interactiveEvent.kind) ? (interactiveEvent.metadata?.required ? '请输入（必填）' : '请输入（选填）') : '补充信息（可选）' }}
            </span>
            <el-input
              v-model="interactiveFreeText"
              type="textarea"
              :rows="2"
              :placeholder="['variable_input', 'variable_confirm'].includes(interactiveEvent.kind) ? (interactiveEvent.metadata?.validation_pattern ? '格式要求: ' + interactiveEvent.metadata.validation_pattern : '请输入对应的值。') : '输入更准确的现场信息或执行结果。'"
              :disabled="interactiveSubmitting"
            />
            <el-button
              class="mt-2"
              size="small"
              :disabled="!interactiveFreeText.trim() || interactiveSubmitting"
              :loading="interactiveSubmitting"
              @click="handleInteractiveFreeText"
            >
              {{ ['variable_input', 'variable_confirm'].includes(interactiveEvent.kind) ? '提交变量值' : '提交补充信息' }}
            </el-button>
          </div>
        </div>

        <!-- T-TOOL-18: exec_confirm 气泡（命令执行确认卡片） -->
        <div v-if="execConfirmEvent" class="exec-confirm-bubble">
          <!-- 标题 -->
          <div class="exec-confirm-title">
            <span class="exec-confirm-icon">⚠️</span>
            Agent 请求执行以下命令
          </div>

          <!-- 目标节点 -->
          <div v-if="execConfirmEvent.nodeIp" class="exec-confirm-field">
            <span class="exec-confirm-label">目标节点</span>
            <code class="exec-confirm-node">{{ execConfirmEvent.nodeIp }}</code>
          </div>

          <!-- 执行原因 -->
          <div v-if="execConfirmEvent.reason" class="exec-confirm-field">
            <span class="exec-confirm-label">执行原因</span>
            <p>{{ execConfirmEvent.reason }}</p>
          </div>

          <!-- 命令内容（代码块） -->
          <div class="exec-confirm-command-block">
            <pre class="exec-confirm-code"><code>{{ execConfirmEvent.command }}</code></pre>
          </div>

          <!-- 倒计时 -->
          <div class="exec-confirm-countdown">
            <span :class="{ 'countdown-warning': execConfirmCountdown < 60 }">
              {{ execConfirmCountdown }}s 后自动拒绝
            </span>
          </div>

          <!-- 操作按钮 -->
          <div v-if="!execConfirmSubmitted" class="exec-confirm-actions">
            <el-button
              type="danger"
              size="default"
              :disabled="execConfirmSubmitting"
              @click="handleExecConfirmReject"
            >
              拒绝
            </el-button>
            <el-button
              type="success"
              size="default"
              :disabled="execConfirmSubmitting"
              :loading="execConfirmSubmitting"
              @click="handleExecConfirmApprove"
            >
              确认执行
            </el-button>
          </div>

          <!-- 已响应状态提示 -->
          <div v-else class="exec-confirm-result">
            <el-tag v-if="execConfirmResult === 'exec_confirmed'" type="success" size="small">
              已确认
            </el-tag>
            <el-tag v-else-if="execConfirmResult === 'exec_rejected'" type="danger" size="small">
              已拒绝
            </el-tag>
            <el-tag v-else-if="execConfirmResult === 'exec_timeout'" type="warning" size="small">
              已超时
            </el-tag>
          </div>
        </div>

        <!-- 可点击选项区（S0候选故障选择 / S6等交互阶段） -->
        <div v-if="choiceOptions.length > 0" class="choice-selector">
          <div class="choice-hint">
            <template v-if="hasBeenInteracted || selectedChoice">已选择：</template>
            <template v-else>点击选择：</template>
          </div>
          <div class="choice-buttons">
            <el-button
              v-for="choice in choiceOptions"
              :key="choice.label"
              size="small"
              :type="(selectedChoice === choice.label || interactedChoice === choice.label) ? 'primary' : 'default'"
              :disabled="!!selectedChoice || hasBeenInteracted || chatStore.isLoading"
              class="choice-btn"
              :class="{ 'choice-btn--interacted': hasBeenInteracted && interactedChoice !== choice.label }"
              @click="handleChoiceSelect(choice)"
            >
              {{ choice.label }} {{ choice.title }}
            </el-button>
          </div>
          <!-- “以上都不是”补充输入框 -->
          <div v-if="pendingInputChoice" class="free-input-area">
            <div class="free-input-hint">{{ pendingInputChoice.label }} {{ pendingInputChoice.title }}，请补充具体症状：</div>
            <el-input
              v-model="freeInputText"
              type="textarea"
              :autosize="{ minRows: 2, maxRows: 5 }"
              placeholder="请输入具体症状描述..."
              autofocus
            />
            <div class="free-input-actions">
              <el-button size="small" @click="cancelFreeInput">取消</el-button>
              <el-button
                size="small"
                type="primary"
                :disabled="!freeInputText.trim() || chatStore.isLoading"
                @click="handleFreeInputSubmit"
              >提交</el-button>
            </div>
          </div>
        </div>

        <div class="bubble-meta">
          <span class="bubble-time">{{ formatTime(message.timestamp) }}</span>
          <el-button
            v-if="isAssistant && message.content"
            size="small"
            text
            @click="copyContent"
          >
            {{ copiedMessage ? '已复制' : '复制' }}
          </el-button>
        </div>
        <!-- 流式输出光标 -->
        <span v-if="message.isStreaming && message.content" class="streaming-cursor">▊</span>
      </div>
    </template>
  </div>
</template>

<style scoped>
.message-bubble {
  display: flex;
  gap: 10px;
  max-width: 85%;
}

.message-bubble.is-user {
  align-self: flex-end;
  flex-direction: row-reverse;
}

.message-bubble.is-system {
  align-self: center;
  max-width: 100%;
}

.system-msg {
  font-size: 12px;
  color: #909399;
  background: rgba(0, 0, 0, 0.04);
  padding: 6px 16px;
  border-radius: 12px;
  text-align: center;
}

/* 工单分割线样式 */
.message-bubble.is-divider {
  max-width: 100%;
  width: 100%;
}

.message-bubble.is-divider .system-msg {
  background: transparent;
  border-radius: 0;
  padding: 12px 0;
  margin: 4px 0;
  color: #c0c4cc;
  font-size: 12px;
  position: relative;
  display: flex;
  align-items: center;
  gap: 12px;
}

.message-bubble.is-divider .system-msg::before,
.message-bubble.is-divider .system-msg::after {
  content: '';
  flex: 1;
  height: 1px;
  background: #dcdfe6;
}

.avatar {
  width: 36px;
  height: 36px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  font-weight: 600;
  flex-shrink: 0;
}

.is-user .avatar {
  background: #409eff;
  color: #fff;
}

.is-assistant .avatar {
  background: #67c23a;
  color: #fff;
}

.bubble-content {
  background: #fff;
  border-radius: 12px;
  padding: 12px 16px;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.08);
  position: relative;
  line-height: 1.7;
  font-size: 14px;
  overflow: hidden;
  max-width: 100%;
}

.is-user .bubble-content {
  background: #409eff;
  color: #fff;
}

.bubble-meta {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 6px;
  margin-top: 6px;
}

.bubble-time {
  font-size: 11px;
  color: #c0c4cc;
}

.is-user .bubble-time {
  color: rgba(255, 255, 255, 0.6);
}

.streaming-cursor {
  animation: blink 1s infinite;
  color: #409eff;
}

@keyframes blink {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0;
  }
}

/* ========================================
   Thinking 动画
   ======================================== */
.thinking-indicator {
  display: flex;
  align-items: center;
  gap: 5px;
  padding: 4px 2px;
}

.thinking-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #409eff;
  opacity: 0.3;
  animation: thinking-pulse 1.4s ease-in-out infinite;
}

.thinking-dot:nth-child(1) { animation-delay: 0s; }
.thinking-dot:nth-child(2) { animation-delay: 0.22s; }
.thinking-dot:nth-child(3) { animation-delay: 0.44s; }

@keyframes thinking-pulse {
  0%, 80%, 100% {
    opacity: 0.2;
    transform: scale(0.85);
  }
  40% {
    opacity: 1;
    transform: scale(1.15);
  }
}

.thinking-label {
  font-size: 12px;
  color: #909399;
  margin-left: 4px;
  letter-spacing: 0.5px;
}

/* ========================================
   阶段3 升级渲染：每个 segment 用单根 div.segment-item 包裹
   display: contents 让包裹层对布局透明不影响内部元素排列
   动画加在内部实际元素上（避免 display:contents 动画兼容性问题）
   ======================================== */
.segment-item {
  display: contents;
}

/* 动画施加在阶段3的实际内容元素上 */
.stage3-item {
  animation: segment-enter 0.35s ease both;
}

@keyframes segment-enter {
  from {
    opacity: 0;
    transform: translateY(4px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

/* ========================================
   文本片段样式（Markdown 渲染）
   ======================================== */

.bubble-body {
  word-break: break-word;
}

.text-segment {
  word-break: break-word;
}

/* 标题样式 */
.text-segment :deep(h1) {
  font-size: 1.5em;
  font-weight: 600;
  margin: 16px 0 8px 0;
  padding-bottom: 6px;
  border-bottom: 1px solid #ebeef5;
  line-height: 1.4;
}

.text-segment :deep(h2) {
  font-size: 1.3em;
  font-weight: 600;
  margin: 14px 0 6px 0;
  line-height: 1.4;
}

.text-segment :deep(h3) {
  font-size: 1.15em;
  font-weight: 600;
  margin: 12px 0 4px 0;
  line-height: 1.4;
}

.text-segment :deep(h4),
.text-segment :deep(h5),
.text-segment :deep(h6) {
  font-size: 1em;
  font-weight: 600;
  margin: 10px 0 4px 0;
  line-height: 1.4;
}

/* 段落样式 */
.text-segment :deep(p) {
  margin: 8px 0;
  line-height: 1.7;
}

.text-segment :deep(p:first-child) {
  margin-top: 0;
}

.text-segment :deep(p:last-child) {
  margin-bottom: 0;
}

/* 列表样式 */
.text-segment :deep(ul),
.text-segment :deep(ol) {
  margin: 8px 0;
  padding-left: 24px;
}

.text-segment :deep(li) {
  margin: 4px 0;
  line-height: 1.6;
}

.text-segment :deep(ul) {
  list-style-type: disc;
}

.text-segment :deep(ol) {
  list-style-type: decimal;
}

/* 引用样式 */
.text-segment :deep(blockquote) {
  margin: 10px 0;
  padding: 8px 16px;
  border-left: 4px solid #409eff;
  background: #f5f7fa;
  color: #606266;
  border-radius: 0 4px 4px 0;
}

.text-segment :deep(blockquote p) {
  margin: 4px 0;
}

.is-user .text-segment :deep(blockquote) {
  border-left-color: rgba(255, 255, 255, 0.6);
  background: rgba(255, 255, 255, 0.1);
  color: rgba(255, 255, 255, 0.9);
}

/* 粗体和斜体 */
.text-segment :deep(strong),
.text-segment :deep(b) {
  font-weight: 600;
}

.text-segment :deep(em),
.text-segment :deep(i) {
  font-style: italic;
}

/* 链接样式 */
.text-segment :deep(a) {
  color: #409eff;
  text-decoration: none;
  border-bottom: 1px solid transparent;
  transition: border-color 0.2s;
}

.text-segment :deep(a:hover) {
  border-bottom-color: #409eff;
}

.is-user .text-segment :deep(a) {
  color: rgba(255, 255, 255, 0.9);
  border-bottom-color: rgba(255, 255, 255, 0.3);
}

.is-user .text-segment :deep(a:hover) {
  border-bottom-color: rgba(255, 255, 255, 0.9);
}

/* 分隔线 */
.text-segment :deep(hr) {
  border: none;
  border-top: 1px solid #ebeef5;
  margin: 16px 0;
}

/* 表格样式 */
.text-segment :deep(table) {
  width: 100%;
  border-collapse: collapse;
  margin: 10px 0;
  font-size: 13px;
}

.text-segment :deep(th),
.text-segment :deep(td) {
  border: 1px solid #ebeef5;
  padding: 8px 12px;
  text-align: left;
}

.text-segment :deep(th) {
  background: #f5f7fa;
  font-weight: 600;
}

/* ========================================
   非命令代码块样式
   ======================================== */

.text-segment :deep(pre) {
  background: #1e1e1e;
  color: #d4d4d4;
  padding: 12px 16px;
  border-radius: 6px;
  overflow-x: auto;
  margin: 10px 0;
  font-size: 13px;
  line-height: 1.5;
  font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
}

.text-segment :deep(code) {
  font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
}

/* 行内代码 */
.text-segment :deep(code:not(pre code)) {
  background: rgba(0, 0, 0, 0.06);
  color: #e6a23c;
  padding: 2px 6px;
  border-radius: 3px;
  font-size: 13px;
}

.is-user .text-segment :deep(code:not(pre code)) {
  background: rgba(255, 255, 255, 0.2);
  color: rgba(255, 255, 255, 0.9);
}

.is-user .text-segment :deep(pre) {
  background: rgba(0, 0, 0, 0.15);
  color: rgba(255, 255, 255, 0.95);
}

/* 命令块可用标记 */
.bubble-body.is-command-available {
  /* 预留扩展点 */
}

/* 流式展示阶段临时命令块占位样式 */
.streaming-command-placeholder pre {
  background: #1e1e1e;
  color: #d4d4d4;
  padding: 12px 16px;
  border-radius: 6px;
  overflow-x: auto;
  margin: 10px 0;
  font-size: 13px;
  line-height: 1.5;
  font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
}
.streaming-command-placeholder code {
  font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
}

/* 可点击选项区（S0/S6 等交互选择阶段） */
.choice-selector {
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px solid #e4e7ed;
}

.choice-hint {
  font-size: 12px;
  color: #909399;
  margin-bottom: 6px;
}

.choice-buttons {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.choice-btn {
  max-width: 280px;
  white-space: normal;
  text-align: left;
  height: auto;
  line-height: 1.4;
  padding: 6px 12px;
}

/* 已交互状态：非选中项置灰 */
.choice-btn--interacted {
  opacity: 0.45;
}

/* 以上都不是补充输入区 */
.free-input-area {
  margin-top: 10px;
  padding: 10px 12px;
  background: #f5f7fa;
  border-radius: 6px;
  border: 1px solid #e4e7ed;
}

.free-input-hint {
  font-size: 12px;
  color: #606266;
  margin-bottom: 8px;
  font-weight: 500;
}

.free-input-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 8px;
}

/* ===== interactive_request 气泡样式 ===== */
.interactive-bubble {
  margin-top: 8px;
  background: #f7f8fa;
  border: 1px solid #e4e7ed;
  border-radius: 8px;
  padding: 12px 14px;
  font-size: 13px;
}

.interactive-title {
  font-weight: 600;
  font-size: 14px;
  color: #303133;
  margin-bottom: 10px;
  padding-bottom: 8px;
  border-bottom: 1px solid #ebeef5;
}

.ir-field {
  margin-bottom: 8px;
}

.ir-label {
  display: inline-block;
  font-size: 11px;
  font-weight: 600;
  color: #909399;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 4px;
}

.ir-route {
  display: block;
  font-size: 12px;
  background: #ecf5ff;
  color: #409eff;
  padding: 4px 8px;
  border-radius: 4px;
  word-break: break-all;
}

.ir-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
  margin-bottom: 8px;
}

.ir-question {
  font-weight: 500;
  color: #303133;
}

.ir-free-input {
  margin-top: 10px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.mt-2 {
  margin-top: 6px;
  align-self: flex-start;
}

/* ===== T-TOOL-18: exec_confirm 气泡样式 ===== */
.exec-confirm-bubble {
  margin-top: 8px;
  background: #fff8e6;
  border: 1px solid #e6a23c;
  border-radius: 8px;
  padding: 12px 14px;
  font-size: 13px;
}

.exec-confirm-title {
  font-weight: 600;
  font-size: 14px;
  color: #303133;
  margin-bottom: 10px;
  padding-bottom: 8px;
  border-bottom: 1px solid #f5dab1;
}

.exec-confirm-icon {
  margin-right: 6px;
}

.exec-confirm-field {
  margin-bottom: 8px;
}

.exec-confirm-label {
  display: inline-block;
  font-size: 11px;
  font-weight: 600;
  color: #909399;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 4px;
}

.exec-confirm-node {
  display: inline-block;
  font-size: 12px;
  background: #ecf5ff;
  color: #409eff;
  padding: 2px 8px;
  border-radius: 4px;
}

.exec-confirm-command-block {
  margin: 10px 0;
  background: #1e1e1e;
  border-radius: 6px;
  padding: 10px 14px;
  overflow-x: auto;
}

.exec-confirm-code {
  color: #d4d4d4;
  font-size: 13px;
  line-height: 1.5;
  font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
  margin: 0;
  white-space: pre-wrap;
  word-break: break-all;
}

.exec-confirm-countdown {
  text-align: right;
  font-size: 12px;
  color: #909399;
  margin-bottom: 8px;
}

.countdown-warning {
  color: #f56c6c;
  font-weight: bold;
}

.exec-confirm-actions {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
  margin-top: 12px;
  padding-top: 10px;
  border-top: 1px solid #f5dab1;
}

.exec-confirm-result {
  display: flex;
  justify-content: flex-end;
  margin-top: 10px;
}
</style>
