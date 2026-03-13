<script setup lang="ts">
import { ref, watch, nextTick } from 'vue'
import { useChatStore } from '@/stores/chat'

/**
 * 终端面板组件
 * 显示终端输入区，接收来自 CommandBlock 的命令
 * 注意：此为基础版本，完整的 SSH 终端功能由 Task 36 实现
 */

const chatStore = useChatStore()
const terminalInput = ref<HTMLTextAreaElement | null>(null)

// 本地输入状态
const localInput = ref('')

// 监听 store 中的待发送命令
watch(
  () => chatStore.terminalInputCommand,
  (newCommand) => {
    if (newCommand) {
      localInput.value = newCommand
      // 清空 store 中的命令
      nextTick(() => {
        chatStore.clearTerminalInput()
        // 聚焦到输入框
        terminalInput.value?.focus()
        // 光标移到末尾
        const len = localInput.value.length
        terminalInput.value?.setSelectionRange(len, len)
      })
    }
  },
  { immediate: true }
)

/**
 * 执行命令
 */
function executeCommand() {
  const command = localInput.value.trim()
  if (!command) return

  // 打印命令到控制台（实际应发送到 SSH 终端）
  console.log('[Terminal] 执行命令:', command)

  // 清空输入
  localInput.value = ''

  // TODO: Task 36 实现实际的 SSH 命令发送
}

/**
 * 键盘事件处理
 */
function handleKeydown(e: KeyboardEvent) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    executeCommand()
  }
}

/**
 * 清空输入
 */
function clearInput() {
  localInput.value = ''
}

/**
 * 关闭面板
 */
function closePanel() {
  chatStore.closeTerminalPanel()
}
</script>

<template>
  <div class="terminal-panel">
    <!-- 面板头部 -->
    <div class="terminal-header">
      <div class="header-left">
        <el-icon class="header-icon"><i class="el-icon-monitor" /></el-icon>
        <span class="header-title">终端</span>
        <el-tag size="small" type="info" effect="plain">模拟模式</el-tag>
      </div>
      <div class="header-actions">
        <el-button
          text
          size="small"
          @click="clearInput"
        >
          <el-icon><i class="el-icon-delete" /></el-icon>
          清空
        </el-button>
        <el-button
          text
          size="small"
          @click="closePanel"
        >
          <el-icon><i class="el-icon-close" /></el-icon>
        </el-button>
      </div>
    </div>

    <!-- 终端输出区域（占位，Task 36 实现） -->
    <div class="terminal-output">
      <div class="output-placeholder">
        <el-icon class="placeholder-icon"><i class="el-icon-monitor" /></el-icon>
        <p>终端连接区域</p>
        <p class="placeholder-sub">Task 36 将实现 SSH 连接与交互终端</p>
      </div>
    </div>

    <!-- 终端输入区域 -->
    <div class="terminal-input-area">
      <div class="input-prompt">
        <span class="prompt-symbol">$</span>
      </div>
      <el-input
        ref="terminalInput"
        v-model="localInput"
        type="textarea"
        :autosize="{ minRows: 1, maxRows: 4 }"
        placeholder="输入命令或从命令卡片发送..."
        class="terminal-input"
        @keydown="handleKeydown"
      />
      <el-button
        type="primary"
        size="small"
        class="execute-btn"
        :disabled="!localInput.trim()"
        @click="executeCommand"
      >
        <el-icon><i class="el-icon-right" /></el-icon>
        执行
      </el-button>
    </div>

    <!-- 底部提示 -->
    <div class="terminal-footer">
      <el-icon class="footer-icon"><i class="el-icon-info-filled" /></el-icon>
      <span>按 Enter 执行命令，Shift+Enter 换行</span>
    </div>
  </div>
</template>

<style scoped>
.terminal-panel {
  background: #1e1e1e;
  border-radius: 8px;
  border: 1px solid #333;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  height: 300px;
}

/* 头部 */
.terminal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 12px;
  background: #2d2d2d;
  border-bottom: 1px solid #333;
}

.header-left {
  display: flex;
  align-items: center;
  gap: 8px;
}

.header-icon {
  font-size: 16px;
  color: #67c23a;
}

.header-title {
  font-size: 14px;
  font-weight: 600;
  color: #e0e0e0;
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 4px;
}

.header-actions :deep(.el-button) {
  color: #9cdcfe;
}

/* 输出区域 */
.terminal-output {
  flex: 1;
  padding: 12px;
  overflow-y: auto;
  background: #1e1e1e;
}

.output-placeholder {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
  color: #666;
  text-align: center;
}

.placeholder-icon {
  font-size: 32px;
  margin-bottom: 12px;
  color: #555;
}

.placeholder-sub {
  font-size: 12px;
  color: #555;
  margin-top: 4px;
}

/* 输入区域 */
.terminal-input-area {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 10px 12px;
  background: #252526;
  border-top: 1px solid #333;
}

.input-prompt {
  display: flex;
  align-items: center;
  height: 32px;
  color: #67c23a;
  font-weight: 600;
  font-family: 'Consolas', 'Monaco', monospace;
}

.prompt-symbol {
  font-size: 14px;
}

.terminal-input {
  flex: 1;
}

.terminal-input :deep(.el-textarea__inner) {
  background: #1e1e1e;
  border-color: #444;
  color: #d4d4d4;
  font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
  font-size: 13px;
  line-height: 1.5;
  padding: 6px 10px;
}

.terminal-input :deep(.el-textarea__inner:focus) {
  border-color: #409eff;
}

.terminal-input :deep(.el-textarea__inner::placeholder) {
  color: #666;
}

.execute-btn {
  height: 32px;
  padding: 0 12px;
}

.execute-btn :deep(.el-icon) {
  margin-right: 4px;
}

/* 底部提示 */
.terminal-footer {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  background: #2d2d2d;
  border-top: 1px solid #333;
  font-size: 12px;
  color: #666;
}

.footer-icon {
  font-size: 14px;
  color: #909399;
}
</style>
