<script setup lang="ts">
import { computed, ref, nextTick, onMounted, watch } from 'vue'
import type { ChatMessage } from '@/stores/chat'
import { renderMarkdown, isCommandLanguage } from '@/utils/markdown'

const props = defineProps<{ message: ChatMessage }>()

const isUser = computed(() => props.message.role === 'user')
const isSystem = computed(() => props.message.role === 'system')
const isAssistant = computed(() => props.message.role === 'assistant')
const isDivider = computed(() => isSystem.value && props.message.content.includes('────'))

/** 已复制状态的代码块索引 */
const copiedBlocks = ref<Set<number>>(new Set())

/** 复制整条消息内容 */
const copiedMessage = ref(false)
function copyContent() {
  const text = props.message.content
  copyToClipboard(text)
  copiedMessage.value = true
  setTimeout(() => (copiedMessage.value = false), 2000)
}

/** 复制代码块 */
function copyCodeBlock(index: number, code: string) {
  copyToClipboard(code)
  copiedBlocks.value.add(index)
  setTimeout(() => copiedBlocks.value.delete(index), 2000)
}

/** 复制到剪贴板（兼容非安全上下文） */
function copyToClipboard(text: string) {
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text)
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
    } catch (e) {
      console.warn('复制失败:', e)
    }
    document.body.removeChild(textarea)
  }
}

/** 格式化时间 */
function formatTime(d: Date) {
  return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
}

/** 渲染后的 HTML 内容 */
const renderedContent = computed(() => {
  if (!props.message.content) return ''
  return renderMarkdown(props.message.content)
})

/** 代码块数据，用于挂载复制按钮 */
const codeBlocks = computed(() => {
  const regex = /```(\w*)\n([\s\S]*?)```/g
  const blocks: Array<{ language: string; code: string; isCommand: boolean }> = []
  let match
  while ((match = regex.exec(props.message.content)) !== null) {
    const lang = match[1] || 'plaintext'
    blocks.push({
      language: lang,
      code: match[2].trim(),
      isCommand: isCommandLanguage(lang),
    })
  }
  return blocks
})

/** 为代码块添加复制按钮 */
function setupCodeBlockButtons() {
  if (!isAssistant.value) return

  nextTick(() => {
    const container = document.querySelector('.bubble-body-rendered')
    if (!container) return

    const preBlocks = container.querySelectorAll('pre')
    preBlocks.forEach((pre, index) => {
      // 避免重复添加
      if (pre.querySelector('.code-block-header')) return

      const code = pre.querySelector('code')
      const language = code?.className.replace('language-', '') || 'plaintext'
      const codeText = code?.textContent || ''

      // 创建代码块头部（语言标识 + 复制按钮）
      const header = document.createElement('div')
      header.className = 'code-block-header'

      const langLabel = document.createElement('span')
      langLabel.className = 'code-block-lang'
      langLabel.textContent = language

      const copyBtn = document.createElement('button')
      copyBtn.className = 'code-block-copy-btn'
      copyBtn.textContent = '复制'
      copyBtn.onclick = () => {
        copyCodeBlock(index, codeText)
        copyBtn.textContent = '已复制'
        setTimeout(() => (copyBtn.textContent = '复制'), 2000)
      }

      // 为命令块添加特殊样式
      if (isCommandLanguage(language)) {
        pre.classList.add('is-command-block')
      }

      header.appendChild(langLabel)
      header.appendChild(copyBtn)

      // 将 header 插入到 pre 前面
      pre.parentNode?.insertBefore(header, pre)
    })
  })
}

// 监听内容变化，重新设置代码块按钮（流式输出场景）
onMounted(setupCodeBlockButtons)

// 流式输出时内容会持续变化，需要重新设置按钮
watch(
  () => props.message.content,
  () => nextTick(setupCodeBlockButtons),
)
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
        <!-- 使用新的 Markdown 渲染 -->
        <div
          class="bubble-body bubble-body-rendered"
          :class="{ 'is-command-available': codeBlocks.some(b => b.isCommand) }"
          v-html="renderedContent"
        />
        <div class="bubble-meta">
          <span class="bubble-time">{{ formatTime(message.timestamp) }}</span>
          <el-button
            v-if="isAssistant && message.content"
            :icon="copiedMessage ? '' : ''"
            size="small"
            text
            @click="copyContent"
          >
            {{ copiedMessage ? '已复制' : '复制' }}
          </el-button>
        </div>
        <!-- 流式动画 -->
        <span v-if="message.isStreaming" class="streaming-cursor">▊</span>
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
   Markdown 渲染样式
   ======================================== */

.bubble-body-rendered {
  word-break: break-word;
}

/* 标题样式 */
.bubble-content :deep(h1) {
  font-size: 1.5em;
  font-weight: 600;
  margin: 16px 0 8px 0;
  padding-bottom: 6px;
  border-bottom: 1px solid #ebeef5;
  line-height: 1.4;
}

.bubble-content :deep(h2) {
  font-size: 1.3em;
  font-weight: 600;
  margin: 14px 0 6px 0;
  line-height: 1.4;
}

.bubble-content :deep(h3) {
  font-size: 1.15em;
  font-weight: 600;
  margin: 12px 0 4px 0;
  line-height: 1.4;
}

.bubble-content :deep(h4),
.bubble-content :deep(h5),
.bubble-content :deep(h6) {
  font-size: 1em;
  font-weight: 600;
  margin: 10px 0 4px 0;
  line-height: 1.4;
}

/* 段落样式 */
.bubble-content :deep(p) {
  margin: 8px 0;
  line-height: 1.7;
}

.bubble-content :deep(p:first-child) {
  margin-top: 0;
}

.bubble-content :deep(p:last-child) {
  margin-bottom: 0;
}

/* 列表样式 */
.bubble-content :deep(ul),
.bubble-content :deep(ol) {
  margin: 8px 0;
  padding-left: 24px;
}

.bubble-content :deep(li) {
  margin: 4px 0;
  line-height: 1.6;
}

.bubble-content :deep(ul) {
  list-style-type: disc;
}

.bubble-content :deep(ol) {
  list-style-type: decimal;
}

/* 引用样式 */
.bubble-content :deep(blockquote) {
  margin: 10px 0;
  padding: 8px 16px;
  border-left: 4px solid #409eff;
  background: #f5f7fa;
  color: #606266;
  border-radius: 0 4px 4px 0;
}

.bubble-content :deep(blockquote p) {
  margin: 4px 0;
}

.is-user .bubble-content :deep(blockquote) {
  border-left-color: rgba(255, 255, 255, 0.6);
  background: rgba(255, 255, 255, 0.1);
  color: rgba(255, 255, 255, 0.9);
}

/* 粗体和斜体 */
.bubble-content :deep(strong),
.bubble-content :deep(b) {
  font-weight: 600;
}

.bubble-content :deep(em),
.bubble-content :deep(i) {
  font-style: italic;
}

/* 链接样式 */
.bubble-content :deep(a) {
  color: #409eff;
  text-decoration: none;
  border-bottom: 1px solid transparent;
  transition: border-color 0.2s;
}

.bubble-content :deep(a:hover) {
  border-bottom-color: #409eff;
}

.is-user .bubble-content :deep(a) {
  color: rgba(255, 255, 255, 0.9);
  border-bottom-color: rgba(255, 255, 255, 0.3);
}

.is-user .bubble-content :deep(a:hover) {
  border-bottom-color: rgba(255, 255, 255, 0.9);
}

/* 分隔线 */
.bubble-content :deep(hr) {
  border: none;
  border-top: 1px solid #ebeef5;
  margin: 16px 0;
}

/* 表格样式 */
.bubble-content :deep(table) {
  width: 100%;
  border-collapse: collapse;
  margin: 10px 0;
  font-size: 13px;
}

.bubble-content :deep(th),
.bubble-content :deep(td) {
  border: 1px solid #ebeef5;
  padding: 8px 12px;
  text-align: left;
}

.bubble-content :deep(th) {
  background: #f5f7fa;
  font-weight: 600;
}

/* ========================================
   代码块样式
   ======================================== */

/* 代码块头部（语言标识 + 复制按钮） */
.bubble-content :deep(.code-block-header) {
  display: flex;
  justify-content: space-between;
  align-items: center;
  background: #2d2d2d;
  padding: 6px 12px;
  border-radius: 6px 6px 0 0;
  margin-top: 10px;
}

.bubble-content :deep(.code-block-lang) {
  font-size: 12px;
  color: #9cdcfe;
  font-family: 'Consolas', 'Monaco', monospace;
  text-transform: lowercase;
}

.bubble-content :deep(.code-block-copy-btn) {
  background: transparent;
  border: 1px solid #4a4a4a;
  color: #9cdcfe;
  font-size: 12px;
  padding: 2px 10px;
  border-radius: 4px;
  cursor: pointer;
  transition: all 0.2s;
}

.bubble-content :deep(.code-block-copy-btn:hover) {
  background: #3a3a3a;
  border-color: #5a5a5a;
}

/* 代码块主体 */
.bubble-content :deep(pre) {
  background: #1e1e1e;
  color: #d4d4d4;
  padding: 12px 16px;
  border-radius: 0 0 6px 6px;
  overflow-x: auto;
  margin: 0;
  font-size: 13px;
  line-height: 1.5;
  font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
}

/* 有 header 的代码块去掉顶部圆角 */
.bubble-content :deep(.code-block-header + pre) {
  border-radius: 0 0 6px 6px;
}

/* 单独的代码块（无 header）保持完整圆角 */
.bubble-content :deep(pre:not(.code-block-header + pre)) {
  border-radius: 6px;
  margin: 10px 0;
}

/* 命令块特殊样式 */
.bubble-content :deep(.is-command-block) {
  border-left: 3px solid #67c23a;
}

/* 行内代码 */
.bubble-content :deep(code:not(pre code)) {
  background: rgba(0, 0, 0, 0.06);
  color: #e6a23c;
  padding: 2px 6px;
  border-radius: 3px;
  font-family: 'Consolas', 'Monaco', monospace;
  font-size: 13px;
}

.is-user .bubble-content :deep(code:not(pre code)) {
  background: rgba(255, 255, 255, 0.2);
  color: rgba(255, 255, 255, 0.9);
}

/* 用户消息中的代码块样式调整 */
.is-user .bubble-content :deep(pre) {
  background: rgba(0, 0, 0, 0.15);
  color: rgba(255, 255, 255, 0.95);
}

.is-user .bubble-content :deep(.code-block-header) {
  background: rgba(0, 0, 0, 0.2);
}

.is-user .bubble-content :deep(.code-block-lang) {
  color: rgba(255, 255, 255, 0.7);
}

.is-user .bubble-content :deep(.code-block-copy-btn) {
  color: rgba(255, 255, 255, 0.7);
  border-color: rgba(255, 255, 255, 0.3);
}

.is-user .bubble-content :deep(.code-block-copy-btn:hover) {
  background: rgba(255, 255, 255, 0.1);
}

/* 命令块可用标记（用于 Task 35 命令卡片扩展） */
.bubble-body-rendered.is-command-available {
  /* 预留扩展点 */
}
</style>