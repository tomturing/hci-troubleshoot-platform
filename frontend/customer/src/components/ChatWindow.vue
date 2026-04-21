<script setup lang="ts">
import { ref, nextTick, watch, onMounted, onBeforeUnmount } from 'vue'
import { useChatStore } from '@/stores/chat'
import MessageBubble from './MessageBubble.vue'
import RatingCard from './RatingCard.vue'
import TerminalPanel from './TerminalPanel.vue'
import ConfirmDialog from './ConfirmDialog.vue'
import DiagnosticProgress from './DiagnosticProgress.vue'
import CaseCreateDialog from './CaseCreateDialog.vue'
import EnvironmentSummary from './EnvironmentSummary.vue'

const chatStore = useChatStore()
const inputText = ref('')
const messagesContainer = ref<HTMLElement | null>(null)
const assistantInputRef = ref<{ focus: () => void } | null>(null)
const terminalPinned = ref(false)
const historyPinned = ref(false)
const terminalPanelWidth = ref(getDefaultPanelWidth())
const historyPanelWidth = ref(getDefaultPanelWidth())

// 拖拽调整抽屉宽度
const startResizeTerminal = (e: MouseEvent) => {
  e.preventDefault()
  const startX = e.clientX
  const startWidth = terminalPanelWidth.value
  const onMouseMove = (moveE: MouseEvent) => {
    terminalPanelWidth.value = Math.max(300, Math.min(window.innerWidth * 0.8, startWidth - (moveE.clientX - startX)))
  }
  const onMouseUp = () => {
    document.removeEventListener('mousemove', onMouseMove)
    document.removeEventListener('mouseup', onMouseUp)
  }
  document.addEventListener('mousemove', onMouseMove)
  document.addEventListener('mouseup', onMouseUp)
}

const startResizeHistory = (e: MouseEvent) => {
  e.preventDefault()
  const startX = e.clientX
  const startWidth = historyPanelWidth.value
  const onMouseMove = (moveE: MouseEvent) => {
    historyPanelWidth.value = Math.max(300, Math.min(window.innerWidth * 0.8, startWidth + (moveE.clientX - startX)))
  }
  const onMouseUp = () => {
    document.removeEventListener('mousemove', onMouseMove)
    document.removeEventListener('mouseup', onMouseUp)
  }
  document.addEventListener('mousemove', onMouseMove)
  document.addEventListener('mouseup', onMouseUp)
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

watch(
  () => chatStore.assistantDraftText,
  (text) => {
    if (!text) return
    inputText.value = text
    chatStore.clearAssistantDraftText()
    nextTick(() => assistantInputRef.value?.focus())
  },
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

onMounted(() => {
  const onResize = () => {
    if (!chatStore.showTerminalSidebar) {
      terminalPanelWidth.value = getDefaultPanelWidth()
    }
    if (!chatStore.showHistoryDrawer) {
      historyPanelWidth.value = getDefaultPanelWidth()
    }
  }
  onResize()
  window.addEventListener('resize', onResize)
  ;(window as Window & { __hciPanelResizeHandler?: () => void }).__hciPanelResizeHandler = onResize
})

onBeforeUnmount(() => {
  const handler = (window as Window & { __hciPanelResizeHandler?: () => void }).__hciPanelResizeHandler
  if (handler) window.removeEventListener('resize', handler)
})

function getDefaultPanelWidth(): number {
  if (typeof window === 'undefined') return 510
  const appMaxWidth = 900
  // Each side gets half of the remaining blank space
  const sideBlankSpace = Math.max((window.innerWidth - appMaxWidth) / 2, 0)
  // If the screen is too narrow, just take 85% of screen
  const target = sideBlankSpace >= 300 ? sideBlankSpace : window.innerWidth * 0.85
  // We constrain it to a min of 400 and max of 50% to not dominate small screens
  return Math.max(400, Math.round(target))
}

/** 状态中文映射 */
function statusLabel(status: string): string {
  const map: Record<string, string> = {
    created: '待确认', confirmed: '已确认', in_progress: '处理中',
    resolved: '已解决', closed: '已关闭', cancelled: '已取消',
  }
  return map[status] || status
}

/** 状态颜色映射 */
function statusType(status: string): '' | 'success' | 'warning' | 'danger' | 'info' {
  const map: Record<string, '' | 'success' | 'warning' | 'danger' | 'info'> = {
    created: 'warning', confirmed: '', in_progress: '',
    resolved: 'success', closed: 'info', cancelled: 'danger',
  }
  return map[status] || 'info'
}

/** 格式化时间 */
function formatDate(d: string): string {
  return new Date(d).toLocaleString('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
  })
}
</script>

<template>
  <div class="chat-window">
    <!-- Agent 模式：高风险操作确认弹窗 -->
    <ConfirmDialog
      v-if="chatStore.pendingConfirm"
      :event="{ type: 'confirm_request', ...chatStore.pendingConfirm }"
      :session-id="chatStore.conversationId ?? ''"
      @confirmed="chatStore.handleConfirmResult"
    />

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

    <!-- SSH 集成创建工单对话框 -->
    <CaseCreateDialog />

    <!-- 消息区域 -->
    <!-- AI 助手选择器（v2.2：对话界面顶部，始终可见）-->
    <div class="assistant-selector-bar" v-if="chatStore.showAssistantSelector && chatStore.hasActiveCase">
      <span class="assistant-label">当前助手：</span>
      <el-select
        v-model="chatStore.selectedAssistant"
        size="small"
        placeholder="选择 AI 助手"
        style="width: 180px"
      >
        <el-option
          v-for="assistant in chatStore.assistants"
          :key="assistant.type"
          :label="assistant.display_name"
          :value="assistant.type"
          :disabled="!assistant.available"
        >
          <div class="assistant-option-compact">
            <span>{{ assistant.display_name }}</span>
            <el-tag v-if="assistant.is_default" size="small" type="success">默认</el-tag>
            <el-tag v-if="!assistant.available" size="small" type="warning">不可用</el-tag>
          </div>
        </el-option>
      </el-select>
    </div>
    <!-- 诊断阶段进度条 (仅在非 S0 时显示) -->
    <DiagnosticProgress
      v-if="chatStore.diagnosticStage !== 'S0'"
      :stage="chatStore.diagnosticStage"
    />
    <!-- 环境数据卡片（工单创建成功后显示） -->
    <EnvironmentSummary v-if="chatStore.hasActiveCase" />
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

      <!-- 评分卡 -->
      <RatingCard
        :visible="chatStore.showRatingCard"
        :conversation-id="chatStore.ratingConversationId"
        @submit="chatStore.submitRating"
        @skip="chatStore.skipRating"
        @close="chatStore.closeRatingCard"
      />
    </div>

    <!-- SSH 终端抽屉 -->
    <el-drawer
      v-model="chatStore.showTerminalSidebar"
      direction="rtl"
      :size="`${terminalPanelWidth}px`"
      :append-to-body="true"
      :modal="true"
      :lock-scroll="!terminalPinned"
      :close-on-click-modal="!terminalPinned"
      :close-on-press-escape="!terminalPinned"
      :with-header="true"
      :modal-class="terminalPinned ? 'drawer-unblocked-overlay' : ''"
      class="workspace-drawer"
      @close="chatStore.closeTerminalSidebar()"
    >
      <div class="drawer-resize-handle drawer-resize-left" @mousedown="startResizeTerminal"></div>
      <template #header>
        <div class="drawer-header">
          <span class="drawer-title">SSH 终端</span>
          <div class="drawer-actions">
            <el-button text size="small" @click="terminalPinned = !terminalPinned">
              {{ terminalPinned ? '取消钉住' : '钉住' }}
            </el-button>
          </div>
        </div>
      </template>
      <div class="drawer-body-content terminal-content">
        <TerminalPanel />
      </div>
    </el-drawer>

    <!-- 历史工单抽屉 -->
    <el-drawer
      v-model="chatStore.showHistoryDrawer"
      direction="ltr"
      :size="`${historyPanelWidth}px`"
      :append-to-body="true"
      :modal="true"
      :lock-scroll="!historyPinned"
      :close-on-click-modal="!historyPinned"
      :close-on-press-escape="!historyPinned"
      :with-header="true"
      :modal-class="historyPinned ? 'drawer-unblocked-overlay' : ''"
      class="workspace-drawer"
      @close="chatStore.closeHistoryDrawer()"
    >
      <div class="drawer-resize-handle drawer-resize-right" @mousedown="startResizeHistory"></div>
      <template #header>
        <div class="drawer-header">
          <span class="drawer-title">历史工单</span>
          <div class="drawer-actions">
            <el-button text size="small" @click="historyPinned = !historyPinned">
              {{ historyPinned ? '取消钉住' : '钉住' }}
            </el-button>
          </div>
        </div>
      </template>
      <div class="drawer-body-content">
        <!-- 左右分栏：列表 + 消息预览 -->
        <div class="history-container">
        <!-- 工单列表 -->
        <div class="history-list" v-if="!chatStore.historyCase">
          <div
            v-for="c in chatStore.existingCases"
            :key="c.case_id"
            class="history-item"
            @click="chatStore.loadHistoryMessages(c)"
          >
            <div class="history-item-header">
              <span class="history-case-id">{{ c.case_id }}</span>
              <el-tag :type="statusType(c.status)" size="small">{{ statusLabel(c.status) }}</el-tag>
            </div>
            <div class="history-item-title">{{ c.title }}</div>
            <div class="history-item-time">{{ formatDate(c.created_at) }}</div>
          </div>
          <div v-if="chatStore.existingCases.length === 0" class="history-empty">
            暂无历史工单
          </div>
        </div>

        <!-- 消息预览 -->
        <div class="history-detail" v-else>
          <div class="history-detail-header">
            <el-button text size="small" @click="chatStore.historyCase = null; chatStore.historyMessages = []">
              ← 返回列表
            </el-button>
            <div class="history-detail-info">
              <strong>{{ chatStore.historyCase.case_id }}</strong>
              <span>{{ chatStore.historyCase.title }}</span>
              <el-tag :type="statusType(chatStore.historyCase.status)" size="small">
                {{ statusLabel(chatStore.historyCase.status) }}
              </el-tag>
            </div>
          </div>
          <div class="history-messages" v-loading="chatStore.historyLoading">
            <MessageBubble
              v-for="msg in chatStore.historyMessages"
              :key="msg.id"
              :message="msg"
            />
          </div>
          <!-- 如果是活跃工单，允许切换 -->
          <div
            v-if="chatStore.historyCase && !['closed', 'cancelled'].includes(chatStore.historyCase.status)"
            class="history-action"
          >
            <el-button type="primary" size="small" @click="chatStore.switchToCase(chatStore.historyCase!)">
              切换到此工单
            </el-button>
          </div>
        </div>
        </div>
      </div>
    </el-drawer>

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
          ref="assistantInputRef"
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

/* AI 助手选择器顶部栏（v2.2）*/
.assistant-selector-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 16px;
  background: #fff;
  border-bottom: 1px solid #e4e7ed;
  font-size: 14px;
}

.assistant-label {
  color: #606266;
  font-weight: 500;
}

.assistant-option-compact {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
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

/* 历史工单抽屉 */
.history-container {
  height: 100%;
  display: flex;
  flex-direction: column;
}

.history-list {
  flex: 1;
  overflow-y: auto;
}

.history-item {
  padding: 12px 16px;
  border-bottom: 1px solid #f0f2f5;
  cursor: pointer;
  transition: background 0.2s;
}

.history-item:hover {
  background: #f5f7fa;
}

.history-item-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 4px;
}

.history-case-id {
  font-size: 13px;
  font-weight: 600;
  color: #303133;
  font-family: 'Consolas', monospace;
}

.history-item-title {
  font-size: 14px;
  color: #606266;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  margin-bottom: 4px;
}

.history-item-time {
  font-size: 12px;
  color: #c0c4cc;
}

.history-empty {
  padding: 40px 16px;
  text-align: center;
  color: #c0c4cc;
  font-size: 14px;
}

.history-detail {
  display: flex;
  flex-direction: column;
  height: 100%;
}

.history-detail-header {
  padding: 0 0 12px 0;
  border-bottom: 1px solid #f0f2f5;
}

.history-detail-info {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-top: 8px;
}

.history-detail-info strong {
  font-size: 14px;
  color: #303133;
  font-family: 'Consolas', monospace;
}

.history-detail-info span {
  font-size: 13px;
  color: #606266;
}

.history-messages {
  flex: 1;
  overflow-y: auto;
  padding: 12px 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.history-action {
  padding: 12px 0 0;
  border-top: 1px solid #f0f2f5;
  text-align: center;
}

/* AI 助手选择器下拉选项（v2.1 增强）*/
.assistant-option {
  display: flex;
  flex-direction: column;
  gap: 4px;
  line-height: 1.4;
}

.assistant-header {
  display: flex;
  align-items: center;
  gap: 8px;
}

.assistant-name {
  font-weight: 500;
  color: #303133;
}

.assistant-desc {
  font-size: 12px;
  color: #606266;
}

.assistant-capabilities {
  display: flex;
  gap: 4px;
  margin-top: 4px;
}

.assistant-capabilities .el-tag {
  font-size: 11px;
}

.workspace-drawer :deep(.el-drawer) {
  display: flex;
  flex-direction: column;
}

.workspace-drawer :deep(.el-drawer__body) {
  flex: 1;
  overflow: hidden;
  padding: 12px 16px;
}

.workspace-drawer :deep(.el-drawer__header) {
  margin-bottom: 0;
  padding: 16px;
  border-bottom: 1px solid #f0f2f5;
}

.drawer-body-content {
  height: 100%;
}

/* 拖拽调整手柄 */
.drawer-resize-handle {
  position: absolute;
  top: 0;
  bottom: 0;
  width: 8px;
  cursor: col-resize;
  z-index: 100;
  transition: background-color 0.2s;
}
.drawer-resize-handle:hover,
.drawer-resize-handle:active {
  background-color: rgba(64, 158, 255, 0.2);
}
.drawer-resize-left {
  left: 0;
}
.drawer-resize-right {
  right: 0;
}

.terminal-content {
  display: flex;
  flex-direction: column;
}

.drawer-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
}

.drawer-title {
  font-size: 15px;
  font-weight: 600;
  color: #303133;
}

.drawer-actions {
  display: inline-flex;
  align-items: center;
  gap: 2px;
}

</style>

<style>
/* 全局透传点击事件：当钉住时，取消全屏遮罩及背景色 */
.drawer-unblocked-overlay {
  pointer-events: none !important;
  background-color: transparent !important;
}
.drawer-unblocked-overlay .el-drawer {
  pointer-events: auto !important;
  box-shadow: 0 4px 24px rgba(0, 0, 0, 0.1);
}
</style>
