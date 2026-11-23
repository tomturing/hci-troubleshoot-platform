<script setup lang="ts">
/**
 * 日志收集「唯一结论呈现」组件：表单失败态与结果态共用同一份渲染。
 *
 * 之前同一句 error_message 在结果态最多出现 4 次（横幅、观察事实、失败原因、
 * 诊断卡），两个视图态又各写一套模板，口径必然漂移。这里把「定界卡 / 观察事实 /
 * 失败原文 / 怎么查怎么办」收进一个组件：
 *   - 结果态的观察事实已在横幅里给出，这里不再重复；
 *   - 表单态没有横幅，观察事实在这里给出；
 *   - 失败原文与建议只在诊断卡出现一次。
 * 所有标签取自契约生成物，组件自身不写展示文案。
 */
import { computed } from 'vue'
import { UI_TEXT } from '@hci/shared'
import LogCollectDiagnosis from '@/components/LogCollectDiagnosis.vue'
import LogCollectFaultCard from './LogCollectFaultCard.vue'
import type { LogCollectDisplayView } from '@/api/logCollect'

interface CollectAlertView {
  code?: string | null
  title?: string | null
  customer_text?: string | null
  tone?: string | null
  check?: string[]
  fix?: string[]
}

const props = withDefaults(
  defineProps<{
    variant: 'form' | 'result'
    showDiagnosis?: boolean
    code?: string | null
    reason?: string
    display?: (LogCollectDisplayView & { collect_alert?: CollectAlertView | null }) | null
    conditions?: { key: string; label: string }[]
  }>(),
  {
    showDiagnosis: false,
    code: null,
    reason: '',
    display: null,
    conditions: () => [],
  },
)

// 采集告警：交付成功也可能存在，客户需要「如何检查 / 如何处理」，
// 但不能与失败诊断卡混淆——这是两件正交的事。
const collectAlert = computed<CollectAlertView | null>(() => props.display?.collect_alert ?? null)

// 2026-11-16：已交付（success / partial）不再向客户呈现 fault 归因卡，
// 只保留阶段条；真正的失败结论不受影响，仍完整显示错误定位。
const showFault = computed(
  () => !['success', 'partial'].includes(String(props.display?.conclusion ?? '')),
)
</script>

<template>
  <div class="lc-status-card">
    <!-- 结果态：三段五组件 + S1-S8 阶段条；已交付时隐藏 fault 卡 -->
    <LogCollectFaultCard
      v-if="variant === 'result' && props.display"
      :display="props.display"
      :show-fault="showFault"
    />

    <!-- 观察事实：两个视图态都在这里给出一次；横幅不再重复拼这一段。
         横幅在 fault_attribution 存在时会提前返回，指望它兜底会漏掉多值条件。 -->
    <div v-if="props.conditions.length" class="lc-evidence">
      {{ UI_TEXT.labels.observed_facts }}：{{ props.conditions.map(item => item.label).join('、') }}
    </div>

    <!-- 采集告警三问：交付成功也可能出现，单独成卡且明确不是交付失败 -->
    <div v-if="collectAlert" class="lc-collect-alert">
      <div class="lc-collect-alert-head">
        <span class="lc-collect-alert-title">{{ collectAlert.title || '采集告警' }}</span>
        <el-tag v-if="collectAlert.code" size="small" effect="plain">{{ collectAlert.code }}</el-tag>
      </div>
      <p class="lc-collect-alert-problem">{{ collectAlert.customer_text }}</p>
      <div v-if="collectAlert.check?.length" class="lc-collect-alert-block">
        <strong>如何确认</strong>
        <ol><li v-for="(item, index) in collectAlert.check" :key="`ca-check-${index}`">{{ item }}</li></ol>
      </div>
      <div v-if="collectAlert.fix?.length" class="lc-collect-alert-block">
        <strong>如何处理</strong>
        <ol><li v-for="(item, index) in collectAlert.fix" :key="`ca-fix-${index}`">{{ item }}</li></ol>
      </div>
    </div>

    <!-- 失败原文 + 怎么查 + 怎么办：全页唯一一处 -->
    <LogCollectDiagnosis
      v-if="props.showDiagnosis"
      :code="props.code ?? null"
      :reason="props.reason ?? ''"
    />
  </div>
</template>

<style scoped>
.lc-collect-alert {
  margin-top: 8px;
  padding: 12px;
  border: 1px solid var(--el-color-warning-light-5);
  border-radius: 6px;
  background: var(--el-color-warning-light-9);
}
.lc-collect-alert-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.lc-collect-alert-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--el-color-warning-dark-2);
}
.lc-collect-alert-problem {
  margin: 8px 0 0;
  font-size: 13px;
  color: var(--el-text-color-primary);
}
.lc-collect-alert-block {
  margin-top: 10px;
  font-size: 13px;
  color: var(--el-text-color-regular);
}
.lc-collect-alert-block strong {
  color: var(--el-text-color-primary);
}
.lc-collect-alert-block ol {
  margin: 4px 0 0;
  padding-left: 20px;
}
.lc-collect-alert-block li {
  margin-top: 2px;
}
</style>
