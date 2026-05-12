<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import { useChatStore } from '@/stores/chat'
import { canAutoExecute } from '@/utils/commandRisk'

/**
 * 命令卡片组件
 * 将 AI 输出的命令渲染为独立卡片，提供发送到终端、复制等功能
 * 支持自动执行模式：倒计时 3 秒后通过 SSH 自动执行
 */

// 组件属性定义
interface Props {
  /** 命令内容 */
  command: string
  /** 语言类型 (bash/sh/shell) */
  language?: string
  /** 命令描述/说明 (可选) */
  description?: string
  /** 风险提示 (可选) */
  riskLevel?: 'none' | 'readonly' | 'caution' | 'danger'
  /** 命令索引 (用于追踪) */
  index?: number
  /** 是否为消息中的第一个命令块（仅第一个触发自动执行） */
  isFirstBlock?: boolean
  /** 命令块 ID（传给 store 用于追踪） */
  blockId?: string
}

const props = withDefaults(defineProps<Props>(), {
  language: 'bash',
  description: '',
  riskLevel: 'caution', // 默认 caution，防止推断失败时命令被误判为可自动执行
  index: 0,
  isFirstBlock: false,
  blockId: '',
})

// 使用 chat store 获取终端相关状态
const chatStore = useChatStore()

// 本地状态
const isExpanded = ref(false) // 说明区域展开状态
const isCopied = ref(false) // 复制状态

// 自动执行状态机
type AutoExecState = 'idle' | 'countdown' | 'executing' | 'success' | 'failed'
const autoExecState = ref<AutoExecState>('idle')
const countdown = ref(3)
let countdownTimer: ReturnType<typeof setInterval> | null = null
const execExitCode = ref<number | null>(null)

/**
 * 风险提示标签映射
 */
const riskLabelMap = {
  none: { text: '安全', type: 'success' as const },
  readonly: { text: '只读', type: 'info' as const },
  caution: { text: '谨慎', type: 'warning' as const },
  danger: { text: '高危', type: 'danger' as const },
}

const riskLabel = computed(() => riskLabelMap[props.riskLevel])

const impactText = computed(() => {
  if (props.riskLevel === 'danger') return '高风险操作，可能影响业务稳定性'
  if (props.riskLevel === 'caution') return '存在潜在影响，建议先确认执行环境'
  return '只读查询，无风险'
})

const impactClass = computed(() => {
  if (props.riskLevel === 'danger') return 'impact-danger'
  if (props.riskLevel === 'caution') return 'impact-caution'
  return 'impact-safe'
})

/**
 * 处理后的命令内容
 * 保持原始格式，不做任何修改
 */
const processedCommand = computed(() => {
  // 不修改命令内容，保持原样（包括换行符、反斜杠等）
  return props.command
})

/**
 * 多行命令检测
 */
const isMultiLine = computed(() => {
  return props.command.includes('\n')
})

/**
 * 复制命令到剪贴板
 */
function copyCommand() {
  const text = processedCommand.value
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text)
  } else {
    // 非安全上下文降级方案
    const textarea = document.createElement('textarea')
    textarea.value = text
    textarea.style.position = 'fixed'
    textarea.style.opacity = '0'
    document.body.appendChild(textarea)
    textarea.select()
    try {
      document.execCommand('copy')
    } catch (e) {
      console.warn('复制失败:', e)
    }
    document.body.removeChild(textarea)
  }
  isCopied.value = true
  setTimeout(() => (isCopied.value = false), 2000)
}

/**
 * 发送到终端
 * 将命令发送到终端输入区，不自动执行
 */
function sendToTerminal() {
  // 调用 store 方法发送命令到终端
  chatStore.sendCommandToTerminal(processedCommand.value)
}

// ===== 自动执行逻辑 =====

/**
 * 是否满足自动执行条件：
 * - 是该消息的第一个命令块
 * - 风险等级允许（canAutoExecute 判断）
 * - SSH 已连接
 * - 没有命令正在执行（串行锁）
 */
const shouldAutoExec = computed(() =>
  props.isFirstBlock &&
  canAutoExecute(props.riskLevel ?? 'caution', chatStore.autoExecuteMode) &&
  chatStore.sshConnectionState === 'connected' &&
  !chatStore.isExecutingCommand &&
  chatStore.pageVisible, // 页面隐藏时不触发自动执行
)

/** 清理倒计时 */
function clearCountdown() {
  if (countdownTimer !== null) {
    clearInterval(countdownTimer)
    countdownTimer = null
  }
}

/** 取消自动执行（用户手动取消） */
function cancelAutoExec() {
  clearCountdown()
  autoExecState.value = 'idle'
}

/** 立即执行命令（不再等待倒计时） */
async function doExecute() {
  clearCountdown()
  autoExecState.value = 'executing'
  const id = props.blockId || `block-${props.index}`
  try {
    const result = await chatStore.executeCommandViaSSH(processedCommand.value, id)
    execExitCode.value = result.exitCode
    autoExecState.value = result.exitCode === 0 ? 'success' : 'failed'
  } catch (_e) {
    execExitCode.value = -1
    autoExecState.value = 'failed'
  }
}

/** 启动倒计时 */
function startCountdown() {
  if (autoExecState.value !== 'idle') return
  countdown.value = 3
  autoExecState.value = 'countdown'
  countdownTimer = setInterval(() => {
    countdown.value -= 1
    if (countdown.value <= 0) {
      clearCountdown()
      doExecute()
    }
  }, 1000)
}

// 挂载后若条件满足则立即启动倒计时
onMounted(() => {
  if (shouldAutoExec.value) {
    startCountdown()
  }
})

// 若执行中途条件不再满足（如用户关闭自动执行），取消倒计时
watch(shouldAutoExec, (val) => {
  if (!val && autoExecState.value === 'countdown') {
    cancelAutoExec()
  }
})

onBeforeUnmount(() => {
  clearCountdown()
})

/**
 * 切换说明展开状态
 */
function toggleDescription() {
  isExpanded.value = !isExpanded.value
}
</script>

<template>
  <div
    class="command-block"
    :class="{
      'is-multi-line': isMultiLine,
      [`risk-${riskLevel}`]: true,
    }"
  >
    <!-- 卡片头部：语言标识 + 风险标签 + 操作按钮 -->
    <div class="command-header">
      <div class="header-left">
        <span class="lang-badge">{{ language }}</span>
        <el-tag
          :type="riskLabel.type"
          size="small"
          class="risk-tag"
          effect="light"
        >
          {{ riskLabel.text }}
        </el-tag>
      </div>
      <div class="header-actions">
        <el-button
          v-if="description"
          text
          size="small"
          class="action-btn"
          @click="toggleDescription"
        >
          <el-icon><i class="el-icon-info-filled" /></el-icon>
          <span>{{ isExpanded ? '收起' : '说明' }}</span>
        </el-button>
        <el-button
          text
          size="small"
          class="action-btn"
          @click="copyCommand"
        >
          <el-icon><i class="el-icon-copy-document" /></el-icon>
          <span>{{ isCopied ? '已复制' : '复制' }}</span>
        </el-button>

        <!-- 自动执行：倒计时状态 -->
        <template v-if="autoExecState === 'countdown'">
          <el-button
            type="primary"
            size="small"
            class="auto-exec-btn countdown-btn"
            @click="doExecute"
          >
            ▶ {{ countdown }}s 后自动执行
          </el-button>
          <el-button
            size="small"
            class="action-btn cancel-btn"
            @click="cancelAutoExec"
          >
            ✕
          </el-button>
        </template>

        <!-- 自动执行：执行中状态 -->
        <template v-else-if="autoExecState === 'executing'">
          <el-button
            type="info"
            size="small"
            class="auto-exec-btn"
            :loading="true"
            disabled
          >
            执行中
          </el-button>
        </template>

        <!-- 自动执行：成功状态 -->
        <template v-else-if="autoExecState === 'success'">
          <el-tag type="success" size="small" class="exec-result-tag">
            ✓ 已执行 (exit {{ execExitCode }})
          </el-tag>
        </template>

        <!-- 自动执行：失败状态 -->
        <template v-else-if="autoExecState === 'failed'">
          <el-tag type="danger" size="small" class="exec-result-tag">
            ✗ 执行失败 (exit {{ execExitCode }})
          </el-tag>
        </template>

        <!-- 默认：手动发送按钮 -->
        <el-button
          v-else
          type="primary"
          size="small"
          class="send-btn"
          @click="sendToTerminal"
        >
          <el-icon><i class="el-icon-upload2" /></el-icon>
          <span>发送到终端</span>
        </el-button>
      </div>
    </div>

    <!-- 命令内容区域 -->
    <div class="command-body">
      <pre class="command-code"><code>{{ processedCommand }}</code></pre>
    </div>

    <!-- 可展开的说明区域 -->
    <div v-if="description && isExpanded" class="command-description">
      <div class="description-content">
        <p>{{ description }}</p>
      </div>
    </div>

    <!-- 底部提示信息 -->
    <div class="command-footer">
      <span class="footer-title">影响说明：</span>
      <span class="footer-text" :class="impactClass">{{ impactText }}</span>
    </div>
  </div>
</template>

<style scoped>
/* 命令卡片主容器 */
.command-block {
  margin: 12px 0;
  border-radius: 8px;
  background: #1e1e1e;
  border: 1px solid #333;
  overflow: hidden;
  font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
}

/* 风险等级样式 */
.command-block.risk-none {
  border-left: 3px solid #67c23a;
}

.command-block.risk-readonly {
  border-left: 3px solid #909399;
}

.command-block.risk-caution {
  border-left: 3px solid #e6a23c;
}

.command-block.risk-danger {
  border-left: 3px solid #f56c6c;
}

/* 头部区域 */
.command-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px;
  background: #2d2d2d;
  border-bottom: 1px solid #333;
}

.header-left {
  display: flex;
  align-items: center;
  gap: 8px;
}

.lang-badge {
  font-size: 11px;
  color: #9cdcfe;
  text-transform: lowercase;
  font-family: 'Consolas', 'Monaco', monospace;
}

.risk-tag {
  font-size: 11px;
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 4px;
}

.action-btn {
  color: #9cdcfe;
  padding: 4px 8px;
}

.action-btn:hover {
  color: #fff;
}

.send-btn {
  padding: 4px 12px;
}

.send-btn :deep(span) {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
}

.send-btn :deep(.el-icon) {
  margin-right: 4px;
}

/* 自动执行按钮样式 */
.auto-exec-btn {
  padding: 4px 10px;
  font-size: 12px;
}

.countdown-btn {
  animation: pulse 1s ease-in-out infinite alternate;
}

@keyframes pulse {
  from { opacity: 0.85; }
  to { opacity: 1; }
}

.cancel-btn {
  color: #f56c6c;
  padding: 4px 6px;
  min-width: unset;
}

.exec-result-tag {
  font-size: 11px;
}

/* 命令内容区域 */
.command-body {
  padding: 12px 16px;
  overflow-x: auto;
}

.command-code {
  margin: 0;
  padding: 0;
  background: transparent;
  color: #d4d4d4;
  font-size: 13px;
  line-height: 1.6;
  white-space: pre;
  word-break: break-all;
  font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
}

/* 说明区域 */
.command-description {
  padding: 12px 16px;
  background: #252526;
  border-top: 1px solid #333;
}

.description-content {
  color: #9cdcfe;
  font-size: 13px;
  line-height: 1.6;
}

.description-content p {
  margin: 0;
}

/* 底部提示 */
.command-footer {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 12px;
  background: #2d2d2d;
  border-top: 1px solid #333;
  font-size: 12px;
  color: #909399;
}

.footer-title {
  color: #909399;
}

.footer-text {
  flex: 1;
}

.footer-text.impact-safe {
  color: #67c23a;
}

.footer-text.impact-caution {
  color: #e6a23c;
}

.footer-text.impact-danger {
  color: #f56c6c;
}

/* 多行命令特殊样式 */
.command-block.is-multi-line .command-code {
  min-height: 40px;
}
</style>
