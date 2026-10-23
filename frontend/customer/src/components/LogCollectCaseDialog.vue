<script setup lang="ts">
/**
 * 人工指定工单号（日志收集）
 *
 * 为什么需要：
 * - 日志任务必须挂在平台工单上（`log_collect_task.case_id` 是必填外键），而入口已改为
 *   常驻，客户在"无活跃工单"（刚进页面 / 工单已关闭）时也需要能补采日志。
 * - 归属边界不变：只允许当前客户端名下工单，校验完全交给服务端
 *   `GET /api/cases/{case_id}`（`verify_case_ownership` 对不存在/非归属统一 404）。
 *   前端不做存在性判断，避免把这里变成枚举他人工单的接口。
 */
import { ref, watch } from 'vue'
import { useChatStore } from '@/stores/chat'

const chatStore = useChatStore()

const caseId = ref('')
const errorMessage = ref('')

// 与服务端 case_id 长度上限一致（LogCollectTaskCreate.case_id max_length=20）
const CASE_ID_PATTERN = /^[A-Za-z0-9_-]{1,20}$/

watch(
  () => chatStore.logCollectCaseDialogVisible,
  (open) => {
    if (!open) return
    caseId.value = chatStore.logCollectCaseId ?? ''
    errorMessage.value = ''
  },
)

async function submit() {
  errorMessage.value = ''
  const value = caseId.value.trim()
  if (!CASE_ID_PATTERN.test(value)) {
    errorMessage.value = '工单号格式不正确（字母/数字/下划线/短横线，最长 20 位）'
    return
  }
  try {
    await chatStore.confirmLogCollectCase(value)
  } catch (e: any) {
    errorMessage.value = e?.message || '工单校验失败'
  }
}
</script>

<template>
  <el-dialog
    :model-value="chatStore.logCollectCaseDialogVisible"
    title="指定工单号"
    width="440px"
    :close-on-click-modal="false"
    @close="chatStore.closeLogCollectCaseDialog()"
  >
    <p class="lc-case-tip">
      日志包必须归档到平台工单。请输入<b>当前客户端名下</b>的工单号（如 Q2026102300001）。
    </p>
    <el-input
      v-model="caseId"
      placeholder="Q2026102300001"
      maxlength="20"
      clearable
      @keyup.enter="submit"
    />
    <p v-if="errorMessage" class="lc-case-error">{{ errorMessage }}</p>
    <p class="lc-case-hint">
      换设备或清理过浏览器缓存时，旧工单可能不属于当前客户端，需要在原浏览器中操作。
      已结单工单可以指定，日志将归档但不改变工单状态。
    </p>
    <template #footer>
      <el-button @click="chatStore.closeLogCollectCaseDialog()">取消</el-button>
      <el-button type="primary" :loading="chatStore.logCollectCaseLoading" @click="submit">
        确定
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.lc-case-tip {
  margin: 0 0 12px;
  font-size: 13px;
  line-height: 1.6;
  color: #303133;
}

.lc-case-error {
  margin: 8px 0 0;
  font-size: 12px;
  color: #f56c6c;
}

.lc-case-hint {
  margin: 12px 0 0;
  font-size: 12px;
  line-height: 1.6;
  color: #909399;
}
</style>
