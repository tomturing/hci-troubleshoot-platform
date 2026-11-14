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
import { UI_TEXT } from '@hci/shared'
import LogCollectDiagnosis from '@/components/LogCollectDiagnosis.vue'
import LogCollectFaultCard from './LogCollectFaultCard.vue'
import type { LogCollectDisplayView } from '@/api/logCollect'

const props = withDefaults(
  defineProps<{
    variant: 'form' | 'result'
    showDiagnosis?: boolean
    code?: string | null
    reason?: string
    display?: LogCollectDisplayView | null
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
</script>

<template>
  <div class="lc-status-card">
    <!-- 结果态：三段五组件 + 首个异常点 + S1-S8 阶段条，与管理端同源 -->
    <LogCollectFaultCard v-if="variant === 'result' && props.display" :display="props.display" />

    <!-- 观察事实：两个视图态都在这里给出一次；横幅不再重复拼这一段。
         横幅在 fault_attribution 存在时会提前返回，指望它兜底会漏掉多值条件。 -->
    <div v-if="props.conditions.length" class="lc-evidence">
      {{ UI_TEXT.labels.observed_facts }}：{{ props.conditions.map(item => item.label).join('、') }}
    </div>

    <!-- 失败原文 + 怎么查 + 怎么办：全页唯一一处 -->
    <LogCollectDiagnosis
      v-if="props.showDiagnosis"
      :code="props.code ?? null"
      :reason="props.reason ?? ''"
    />
  </div>
</template>
