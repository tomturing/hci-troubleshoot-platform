<script setup lang="ts">
import { ref, nextTick, watch, onMounted } from 'vue'
import { useChatStore } from '@/stores/chat'
import MessageBubble from './MessageBubble.vue'
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

function handleQuoteToChat(content: string) {
  // 设置助手输入框内容
  chatStore.setAssistantDraftText(`请分析以下终端操作记录：\n${content}`)
  // 关闭终端回放弹窗（状态在 store，右栏终端与历史工单都能触发）
  chatStore.closeTerminalReplay()
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

    <!-- 终端历史回放弹窗：状态放 store，右栏终端与左侧历史工单都能触发 -->
    <el-dialog
      :model-value="chatStore.showTerminalReplay"
      title="终端历史回放"
      width="80%"
      top="5vh"
      :close-on-click-modal="false"
      class="terminal-replay-dialog"
      @close="chatStore.closeTerminalReplay()"
    >
      <TerminalReplay
        v-if="chatStore.terminalReplayCaseId"
        :case-id="chatStore.terminalReplayCaseId"
        @quote-to-chat="handleQuoteToChat"
      />
    </el-dialog>

    <!--
      底部操作区：结构与左右面板完全一致（hint 行 + 36px 控件行 + 按钮组），
      规范在 styles/panel-composer.css。三块面板并排时底边、输入框、按钮必须齐平。
    -->
    <div class="panel-composer">
      <div class="panel-composer__hint">
        <template v-if="chatStore.currentCase && !chatStore.hasActiveCase">
          当前工单已关闭，新建工单后可继续对话
        </template>
        <template v-else-if="chatStore.currentCase">
          输入 <code>/close</code> 关闭当前工单
        </template>
        <template v-else>描述您遇到的问题，我会帮您创建工单</template>
      </div>
      <div class="panel-composer__body">
        <el-input
          ref="assistantInputRef"
          v-model="inputText"
          class="panel-composer__field"
          type="textarea"
          :rows="1"
          :placeholder="chatStore.isCaseClosed ? '工单已关闭，请点击「新建工单」' : '描述您遇到的问题...'"
          :disabled="chatStore.isStreaming || chatStore.isCaseClosed"
          @keydown="handleKeydown"
        />
        <div class="panel-composer__actions">
          <el-button
            v-if="chatStore.currentCase && !chatStore.hasActiveCase"
            type="primary"
            @click="chatStore.startNewConversation()"
          >
            新建工单
          </el-button>
          <el-button
            v-else
            type="primary"
            :loading="chatStore.isStreaming"
            :disabled="!inputText.trim() || chatStore.isStreaming"
            @click="handleSend"
          >
            发送
          </el-button>
        </div>
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
  /* 中央是一列内容，块级子项统一居中到同一条内容列 */
  align-items: center;
}

/*
 * 内容列：引导卡 / 阶段条 / 环境卡 / 告警条与消息气泡共用 860。
 *
 * 早先这些块各自通栏（1352）、气泡却 860 居中，同一屏上宽度五花八门，
 * 视觉上像几块互不相干的板子拼在一起。限宽放在子项上而不是外壳上，
 * 外壳仍是通栏灰底，不会出现"两侧露页面底色"的空白带。
 *
 * 必须排除 .el-overlay：el-dialog 不开 append-to-body 时是「就地渲染」，
 * 遮罩会成为 .chat-window 的直接子元素（组件根元素会继承本组件的 scoped
 * 属性，所以 `> *` 必然命中它）。遮罩一旦被限宽就只剩 860px——1920 屏上
 * 表现为「左半屏变暗、右半屏照常」，看起来像渲染坏了一半。
 */
.chat-window > :not(.el-overlay) {
  width: 100%;
  max-width: 860px;
}

/* 消息区自身要滚动、吃掉剩余高度，限宽交给内部气泡 */
.chat-window > .messages-area {
  flex: 1;
  max-width: none;
  min-height: 0;
}

/* 底部操作区是通栏底条，内部控件行自己限宽到 860 */
.chat-window > .panel-composer {
  max-width: none;
}

.messages-area {
  flex: 1;
  overflow-y: auto;
  padding: 16px;
  display: flex;
  flex-direction: column;
  /* 背景铺满整个中央区，消息本身限宽居中：留白在气泡两侧而不是「两块白色空白带」 */
  align-items: center;
  gap: 12px;
}

/*
 * 单条消息/卡片限宽。中央区现在会随抽屉让位而变化（900 → 460），
 * 光靠容器限宽在宽屏上会留下大片空白；限宽放在内容上，
 * 宽屏保持可读行长，窄屏（<860px）自动退化为自适应。
 *
 * 选择器必须带到 .message-bubble：MessageBubble 自身声明了 max-width:100%，
 * 同权重的 `> *` 会被它按加载顺序压掉，消息又会拉满整行。
 */
.messages-area > * {
  width: 100%;
}

.messages-area > .message-bubble {
  max-width: 860px;
}

/* 系统提示按文字宽度收窄：一句话撑满 860 宽的空灰条会显得整块内容没对齐 */
.messages-area > .message-bubble.is-system {
  width: fit-content;
}

/* 分隔线是"整行"语义，必须保持通栏，上面的 fit-content 不能作用到它 */
.messages-area > .message-bubble.is-divider {
  width: 100%;
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

/*
 * 底部操作区的结构、高度、字号、按钮规格统一在 styles/panel-composer.css，
 * 这里只保留「与中央内容列同宽」这一条布局规则（其他面板是通栏，不需要）。
 */
.panel-composer .panel-composer__body,
.panel-composer .panel-composer__hint {
  width: 100%;
  max-width: min(var(--ws-center-max, 900px), 860px);
  margin-left: auto;
  margin-right: auto;
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

