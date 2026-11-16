<script setup lang="ts">
/**
 * 管理端「链路定界」：首个异常点摘要 + 段/组件表 + S1-S8 阶段时间线。
 *
 * 抽成独立组件的原因：管理端详情抽屉与真浏览器验收页必须渲染同一份实现，
 * 否则截图证明的是验收页而不是产品页面。所有文案与枚举仍来自单一事实源契约。
 *
 * variant="summary"：摘要卡（首个异常点，判据/证据入口/调用链）
 * variant="panel"：S1-S8 阶段时间线；固定段/组件设计说明收进标题旁感叹号弹框
 */
import { computed, ref } from 'vue'
import { COMPONENT_ORDER, COMPONENTS, FAULT_CATALOG, SCOPE_LABELS, type ScopeKey } from '@hci/shared'
import type { AdminLogCollectTask } from '@/api/logCollectAdminTasks'

const props = defineProps<{
  variant: 'summary' | 'panel'
  display?: AdminLogCollectTask['display'] | null
  errorCode?: string | null
  fallbackTraceId?: string | null
}>()

/** 固定设计说明弹框：与工单无关，管理员按需查看，不占详情抽屉的常驻空间。 */
const referenceVisible = ref(false)

/**
 * 颜色只是辅助提示，状态文字标签始终保留：
 * 色觉障碍、打印、黑白屏场景下仍必须能一眼区分未知/成功/失败。
 */
const STAGE_TONES: Record<string, 'success' | 'danger' | 'info' | 'primary' | 'warning'> = {
  success: 'success',
  failed: 'danger',
  unknown: 'info',
  running: 'primary',
  waiting: 'warning',
  skipped: 'info',
}
function statusTone(status: string) {
  return STAGE_TONES[status] ?? 'info'
}
function statusHint(status: string): string {
  // 「查不到证据」不等于「该环节失败」，这是派生层最重要的一条硬约束。
  return status === 'unknown' ? '客观证据未上报，未知不等于失败；请先按证据入口核对。' : ''
}

/**
 * 段与组件固定枚举；问题描述与建议动作取自错误字典，管理端不另写一份。
 * 同一错误码的 problem/action 绑定成一个 pair：配对关系在数据层成立，
 * 渲染层不依赖两个平行数组的索引对齐，也不依赖任何滚动联动。
 */
const componentRows = computed(() =>
  COMPONENT_ORDER.map((key) => {
    const item = COMPONENTS[key]
    const cases = Object.entries(FAULT_CATALOG).filter(([, entry]) => entry.component === key)
    return {
      key,
      scopeLabel: SCOPE_LABELS[item.scope as ScopeKey] ?? item.scope,
      label: item.label,
      tag: item.tag,
      duty: item.duty,
      pairs: cases.map(([code, entry]) => ({ code, problem: entry.problem, action: entry.action })),
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
    <h4 class="section-title">
      S1-S8 阶段时间线（首个异常点按此顺序取第一个异常段）
      <el-button
        class="section-help"
        type="info"
        size="small"
        circle
        plain
        data-testid="attribution-help"
        aria-label="查看三段五组件设计说明"
        @click="referenceVisible = true"
      >!</el-button>
    </h4>
    <el-table :data="stageRows" size="small" border>
      <el-table-column prop="key" label="阶段" width="52" />
      <el-table-column prop="label" label="环节" width="78" />
      <el-table-column prop="scope_label" label="大段" width="92" />
      <el-table-column label="组件" min-width="136">
        <template #default="{ row }">
          <el-tag v-for="item in row.components" :key="item.key" size="small" effect="plain">{{ item.label }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="76">
        <template #default="{ row }">
          <el-tooltip v-if="statusHint(row.status)" :content="statusHint(row.status)" placement="top">
            <el-tag size="small" :type="statusTone(row.status)" effect="dark" data-testid="stage-status">
              {{ row.status_label }}
            </el-tag>
          </el-tooltip>
          <el-tag v-else size="small" :type="statusTone(row.status)" effect="dark" data-testid="stage-status">
            {{ row.status_label }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="criteria" label="客观判据" min-width="140" show-overflow-tooltip />
      <el-table-column prop="fault_code" label="错误码" width="112" />
      <el-table-column prop="customer_text" label="客户文案" min-width="150" show-overflow-tooltip />
    </el-table>
    <div v-if="!stageRows.length" class="sub">
      老后端未返回阶段时间线；请升级 case-service 后重新打开详情。
    </div>

    <!-- 弹框由 el-dialog 挂到 body，不继承抽屉宽度；卡片式布局避免窄列长文本撑高行高。 -->
    <el-dialog
      v-model="referenceVisible"
      title="三段五组件设计说明（固定契约，与当前工单无关）"
      width="min(94vw, 1180px)"
      append-to-body
      class="attribution-reference"
      data-testid="attribution-reference"
    >
      <p class="dialog-intro">
        三段五组件为固定枚举；问题描述与建议动作来自单一事实源契约，客户弹框、横幅与管理端共用。
        此处只解释设计口径，不表示当前工单发生了这些异常。
      </p>
      <div v-for="row in componentRows" :key="row.key" class="ref-card">
        <div class="ref-card-head">
          <span class="ref-scope">{{ row.scopeLabel }}</span>
          <strong>{{ row.label }}</strong>
          <code>{{ row.tag }}</code>
        </div>
        <div class="ref-line"><span class="ref-label">职责范围</span><span>{{ row.duty }}</span></div>
        <!-- 一行一对：错误码 + 问题描述 + 建议动作共享同一行，滚动统一交给弹框正文。 -->
        <div class="ref-columns">
          <div class="ref-pair-head">
            <span>错误码</span>
            <span>问题描述</span>
            <span>建议动作</span>
          </div>
          <div v-for="pair in row.pairs" :key="pair.code" class="ref-pair" data-testid="reference-pair">
            <code class="ref-code">{{ pair.code }}</code>
            <span class="ref-cell" data-label="问题描述">{{ pair.problem }}</span>
            <span class="ref-cell" data-label="建议动作">{{ pair.action }}</span>
          </div>
        </div>
      </div>
      <template #footer>
        <el-button type="primary" @click="referenceVisible = false">知道了</el-button>
      </template>
    </el-dialog>
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
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 14px 0 8px;
  font-size: 13px;
  font-weight: 600;
  line-height: 1.6;
  color: #303133;
}
/* 帮助按钮贴合标题字号：默认 circle 按钮直径远大于 13px 标题，视觉上像「大一号」。 */
.section-help.el-button {
  flex: none;
  width: 18px;
  height: 18px;
  min-height: 18px;
  padding: 0;
  font-size: 12px;
  line-height: 16px;
  font-weight: 700;
}
.dialog-intro {
  margin: 0 0 12px;
  font-size: 13px;
  line-height: 1.7;
  color: var(--el-text-color-regular);
}
.ref-card {
  padding: 12px 14px;
  margin-bottom: 10px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  background: var(--el-bg-color);
}
.ref-card-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 8px;
}
.ref-scope {
  font-size: 12px;
  color: var(--el-color-primary);
}
.ref-card-head code {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.ref-line {
  display: grid;
  grid-template-columns: 72px minmax(0, 1fr);
  gap: 8px;
  padding: 3px 0;
  font-size: 13px;
  line-height: 1.7;
  word-break: break-word;
}
.ref-label {
  color: var(--el-text-color-secondary);
}
.ref-columns {
  margin-top: 6px;
}
.ref-pair-head,
.ref-pair {
  display: grid;
  grid-template-columns: 118px repeat(2, minmax(0, 1fr));
  gap: 12px;
  align-items: start;
  padding: 5px 0;
}
.ref-pair-head {
  border-top: 1px solid var(--el-border-color-lighter);
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.ref-pair {
  border-top: 1px dashed var(--el-border-color-lighter);
  font-size: 13px;
  line-height: 1.6;
}
.ref-code {
  font-size: 11px;
  color: var(--el-text-color-secondary);
  word-break: break-all;
}
.ref-cell {
  word-break: break-word;
}
.ref-cell::before {
  display: none;
  color: var(--el-text-color-secondary);
  content: attr(data-label) '：';
}
@media (max-width: 760px) {
  .ref-pair-head {
    display: none;
  }
  .ref-pair {
    grid-template-columns: minmax(0, 1fr);
    gap: 2px;
  }
  .ref-cell::before {
    display: inline;
  }
}
</style>
<style>
/* el-dialog 挂在 body 上：整体滚动取代列内滚动，配对行不会被两个滚动条拆散。 */
.attribution-reference .el-dialog__body {
  max-height: 80vh;
  overflow-y: auto;
}
</style>