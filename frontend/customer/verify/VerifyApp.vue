<script setup lang="ts">
/**
 * 真浏览器验收入口（仅 dev）：只挂载产品中真实使用的组件与函数。
 *
 * - 横幅文案：buildResultHeadline()（客户面板同一函数）
 * - 定界卡与阶段条：LogCollectFaultCard.vue（客户面板同一组件）
 * - 弹框：showCustomerFault()（客户面板同一出口）
 * - 错误字典：@hci/shared 的 codegen 产物（前后端同源）
 *
 * 用 ?code=LC-XXXX-XXX 切换场景；截图由 Playwright 逐场景全页截取。
 */
import { computed, onMounted } from 'vue'
import {
  COMPONENTS,
  CONDITION_LABELS,
  CONCLUSION_LABELS,
  FAULT_ATTRIBUTION,
  FAULT_CATALOG,
  PHASE_LABELS,
  SCOPE_LABELS,
  STAGES,
  STAGE_STATUS_LABELS,
  UI_TEXT,
  UPLOAD_MODE_LABELS,
  faultDetail,
  formatFaultText,
  type ComponentKey,
  type FaultCode,
  type ScopeKey,
  type StageKey,
} from '@hci/shared'
import LogCollectFaultCard from '../src/components/LogCollectFaultCard.vue'
import { buildResultHeadline } from '../src/utils/logCollectDelivery'
import { showCustomerFault } from '../src/utils/logCollectFault'
import type { LogCollectDisplayView, LogCollectStageView } from '../src/api/logCollect'

const requested = new URLSearchParams(window.location.search).get('code') ?? ''
const code: FaultCode = (requested in FAULT_CATALOG ? requested : 'LC-HCI-001') as FaultCode
const traceId = `trace-${code}-Q2026111300155`
const stageOrder = FAULT_ATTRIBUTION.stage_order as string[]

function stagesFor(target: FaultCode): LogCollectStageView[] {
  const faultStage = FAULT_CATALOG[target].stage as string
  const faultIndex = stageOrder.indexOf(faultStage)
  return stageOrder.map((key, index) => {
    const item = STAGES[key as StageKey]
    const status = index < faultIndex ? 'success' : index === faultIndex ? 'failed' : 'unknown'
    return {
      key,
      label: item.label,
      scopes: [...item.scopes],
      scope_label: item.scope_label,
      components: (item.components as readonly string[]).map((componentKey) => {
        const component = COMPONENTS[componentKey as ComponentKey]
        return {
          key: componentKey,
          label: component.label,
          tag: component.tag,
          scope: component.scope,
          scope_label: SCOPE_LABELS[component.scope as ScopeKey] ?? component.scope,
        }
      }),
      status,
      status_label: (STAGE_STATUS_LABELS as Record<string, string>)[status] ?? status,
      criteria: item.criteria,
      fault_code: index === faultIndex ? target : null,
      customer_text: index === faultIndex ? formatFaultText(target) : null,
    }
  })
}

// 夹具的所有标签都取自契约生成物：验收页自己不写第二份中文口径。
const display: LogCollectDisplayView = {
  phase: 'settled',
  phase_label: PHASE_LABELS.settled,
  conclusion: 'failed',
  conclusion_label: CONCLUSION_LABELS.failed,
  conclusion_tone: 'error',
  conditions: [{ key: 'artifact_uninspected', label: CONDITION_LABELS.artifact_uninspected }],
  progress_percent: null,
  upload_mode_label: UPLOAD_MODE_LABELS.acli_direct,
  stages: stagesFor(code),
  fault_attribution: faultDetail(code, traceId),
  trace_id: traceId,
}

const headline = buildResultHeadline({
  status: 'completed',
  delivered: false,
  when: '2026-11-13 09:49',
  display: {
    conclusion: display.conclusion,
    conclusion_label: display.conclusion_label,
    conclusion_tone: display.conclusion_tone,
    phase_label: display.phase_label,
    conditions: display.conditions,
    fault_attribution: { customer_text: display.fault_attribution?.customer_text ?? '' },
    trace_id: display.trace_id,
  },
})

const catalogRows = computed(() =>
  Object.entries(FAULT_CATALOG).map(([key, item]) => {
    const component = COMPONENTS[item.component as ComponentKey]
    return {
      code: key,
      scope: SCOPE_LABELS[component.scope as ScopeKey],
      tag: component.tag,
      stage: `${item.stage} ${STAGES[item.stage as StageKey].label}`,
      customerText: formatFaultText(key),
      adminDetail: item.admin_detail,
    }
  }),
)

function triggerFault(): void {
  showCustomerFault('', code)
}

onMounted(triggerFault)
</script>

<template>
  <main class="verify-page">
    <header class="verify-header">
      <h1>日志收集报错 · 真浏览器验收</h1>
      <p>
        单一事实源 <code>contracts/log_collect_display.json</code> · 场景 <code>{{ code }}</code>
        · 唯一调用链 <code>{{ traceId }}</code>
      </p>
    </header>

    <section class="verify-section">
      <h2>1. 客户侧真实报错弹框（Element Plus Message）</h2>
      <p class="verify-note">
        调用产品里的 <code>showCustomerFault()</code>；格式固定为「组件标签：问题描述；建议动作」。
      </p>
      <el-button type="danger" size="small" @click="triggerFault">再次触发弹框</el-button>
      <div class="verify-inline-text">{{ formatFaultText(code) }}</div>
    </section>

    <section class="verify-section">
      <h2>2. 客户侧结果态（真实横幅 + 真实定界卡 + 真实阶段条）</h2>
      <div class="verify-customer">
        <el-alert
          :type="headline.type"
          :closable="false"
          show-icon
          :title="headline.title"
          :description="headline.description"
        />
        <LogCollectFaultCard :display="display" />
        <div class="verify-facts">
          {{ UI_TEXT.labels.observed_facts }}：
          <el-tag v-for="item in display.conditions" :key="item.key" size="small" effect="plain">
            {{ item.label }}
          </el-tag>
        </div>
      </div>
    </section>

    <section class="verify-section">
      <h2>3. 错误字典全量（{{ catalogRows.length }} 条，客户与管理端同源）</h2>
      <el-table :data="catalogRows" size="small" border>
        <el-table-column prop="code" label="错误码" width="140" />
        <el-table-column prop="scope" label="大段" width="80" />
        <el-table-column prop="tag" label="组件标签" width="180" />
        <el-table-column prop="stage" label="阶段" width="130" />
        <el-table-column prop="customerText" label="客户文案（组件标签：问题描述；建议动作）" min-width="420" />
        <el-table-column prop="adminDetail" label="管理员判据" min-width="360" />
      </el-table>
    </section>
  </main>
</template>

<style>
body {
  margin: 0;
  background: #f5f7fa;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif;
}
.verify-page {
  max-width: 1440px;
  margin: 0 auto;
  padding: 20px 24px 40px;
  display: flex;
  flex-direction: column;
  gap: 18px;
}
.verify-header h1 {
  margin: 0 0 6px;
  font-size: 22px;
}
.verify-header p {
  margin: 0;
  font-size: 13px;
  color: #606266;
}
.verify-header code,
.verify-note code {
  padding: 1px 5px;
  background: #eef1f6;
  border-radius: 3px;
  font-size: 12px;
}
.verify-section {
  padding: 14px 16px;
  background: #fff;
  border: 1px solid #e4e7ed;
  border-radius: 6px;
}
.verify-section h2 {
  margin: 0 0 10px;
  font-size: 15px;
}
.verify-note {
  margin: 0 0 10px;
  font-size: 12px;
  color: #606266;
}
.verify-inline-text {
  margin-top: 10px;
  padding: 8px 10px;
  font-size: 13px;
  font-weight: 600;
  color: #f56c6c;
  background: #fef0f0;
  border: 1px solid #fde2e2;
  border-radius: 4px;
  word-break: break-word;
}
.verify-customer {
  display: flex;
  flex-direction: column;
  gap: 12px;
  max-width: 900px;
}
.verify-facts {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: #606266;
}
</style>
