<script setup lang="ts">
/**
 * 管理端「链路定界」：首个异常点摘要 + 段/组件表 + S1-S8 阶段时间线。
 *
 * 抽成独立组件的原因：管理端详情抽屉与真浏览器验收页必须渲染同一份实现，
 * 否则截图证明的是验收页而不是产品页面。所有文案与枚举仍来自单一事实源契约。
 *
 * variant="summary"：摘要卡（首个异常点，判据/证据入口/调用链）
 * variant="panel"：链路定界页签内容（段与组件表、阶段时间线）
 */
import { computed } from 'vue'
import { COMPONENT_ORDER, COMPONENTS, FAULT_CATALOG, SCOPE_LABELS, type ScopeKey } from '@hci/shared'
import type { AdminLogCollectTask } from '@/api/logCollectAdminTasks'

const props = defineProps<{
  variant: 'summary' | 'panel'
  display?: AdminLogCollectTask['display'] | null
  errorCode?: string | null
  fallbackTraceId?: string | null
}>()

/** 段与组件固定枚举；问题描述与建议动作取自错误字典，管理端不另写一份。 */
const componentRows = computed(() =>
  COMPONENT_ORDER.map((key) => {
    const item = COMPONENTS[key]
    const cases = Object.values(FAULT_CATALOG).filter((entry) => entry.component === key)
    return {
      key,
      scopeLabel: SCOPE_LABELS[item.scope as ScopeKey] ?? item.scope,
      label: item.label,
      tag: item.tag,
      duty: item.duty,
      problems: cases.map((entry) => entry.problem),
      actions: cases.map((entry) => entry.action),
    }
  }),
)
/** S1-S8 阶段时间线；老后端未返回时如实留空，不伪造阶段状态。 */
const stageRows = computed(() => props.display?.stages ?? [])
/** 首个异常点：管理员据此决定查哪张证据表、哪一段日志。 */
const faultOf = computed(() => props.display?.fault_attribution ?? null)
/** 唯一调用链优先取派生字段，降级取任务列。 */
const detailTraceId = computed(() => props.display?.trace_id ?? props.fallbackTraceId ?? null)
</script>

<template>
  <div v-if="variant === 'summary'">
    <div v-if="faultOf" class="upload-attribution">
      <div class="upload-attribution-head">
        <el-tag size="small" effect="dark" type="warning">{{ faultOf.component_tag }}</el-tag>
        <strong>{{ faultOf.scope_label }} · {{ faultOf.stage }} {{ faultOf.stage_label }}</strong>
        <code class="code">{{ faultOf.code }}</code>
      </div>
      <div class="upload-attribution-line">{{ faultOf.customer_text }}</div>
      <div class="sub">管理员判据：{{ faultOf.admin_detail }}</div>
      <div class="sub">证据入口：{{ faultOf.evidence }}</div>
      <div class="sub">
        调用链ID：{{ detailTraceId ?? '—' }} · 任务错误码：{{ errorCode ?? '—' }}
      </div>
    </div>
    <div v-else class="sub">
      平台未记录可识别的首个异常点；请按下方阶段时间线与四张证据表判断真实链路。
    </div>
  </div>

  <div v-else>
    <div class="sub">三段五组件为固定枚举；问题描述与建议动作来自单一事实源契约，客户弹框、横幅与管理端共用。</div>
    <el-table :data="componentRows" size="small" border>
      <el-table-column prop="scopeLabel" label="大段" width="80" />
      <el-table-column prop="label" label="组件" width="140" />
      <el-table-column prop="tag" label="组件标签" width="180" />
      <el-table-column prop="duty" label="职责范围" min-width="190" />
      <el-table-column label="问题描述" min-width="240">
        <template #default="{ row }">
          <div v-for="(text, index) in row.problems" :key="index">{{ text }}</div>
        </template>
      </el-table-column>
      <el-table-column label="建议动作" min-width="260">
        <template #default="{ row }">
          <div v-for="(text, index) in row.actions" :key="index">{{ text }}</div>
        </template>
      </el-table-column>
    </el-table>

    <h4 class="section-title">S1-S8 阶段时间线（首个异常点按此顺序取第一个异常段）</h4>
    <el-table :data="stageRows" size="small" border>
      <el-table-column prop="key" label="阶段" width="62" />
      <el-table-column prop="label" label="环节" width="100" />
      <el-table-column prop="scope_label" label="大段" width="120" />
      <el-table-column label="组件" min-width="190">
        <template #default="{ row }">
          <el-tag v-for="item in row.components" :key="item.key" size="small" effect="plain">{{ item.label }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="status_label" label="状态" width="90" />
      <el-table-column prop="criteria" label="客观判据" min-width="210" />
      <el-table-column prop="fault_code" label="错误码" width="130" />
      <el-table-column prop="customer_text" label="客户文案" min-width="270" />
    </el-table>
    <div v-if="!stageRows.length" class="sub">
      老后端未返回阶段时间线；请升级 case-service 后重新打开详情。
    </div>
  </div>
</template>

<style scoped>
.upload-attribution {
  padding: 10px 12px;
  margin-bottom: 12px;
  background: #fef6f6;
  border: 1px solid #fde2e2;
  border-radius: 4px;
}
.upload-attribution-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}
.upload-attribution-line {
  margin-bottom: 4px;
  font-size: 13px;
  font-weight: 600;
  color: #f56c6c;
  word-break: break-word;
}
.code {
  padding: 1px 6px;
  font-size: 12px;
  background: #fff;
  border: 1px solid #fde2e2;
  border-radius: 3px;
}
.sub {
  font-size: 12px;
  line-height: 1.7;
  color: #909399;
}
.section-title {
  margin: 14px 0 8px;
  font-size: 13px;
  font-weight: 600;
  color: #303133;
}
</style>