<script setup lang="ts">
import { ref, computed, nextTick, watch, onMounted } from 'vue'
import { useChatStore } from '@/stores/chat'
import MessageBubble from './MessageBubble.vue'
import TerminalPanel from './TerminalPanel.vue'
import TerminalReplay from './TerminalReplay.vue'
import ConfirmDialog from './ConfirmDialog.vue'
import InteractiveRequestCard from './InteractiveRequestCard.vue'
import DiagnosticProgress from './DiagnosticProgress.vue'
import CaseCreateDialog from './CaseCreateDialog.vue'
import BridgeLogUploader from './BridgeLogUploader.vue'
import LogCollectEntryCard from './LogCollectEntryCard.vue'
import EnvironmentSummary from './EnvironmentSummary.vue'

const chatStore = useChatStore()
const inputText = ref('')
const messagesContainer = ref<HTMLElement | null>(null)
const assistantInputRef = ref<{ focus: () => void } | null>(null)
const showTerminalReplayDialog = ref(false)
const terminalReplayCaseId = ref<string | null>(null)

/**
 * 只有降级为覆盖态才需要遮罩。
 *
 * 推挤态下抽屉与主区左右分列、互不遮挡，再盖一层半透明遮罩只会
 * 让"一边看 AI 建议一边敲命令"这个核心场景变得不可点。
 */
const isOverlayMode = computed(() => chatStore.workspaceLayout.mode === 'overlay')

/**
 * 拖拽调宽：只写"偏好宽度"，由求解器重新算当前宽度。
 *
 * 直接写当前宽度会在窗口变窄后留下一个越界固定值，把中央会话挤到不可读；
 * 写偏好则始终能被 clamp 回合法区间。
 */
function startResize(side: 'left' | 'right', e: MouseEvent) {
  e.preventDefault()
  const startX = e.clientX
  const startWidth = side === 'left' ? chatStore.leftPanelWidth : chatStore.rightPanelWidth
  const apply = side === 'left' ? chatStore.setLeftPanelPreferred : chatStore.setRightPanelPreferred
  chatStore.setPanelResizing(true)
  const onMouseUp = () => {
    chatStore.setPanelResizing(false)
    document.removeEventListener('mousemove', onMouseMove)
    document.removeEventListener('mouseup', onMouseUp)
  }
  const onMouseMove = (moveE: MouseEvent) => {
    // 鼠标在窗口外松开时收不到 mouseup，靠按键状态兜底复位，否则过渡会被永久关闭
    if (moveE.buttons === 0) {
      onMouseUp()
      return
    }
    // 左侧手柄往右拖是变宽，右侧手柄往左拖是变宽
    const delta = side === 'left' ? moveE.clientX - startX : startX - moveE.clientX
    apply(startWidth + delta)
  }
  document.addEventListener('mousemove', onMouseMove)
  document.addEventListener('mouseup', onMouseUp)
}

const startResizeTerminal = (e: MouseEvent) => startResize('right', e)
const startResizeHistory = (e: MouseEvent) => startResize('left', e)

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

/** 打开终端历史回放 */
function openTerminalReplay() {
  if (chatStore.historyCase) {
    terminalReplayCaseId.value = chatStore.historyCase.case_id
    showTerminalReplayDialog.value = true
  }
}

/** 从 TerminalPanel 打开当前工单的终端历史回放 */
function openCurrentCaseTerminalHistory() {
  // 优先当前聊天工单，其次日志收集绑定的工单：无聊天工单时用户也能看采集会话的回放
  const caseId = chatStore.currentCase?.case_id ?? chatStore.logCollectCaseId ?? null
  if (caseId) {
    terminalReplayCaseId.value = caseId
    showTerminalReplayDialog.value = true
  }
}

function closeTerminalReplay() {
  showTerminalReplayDialog.value = false
  terminalReplayCaseId.value = null
}

/** 处理从终端回放引用内容到聊天 */
function handleQuoteToChat(content: string) {
  // 设置助手输入框内容
  chatStore.setAssistantDraftText(`请分析以下终端操作记录：\n${content}`)
  // 关闭终端回放弹窗
  closeTerminalReplay()
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

    <!-- T-E7: ops-agent 交互请求（已改为对话气泡，弹窗停用） -->
    <!-- <InteractiveRequestCard
      v-if="chatStore.pendingInteractive"
      :event="chatStore.pendingInteractive"
      :conversation-id="String(chatStore.conversationId ?? '')"
      @submitted="chatStore.clearInteractiveRequest()"
    /> -->

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
    <CaseCreateDialog :bridge-status="chatStore.caseCreateDialogBridgeStatus" />

    <!-- SSE 网络中断可重试提示（长耗时诊断期间连接被代理掐断时） -->
    <el-alert
      v-if="chatStore.sseNetworkError"
      type="warning"
      show-icon
      :closable="false"
      class="sse-network-error-banner"
    >
      <template #title>诊断连接中断（网络错误）</template>
      <template #default>
        <span>{{ chatStore.sseNetworkError.message }}，可点击重试。</span>
        <el-button type="primary" size="small" link @click="chatStore.retryLastMessage()">
          点击重试
        </el-button>
      </template>
    </el-alert>

    <!-- 日志收集引导（建单后一次性出现；关闭后仍可从 header 入口进入） -->
    <LogCollectEntryCard />

    <!-- 本地 bridge 日志补采入口（诊断未完成时主动提醒） -->
    <BridgeLogUploader />

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
    </div>

    <!-- SSH 终端抽屉 -->
    <el-drawer
      v-model="chatStore.showTerminalSidebar"
      direction="rtl"
      :size="`${chatStore.rightPanelWidth}px`"
      :append-to-body="true"
      :modal="true"
      :lock-scroll="isOverlayMode"
      :close-on-click-modal="isOverlayMode"
      :close-on-press-escape="true"
      :with-header="true"
      :show-close="false"
      :modal-class="isOverlayMode ? '' : 'drawer-push-overlay'"
      class="workspace-drawer"
      @close="chatStore.closeTerminalSidebar()"
    >
      <!-- 覆盖态宽度由降级规则决定，拖拽无效，手柄不出现以免误导 -->
      <div
        v-if="!isOverlayMode"
        class="drawer-resize-handle drawer-resize-left"
        @mousedown="startResizeTerminal"
      ></div>
      <template #header>
        <div class="drawer-header">
          <span class="drawer-title">SSH 终端</span>
          <div class="drawer-actions">
            <el-button text size="small" @click="chatStore.closeTerminalSidebar()">关闭</el-button>
          </div>
        </div>
      </template>
      <div class="drawer-body-content terminal-content">
        <TerminalPanel @open-terminal-history="openCurrentCaseTerminalHistory" />
      </div>
    </el-drawer>

    <!-- 历史工单抽屉 -->
    <el-drawer
      v-model="chatStore.showHistoryDrawer"
      direction="ltr"
      :size="`${chatStore.leftPanelWidth}px`"
      :append-to-body="true"
      :modal="true"
      :lock-scroll="isOverlayMode"
      :close-on-click-modal="isOverlayMode"
      :close-on-press-escape="true"
      :with-header="true"
      :show-close="false"
      :modal-class="isOverlayMode ? '' : 'drawer-push-overlay'"
      class="workspace-drawer"
      @close="chatStore.closeHistoryDrawer()"
    >
      <div
        v-if="!isOverlayMode"
        class="drawer-resize-handle drawer-resize-right"
        @mousedown="startResizeHistory"
      ></div>
      <template #header>
        <div class="drawer-header">
          <span class="drawer-title">历史工单</span>
          <div class="drawer-actions">
            <el-button text size="small" @click="chatStore.closeHistoryDrawer()">关闭</el-button>
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
            <el-button size="small" type="primary" plain @click="openTerminalReplay">
              终端历史
            </el-button>
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

    <!-- 终端历史回放弹窗 -->
    <el-dialog
      v-model="showTerminalReplayDialog"
      title="终端历史回放"
      width="80%"
      top="5vh"
      :close-on-click-modal="false"
      class="terminal-replay-dialog"
      @close="closeTerminalReplay"
    >
      <TerminalReplay
        v-if="terminalReplayCaseId"
        :case-id="terminalReplayCaseId"
        @quote-to-chat="handleQuoteToChat"
      />
    </el-dialog>

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
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 0 0 12px 0;
  border-bottom: 1px solid #f0f2f5;
}

.history-detail-header .el-button--primary {
  margin-top: 4px;
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

/* 终端面板内部布局（其余抽屉外观已抽到 styles/workspace-drawer.css） */
.terminal-content {
  display: flex;
  flex-direction: column;
}

/* 终端回放弹窗样式 */
.terminal-replay-dialog :deep(.el-dialog__body) {
  padding: 12px;
  max-height: 80vh;
  overflow: hidden;
}

.terminal-replay-dialog :deep(.el-dialog) {
  max-width: 1200px;
}

</style>

