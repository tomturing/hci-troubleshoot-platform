<template>
  <!-- ops-agent 交互卡片：SOP 操作卡 / 信息确认卡 -->
  <el-dialog
    v-model="visible"
    :title="cardTitle"
    width="560px"
    :close-on-click-modal="false"
    :close-on-press-escape="false"
    :show-close="false"
    class="interactive-card"
  >
    <!-- SOP 操作卡：展示路径、目标、操作指引 -->
    <template v-if="props.event.kind === 'sop_step'">
      <div v-if="meta.route" class="field-block route-block">
        <span class="field-label">当前路径</span>
        <code class="route-code">{{ meta.route }}</code>
      </div>

      <div class="sop-grid">
        <div v-if="meta.operationGoal" class="field-block">
          <span class="field-label">操作目标</span>
          <p>{{ meta.operationGoal }}</p>
        </div>
        <div v-if="meta.expectedResult" class="field-block">
          <span class="field-label">预期结果</span>
          <p>{{ meta.expectedResult }}</p>
        </div>
      </div>

      <div v-if="meta.executionGuidance" class="field-block">
        <span class="field-label">操作指引</span>
        <p class="guidance-text">{{ meta.executionGuidance }}</p>
      </div>

      <div v-if="feedbackPrompt" class="field-block">
        <span class="field-label">请反馈</span>
        <p>{{ feedbackPrompt }}</p>
      </div>

      <el-alert
        v-if="meta.riskNotice"
        type="warning"
        :title="meta.riskNotice"
        show-icon
        :closable="false"
        class="mb-3"
      />
    </template>

    <!-- 信息确认卡：展示核心问题 + 背景说明 -->
    <template v-else>
      <div class="field-block question-block">
        <span class="field-label">核心问题</span>
        <p class="question-text">{{ meta.question || props.event.prompt }}</p>
      </div>

      <div v-if="meta.context" class="field-block">
        <span class="field-label">背景说明</span>
        <p>{{ meta.context }}</p>
      </div>

      <el-alert
        v-if="meta.riskNotice"
        type="warning"
        :title="meta.riskNotice"
        show-icon
        :closable="false"
        class="mb-3"
      />
    </template>

    <!-- 选项列表 -->
    <div v-if="props.event.options?.length" class="options-section">
      <span class="field-label">可选回复</span>
      <div class="options-list">
        <el-button
          v-for="opt in props.event.options"
          :key="opt.optionId"
          size="default"
          @click="submitSelected(opt.optionId, opt.name)"
          :loading="submitting"
        >
          <span class="option-id">{{ opt.optionId }}.</span> {{ opt.name }}
        </el-button>
      </div>
    </div>

    <!-- 自由文本输入 -->
    <div v-if="props.event.customInput" class="custom-input-section">
      <span class="field-label">补充信息（可选）</span>
      <el-input
        v-model="customText"
        type="textarea"
        :rows="3"
        placeholder="输入更准确的现场信息或执行结果。"
        :disabled="submitting"
      />
      <el-button
        class="mt-2"
        :disabled="!customText.trim() || submitting"
        :loading="submitting"
        @click="submitFreeText"
      >
        提交补充信息
      </el-button>
    </div>
  </el-dialog>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'

/** metadata 字段的带可选字段接口（避免 strict TS 下 unknown 赋值错误） */
export interface InteractiveRequestMetadata {
  feedbackRequest?: string
  route?: string
  operationGoal?: string
  [key: string]: unknown
}

/** interactive_request SSE 事件的原始 payload 结构（由后端 conversation_service yield） */
export interface InteractiveRequestEvent {
  requestId: string
  acpSessionId: string
  kind: 'sop_step' | 'info_request' | string
  title: string
  prompt: string
  options: Array<{ optionId: string; name: string }>
  customInput: boolean
  metadata: InteractiveRequestMetadata
}

const props = defineProps<{
  event: InteractiveRequestEvent
  conversationId: string
}>()

const emit = defineEmits<{
  /** 用户完成响应（不管是选项还是自由文本），父组件清除 pending state */
  submitted: []
}>()

const visible = ref(true)
const submitting = ref(false)
const customText = ref('')

/** _meta 展平字段（来自 BrainInteractiveRequest.metadata，含 route / operationGoal 等） */
const meta = computed(() => props.event.metadata ?? {})

const cardTitle = computed(() =>
  props.event.kind === 'sop_step' ? '📋 SOP 操作步骤确认' : '❓ 信息确认'
)

/** SOP 卡的用户反馈引导文本：优先取 metadata.feedbackRequest，其次 prompt */
const feedbackPrompt = computed(() =>
  meta.value.feedbackRequest || props.event.prompt || ''
)

async function submitSelected(optionId: string, optionName: string) {
  await doSubmit(
    { outcome: 'selected', optionId },
    optionName,
  )
}

async function submitFreeText() {
  const text = customText.value.trim()
  if (!text) return
  await doSubmit(
    { outcome: 'free_text', text },
    text,
  )
}

async function doSubmit(outcome: Record<string, string>, _visibleReply: string) {
  submitting.value = true
  try {
    const resp = await fetch(`/api/conversations/${props.conversationId}/interactive-response`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        request_id: props.event.requestId,
        acp_session_id: props.event.acpSessionId,
        outcome,
      }),
    })
    if (!resp.ok) {
      const errBody = await resp.json().catch(() => ({}))
      console.warn('[InteractiveRequestCard] 提交失败:', resp.status, errBody)
      // 保留卡片，允许用户重试
      return
    }
    // 仅在成功时关闭卡片
    visible.value = false
    emit('submitted')
  } catch (e) {
    console.warn('[InteractiveRequestCard] 提交请求异常（网络错误）:', e)
    // 网络错误也保留卡片，允许用户重试
  } finally {
    submitting.value = false
  }
}
</script>

<style scoped>
.interactive-card :deep(.el-dialog__body) {
  padding-top: 8px;
}

.field-block {
  margin-bottom: 14px;
}

.field-label {
  display: block;
  font-size: 12px;
  font-weight: 600;
  color: #6b7280;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  margin-bottom: 4px;
}

.route-code {
  display: block;
  background: #f3f4f6;
  border: 1px solid #e5e7eb;
  border-radius: 4px;
  padding: 4px 10px;
  font-size: 13px;
  color: #374151;
}

.sop-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  margin-bottom: 14px;
}

.guidance-text,
.question-text {
  line-height: 1.6;
  color: #1f2937;
}

.options-section {
  margin-top: 16px;
}

.options-list {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 6px;
}

.option-id {
  font-weight: 600;
  margin-right: 2px;
  color: #6366f1;
}

.custom-input-section {
  margin-top: 16px;
}
</style>
