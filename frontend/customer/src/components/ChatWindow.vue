<script setup lang="ts">
import { ref, reactive, nextTick, watch, onMounted } from 'vue'
import { useChatStore } from '@/stores/chat'
import MessageBubble from './MessageBubble.vue'

const chatStore = useChatStore()
const inputText = ref('')
const messagesContainer = ref<HTMLElement | null>(null)

// 工单创建模板本地编辑副本
const templateForm = reactive({ title: '', description: '' })

// 监听 store 弹出模板时同步到本地编辑
watch(() => chatStore.showCaseTemplate, (val) => {
  if (val) {
    templateForm.title = chatStore.caseTemplate.title
    templateForm.description = chatStore.caseTemplate.description
  }
})

/** 确认创建工单 */
function handleConfirmTemplate() {
  chatStore.confirmCreateCase({ ...templateForm })
}

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
    <!-- 未关闭工单确认对话框 -->
    <el-dialog
      v-model="chatStore.showPendingDialog"
      title="发现未关闭的工单"
      width="420px"
      :close-on-click-modal="false"
      :close-on-press-escape="false"
      :show-close="false"
      align-center
    >
      <div class="pending-dialog-content">
        <p>您有一个未关闭的工单：</p>
        <div class="pending-case-info" v-if="chatStore.pendingCase">
          <div><strong>工单号：</strong>{{ chatStore.pendingCase.case_id }}</div>
          <div><strong>标题：</strong>{{ chatStore.pendingCase.title }}</div>
          <div><strong>状态：</strong>{{ chatStore.pendingCase.status }}</div>
        </div>
        <p>请选择操作：</p>
      </div>
      <template #footer>
        <el-button @click="chatStore.closePendingCase()">关闭旧工单</el-button>
        <el-button type="primary" @click="chatStore.resumePendingCase()">继续处理</el-button>
      </template>
    </el-dialog>

    <!-- 工单创建模板对话框 -->
    <el-dialog
      v-model="chatStore.showCaseTemplate"
      title="创建工单"
      width="500px"
      :close-on-click-modal="false"
      align-center
    >
      <div class="template-dialog-content">
        <p class="template-hint">请确认或编辑工单信息后提交：</p>
        <el-form label-position="top">
          <el-form-item label="工单标题">
            <el-input v-model="templateForm.title" placeholder="简要描述问题" maxlength="100" show-word-limit />
          </el-form-item>
          <el-form-item label="问题描述">
            <el-input
              v-model="templateForm.description"
              type="textarea"
              :autosize="{ minRows: 3, maxRows: 8 }"
              placeholder="详细描述您遇到的问题..."
            />
          </el-form-item>
        </el-form>
      </div>
      <template #footer>
        <el-button @click="chatStore.cancelCreateCase()">取消</el-button>
        <el-button
          type="primary"
          :disabled="!templateForm.title.trim() || !templateForm.description.trim()"
          @click="handleConfirmTemplate"
        >
          提交工单
        </el-button>
      </template>
    </el-dialog>

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
          :placeholder="chatStore.isCaseClosed ? '工单已关闭，请点击「新建工单」' : '描述您遇到的问题...'"
          :disabled="chatStore.isStreaming || chatStore.isCaseClosed"
          @keydown="handleKeydown"
        />
        <el-button
          type="primary"
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

/* 未关闭工单对话框 */
.pending-dialog-content p {
  margin: 8px 0;
  color: #606266;
}

.pending-case-info {
  background: #f5f7fa;
  padding: 12px 16px;
  border-radius: 6px;
  margin: 12px 0;
  font-size: 14px;
  line-height: 1.8;
}

.pending-case-info strong {
  color: #303133;
}

/* 工单创建模板对话框 */
.template-hint {
  color: #909399;
  font-size: 13px;
  margin-bottom: 12px;
}
</style>
