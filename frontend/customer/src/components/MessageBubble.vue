<script setup lang="ts">
import { computed, ref } from 'vue'
import type { ChatMessage } from '@/stores/chat'

const props = defineProps<{ message: ChatMessage }>()

const isUser = computed(() => props.message.role === 'user')
const isSystem = computed(() => props.message.role === 'system')
const isAssistant = computed(() => props.message.role === 'assistant')
const isDivider = computed(() => isSystem.value && props.message.content.includes('────'))

/** 复制代码块 */
const copied = ref(false)
function copyContent() {
  navigator.clipboard.writeText(props.message.content).then(() => {
    copied.value = true
    setTimeout(() => (copied.value = false), 2000)
  })
}

/** 格式化时间 */
function formatTime(d: Date) {
  return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
}
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
        <div class="bubble-body" v-html="renderContent(message.content)" />
        <div class="bubble-meta">
          <span class="bubble-time">{{ formatTime(message.timestamp) }}</span>
          <el-button
            v-if="isAssistant && message.content"
            :icon="copied ? '' : ''"
            size="small"
            text
            @click="copyContent"
          >
            {{ copied ? '已复制' : '复制' }}
          </el-button>
        </div>
        <!-- 流式动画 -->
        <span v-if="message.isStreaming" class="streaming-cursor">▊</span>
      </div>
    </template>
  </div>
</template>

<script lang="ts">
/** 简单的内容渲染：处理代码块和换行 */
function renderContent(text: string): string {
  if (!text) return ''
  // 代码块
  let html = text.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => {
    return `<pre class="code-block"><code class="language-${lang}">${escapeHtml(code.trim())}</code></pre>`
  })
  // 行内代码
  html = html.replace(/`([^`]+)`/g, '<code class="inline-code">$1</code>')
  // 换行
  html = html.replace(/\n/g, '<br />')
  return html
}

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
}
</script>

<style scoped>
.message-bubble {
  display: flex;
  gap: 10px;
  max-width: 80%;
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
  padding: 10px 14px;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.08);
  position: relative;
  line-height: 1.6;
  font-size: 14px;
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
  margin-top: 4px;
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

/* 代码块样式 */
.bubble-content :deep(.code-block) {
  background: #1e1e1e;
  color: #d4d4d4;
  padding: 12px;
  border-radius: 6px;
  overflow-x: auto;
  margin: 8px 0;
  font-size: 13px;
  line-height: 1.4;
}

.bubble-content :deep(.inline-code) {
  background: rgba(0, 0, 0, 0.06);
  padding: 2px 6px;
  border-radius: 3px;
  font-family: 'Consolas', 'Monaco', monospace;
  font-size: 13px;
}

.is-user .bubble-content :deep(.inline-code) {
  background: rgba(255, 255, 255, 0.2);
}
</style>
