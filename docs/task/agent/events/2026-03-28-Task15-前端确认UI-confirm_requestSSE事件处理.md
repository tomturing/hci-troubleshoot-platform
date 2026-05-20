---
status: active
category: task
audience: developer
last_updated: 2026-03-28
owner: team
related: 15
---

# Task 15：前端确认 UI——confirm_request SSE 事件处理（P1）

```
你是一名负责 hci-troubleshoot-platform 前端对话体验的 agent。

【仓库】
git clone https://github.com/tomturing/hci-troubleshoot-platform.git
cd hci-troubleshoot-platform

【背景】
当 AI Agent 需要执行高风险操作时，会通过 SSE 推送 confirm_request 事件。
前端需要：
  1. 捕获 confirm_request 事件
  2. 在对话界面显示风险确认弹窗（含工具名称、参数、风险描述）
  3. 用户确认→调用 POST /confirm（confirmed: true）
  4. 用户取消→调用 POST /confirm（confirmed: false）
  
风险等级配色（对应 risk_level）：
  level 1（只读）：不显示弹窗，仅在消息流中显示"正在查询..."提示
  level 2（变更）：黄色警告弹窗（⚠️）
  level 3（高危）：红色危险弹窗（🔴）—— 但 block 策略下不应出现此 SSE 事件

前置条件：Task 12（人工确认机制后端完成）

【任务目标】
1. 在 customer 前端处理 confirm_request SSE 事件
2. 实现 ConfirmDialog 组件（风险弹窗）
3. 用户交互后调用 POST /confirm 接口
4. 处理等待中的 UI 状态（倒计时 120 秒）
5. 处理 tool_executing 事件（显示"正在执行..."状态）

【涉及服务 / 文件范围】
允许新建/修改：
  - frontend/customer/src/components/ConfirmDialog.vue（新建）
  - frontend/customer/src/composables/useConversation.ts（修改：处理新 SSE 事件）
  - frontend/customer/src/views/ConversationView.vue（修改：集成 ConfirmDialog）
  - frontend/customer/src/api/conversations.ts（修改：新增 PostConfirm 方法）
只读参考：
  - frontend/customer/src/（现有组件和 API 客户端，了解现有模式）
  - frontend/shared/src/types/（共享类型定义）
禁止：
  - 修改 admin/ 前端代码
  - 修改 frontend/shared/src/ 中已有的类型（只可新增）

【详细实现步骤】

Step 1：新增 SSE 事件类型定义

在 frontend/shared/src/types/sse.ts（如不存在则新建）：

```typescript
// 新增事件类型（不修改现有类型）
export interface ConfirmRequestEvent {
  type: 'confirm_request'
  tool_name: string
  tool_args: Record<string, unknown>
  risk_level: 2 | 3
  risk_description: string
  timeout_seconds: number
}

export interface ToolExecutingEvent {
  type: 'tool_executing'
  tool: string
  args: Record<string, unknown>
}

export interface ThinkingEvent {
  type: 'thinking'
  step: number
  message: string
}
```

Step 2：实现 ConfirmDialog 组件

```vue
<!-- frontend/customer/src/components/ConfirmDialog.vue -->
<template>
  <el-dialog
    v-model="visible"
    :title="riskTitle"
    width="480px"
    :close-on-click-modal="false"
    :close-on-press-escape="false"
  >
    <!-- 风险等级警告 -->
    <el-alert
      :type="riskLevel === 3 ? 'error' : 'warning'"
      :title="event.risk_description"
      show-icon
      :closable="false"
      class="mb-4"
    />

    <!-- 工具调用详情 -->
    <div class="tool-detail">
      <p class="text-sm font-medium text-gray-700">操作：{{ event.tool_name }}</p>
      <pre class="mt-2 bg-gray-50 p-3 rounded text-xs">{{ formattedArgs }}</pre>
    </div>

    <!-- 倒计时 -->
    <div class="mt-3 text-right text-sm text-gray-500">
      自动取消倒计时：<span :class="countdown < 30 ? 'text-red-600 font-bold' : ''">
        {{ countdown }}s
      </span>
    </div>

    <!-- 高危确认复选框 -->
    <el-checkbox v-if="riskLevel === 3" v-model="acknowledged" class="mt-3">
      我已了解此操作的风险，并确认执行
    </el-checkbox>

    <template #footer>
      <el-button @click="handleCancel">取消</el-button>
      <el-button
        :type="riskLevel === 3 ? 'danger' : 'warning'"
        :disabled="riskLevel === 3 && !acknowledged"
        @click="handleConfirm"
      >
        确认执行
      </el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { ref, computed, onBeforeUnmount } from 'vue'
import type { ConfirmRequestEvent } from '@shared/types/sse'

const props = defineProps<{
  event: ConfirmRequestEvent
  sessionId: string
}>()
const emit = defineEmits<{
  confirmed: [authorized: boolean]
}>()

const visible = ref(true)
const acknowledged = ref(false)
const countdown = ref(props.event.timeout_seconds)
const riskLevel = computed(() => props.event.risk_level)
const riskTitle = computed(() =>
  riskLevel.value === 3 ? '⛔ 高危操作确认' : '⚠️ 高风险操作确认'
)
const formattedArgs = computed(() =>
  JSON.stringify(props.event.tool_args, null, 2)
)

// 倒计时
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
```

Step 3：修改 useConversation.ts，处理新事件

在 SSE 消息处理 switch 中新增：

```typescript
case 'confirm_request':
  pendingConfirm.value = event as ConfirmRequestEvent
  break

case 'tool_executing':
  // 在消息流中显示工具执行状态
  appendToolStatus(`正在查询：${(event as ToolExecutingEvent).tool}...`)
  break

case 'thinking':
  appendToolStatus(`🤔 推理步骤 ${(event as ThinkingEvent).step}：${(event as ThinkingEvent).message}`)
  break
```

新增 API 方法（conversations.ts）：
```typescript
export async function postConfirm(
  sessionId: string,
  confirmed: boolean,
  authorizedBy: string
): Promise<void> {
  await apiClient.post(`/api/v1/conversations/${sessionId}/confirm`, {
    confirmed,
    authorized_by: authorizedBy,
  })
}
```

Step 4：在 ConversationView.vue 集成 ConfirmDialog

```vue
<!-- 在模板中添加 -->
<ConfirmDialog
  v-if="pendingConfirm"
  :event="pendingConfirm"
  :session-id="sessionId"
  @confirmed="handleConfirmResult"
/>
```

```typescript
async function handleConfirmResult(authorized: boolean) {
  if (!pendingConfirm.value) return
  await postConfirm(sessionId, authorized, currentUserId.value)
  pendingConfirm.value = null
}
```

【约束】
- risk_level=3（高危）弹窗必须需要勾选"已了解风险"复选框才能确认
- 倒计时结束后自动调用取消，不可被用户阻止
- 弹窗显示期间，不可关闭（close-on-click-modal=false）

【验收标准】
- [ ] SSE 收到 confirm_request 时，对话界面显示弹窗
- [ ] risk_level=2：黄色⚠️ 弹窗，直接点确认即可
- [ ] risk_level=3：红色⛔ 弹窗，需勾选复选框
- [ ] 倒计时 120 秒后自动取消（调用 POST /confirm confirmed=false）
- [ ] 用户确认后，AI 继续回复（SSE 流继续）
- [ ] pnpm --filter customer build 无新增编译错误
```

---