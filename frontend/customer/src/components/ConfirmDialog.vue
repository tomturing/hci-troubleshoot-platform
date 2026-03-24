<template>
  <!-- 高风险操作确认弹窗 -->
  <el-dialog
    v-model="visible"
    :title="riskTitle"
    width="490px"
    :close-on-click-modal="false"
    :close-on-press-escape="false"
    :show-close="false"
    class="confirm-dialog"
  >
    <!-- 风险等级警告条 -->
    <el-alert
      :type="riskLevel === 3 ? 'error' : 'warning'"
      :title="props.event.risk_description"
      show-icon
      :closable="false"
      class="mb-4"
    />

    <!-- 工具调用详情 -->
    <div class="tool-detail rounded-md bg-gray-50 p-3 mt-3">
      <p class="text-sm font-semibold text-gray-700 mb-1">
        操作：<code class="text-orange-600">{{ props.event.tool_name }}</code>
      </p>
      <pre class="text-xs text-gray-600 whitespace-pre-wrap break-all mt-1">{{ formattedArgs }}</pre>
    </div>

    <!-- 倒计时 -->
    <div class="mt-3 text-right text-sm text-gray-500">
      自动取消倒计时：
      <span :class="countdown < 30 ? 'text-red-600 font-bold' : 'text-gray-700'">
        {{ countdown }}s
      </span>
    </div>

    <!-- 高危需额外勾选确认复选框 -->
    <el-checkbox
      v-if="riskLevel === 3"
      v-model="acknowledged"
      class="mt-3 text-sm"
    >
      我已充分了解此操作的风险，确认执行
    </el-checkbox>

    <template #footer>
      <div class="dialog-footer flex justify-end gap-2">
        <el-button @click="handleCancel">取消</el-button>
        <el-button
          :type="riskLevel === 3 ? 'danger' : 'warning'"
          :disabled="riskLevel === 3 && !acknowledged"
          @click="handleConfirm"
        >
          确认执行
        </el-button>
      </div>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { ref, computed, onBeforeUnmount } from 'vue'

/** confirm_request SSE 事件结构 */
export interface ConfirmRequestEvent {
  type: 'confirm_request'
  tool_name: string
  tool_args: Record<string, unknown>
  risk_level: 2 | 3
  risk_description: string
  timeout_seconds: number
}

const props = defineProps<{
  event: ConfirmRequestEvent
  sessionId: string
}>()

const emit = defineEmits<{
  /** 用户操作完成：true=确认执行，false=取消 */
  confirmed: [authorized: boolean]
}>()

const visible = ref(true)
const acknowledged = ref(false)
const riskLevel = computed(() => props.event.risk_level)
const countdown = ref(props.event.timeout_seconds)

const riskTitle = computed(() =>
  riskLevel.value === 3 ? '⛔ 高危操作确认' : '⚠️ 高风险操作确认'
)

const formattedArgs = computed(() =>
  JSON.stringify(props.event.tool_args, null, 2)
)

// 倒计时定时器
const timer = setInterval(() => {
  countdown.value--
  if (countdown.value <= 0) {
    handleCancel()
  }
}, 1000)

onBeforeUnmount(() => clearInterval(timer))

function handleConfirm() {
  clearInterval(timer)
  visible.value = false
  emit('confirmed', true)
}

function handleCancel() {
  clearInterval(timer)
  visible.value = false
  emit('confirmed', false)
}
</script>

<style scoped>
.confirm-dialog .tool-detail {
  border: 1px solid #e5e7eb;
}
</style>
