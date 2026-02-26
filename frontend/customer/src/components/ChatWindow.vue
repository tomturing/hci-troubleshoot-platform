<script setup lang="ts">
import { ref, nextTick, watch, onMounted } from 'vue'
import { useChatStore } from '@/stores/chat'
import MessageBubble from './MessageBubble.vue'

const chatStore = useChatStore()
const inputText = ref('')
const messagesContainer = ref<HTMLElement | null>(null)

/** 发送消息 */
async function handleSend() {
  const text = inputText.value.trim()
  if (!text) return
  inputText.value = ''
  await chatStore.sendMessage(text)
}

/** 按 Enter 发送 */
function handleKeydown(e: KeyboardEvent) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    handleSend()
  }
}

/** 自动滚动到底部 */
function scrollToBottom() {
  nextTick(() => {
    if (messagesContainer.value) {
      messagesContainer.value.scrollTop = messagesContainer.value.scrollHeight
    }
  })
}

watch(
  () => chatStore.messages.length,
  () => scrollToBottom(),
)

// 流式内容也要滚动
watch(
  () => {
    const last = chatStore.messages[chatStore.messages.length - 1]
    return last?.content?.length
  },
  () => scrollToBottom(),
)

onMounted(scrollToBottom)
</script>

<template>
  <div class="chat-window">
    <!-- 消息区域 -->
    <div ref="messagesContainer" class="messages-area">
      <MessageBubble
        v-for="msg in chatStore.messages"
        :key="msg.id"
        :message="msg"
      />
      <!-- 加载指示 -->
      <div v-if="chatStore.isLoading" class="loading-indicator">
        <el-icon class="is-loading"><i class="el-icon-loading" /></el-icon>
        <span>处理中...</span>
      </div>
    </div>

    <!-- 输入区域 -->
    <div class="input-area">
      <div class="input-tips" v-if="chatStore.currentCase && chatStore.hasActiveCase">
        输入 <code>/close</code> 关闭当前工单
      </div>
      <div v-if="chatStore.currentCase && !chatStore.hasActiveCase" class="closed-bar">
        <span>当前工单已关闭</span>
        <el-button type="primary" size="small" @click="chatStore.startNewConversation()">
          新建工单
        </el-button>
      </div>
      <div class="input-row">
        <el-input
          v-model="inputText"
          type="textarea"
          :autosize="{ minRows: 1, maxRows: 4 }"
          placeholder="描述您遇到的问题..."
          :disabled="chatStore.isStreaming"
          @keydown="handleKeydown"
        />
        <el-button
          type="primary"
          :icon="chatStore.isStreaming ? '' : ''"
          :loading="chatStore.isStreaming"
          :disabled="!inputText.trim() || chatStore.isStreaming"
          @click="handleSend"
          class="send-btn"
        >
          发送
        </el-button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.chat-window {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: #f0f2f5;
}

.messages-area {
  flex: 1;
  overflow-y: auto;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.loading-indicator {
  display: flex;
  align-items: center;
  gap: 8px;
  color: #909399;
  padding: 8px 16px;
  font-size: 14px;
}

.input-area {
  border-top: 1px solid #e4e7ed;
  background: #fff;
  padding: 12px 16px;
}

.input-tips {
  font-size: 12px;
  color: #909399;
  margin-bottom: 8px;
}

.input-tips code {
  background: #f0f2f5;
  padding: 2px 6px;
  border-radius: 3px;
  font-family: monospace;
  color: #409eff;
}

.closed-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
  padding: 8px 12px;
  background: #f0f2f5;
  border-radius: 6px;
  color: #909399;
  font-size: 14px;
}

.input-row {
  display: flex;
  gap: 8px;
  align-items: flex-end;
}

.input-row :deep(.el-textarea) {
  flex: 1;
}

.send-btn {
  height: 40px;
  min-width: 72px;
}
</style>
