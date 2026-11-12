<script setup lang="ts">
/**
 * 日志收集失败诊断卡：结论 + 规范码 + 原因 + 如何检查 + 如何处理。
 *
 * 为什么单独抽组件：失败可能出现在「进行中的表单态」与「结果态」两处，
 * 两处各写一份模板必然会漂移（一处补了新码，另一处还是旧文案）。
 * 这里只吃 error_code 与原始原因，所有建议来自 logCollectErrorHints 的唯一映射表。
 */
import { computed } from 'vue'
import { logCollectErrorHint } from '@/services/logCollectErrorHints'

const props = defineProps<{
  /** 规范错误码（LC-*），为空时走「无码」兜底 */
  code?: string | null
  /** 平台/节点给出的原始原因，原样展示，不做二次概括 */
  reason?: string | null
}>()

const hint = computed(() => logCollectErrorHint(props.code))
const code = computed(() => String(props.code || '').trim())
const reason = computed(() => String(props.reason || '').trim())
</script>

<template>
  <div class="lc-diagnosis">
    <div class="lc-diagnosis-head">
      <span class="lc-diagnosis-title">{{ hint?.title || '任务失败' }}</span>
      <el-tag v-if="code" size="small" effect="plain" class="lc-diagnosis-code">
        {{ code }}
      </el-tag>
    </div>
    <p v-if="reason" class="lc-diagnosis-reason">{{ reason }}</p>
    <template v-if="hint">
      <div class="lc-diagnosis-block">
        <strong>如何检查</strong>
        <ol>
          <li v-for="(item, index) in hint.check" :key="`check-${index}`">{{ item }}</li>
        </ol>
      </div>
      <div class="lc-diagnosis-block">
        <strong>如何处理</strong>
        <ol>
          <li v-for="(item, index) in hint.fix" :key="`fix-${index}`">{{ item }}</li>
        </ol>
      </div>
    </template>
    <p v-else class="lc-diagnosis-fallback">
      平台未返回可识别的错误码。请在任务详情中查看 error_message 与 create_output，
      并记录任务号与时间后提交支持；不要反复重试，避免覆盖现场。
    </p>
  </div>
</template>

<style scoped>
/* 与状态行分开：原因与建议不能被当成进度文案一扫而过 */
.lc-diagnosis {
  padding: 12px;
  border: 1px solid var(--el-color-danger-light-5);
  border-radius: 6px;
  background: var(--el-color-danger-light-9);
}
.lc-diagnosis-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.lc-diagnosis-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--el-color-danger);
}
.lc-diagnosis-code {
  font-family: var(--el-font-family-mono, monospace);
}
.lc-diagnosis-reason {
  margin: 8px 0 0;
  font-size: 13px;
  color: var(--el-text-color-regular);
  white-space: pre-wrap;
}
.lc-diagnosis-block {
  margin-top: 10px;
  font-size: 13px;
  color: var(--el-text-color-regular);
}
.lc-diagnosis-block strong {
  color: var(--el-text-color-primary);
}
.lc-diagnosis-block ol {
  margin: 4px 0 0;
  padding-left: 20px;
}
.lc-diagnosis-block li {
  margin-top: 2px;
}
.lc-diagnosis-fallback {
  margin: 8px 0 0;
  font-size: 13px;
  color: var(--el-text-color-regular);
}
</style>