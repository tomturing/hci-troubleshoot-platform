/**
 * 工单详情 · 日志收集证据。
 *
 * 展示口径（顺序即用户复盘时的提问顺序）：
 *  1. 用户选项：当时选了哪些场景、哪个时间范围、发给谁、走哪条交付路径；
 *  2. 环境与探测：架构/acli 版本/日志服务器可达性 —— 「为什么建议走这条路」的依据；
 *  3. 交付与产物：采完的包到底在哪、有没有校验、代传走到第几片；
 *  4. 异常：错误码/交付错误，放在最后而不是最前（正常单据占绝大多数）。
 *
 * 数据缺失一律显示「-」或明确的解释文案，不显示空白：空白无法区分
 * 「字段没存」和「界面 bug」。
 */
<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { LogCollectCaseEvidence, LogCollectTaskEvidence } from '@/api/caseInsights'

const props = defineProps<{ evidence: LogCollectCaseEvidence }>()

const selectedTaskId = ref<string>('')

// 默认选最新任务（后端按 created_at 倒序返回）；evidence 变化时同步选择
watch(
  () => props.evidence,
  (value) => {
    const latest = value?.tasks?.[0]?.task_id ?? ''
    if (!value?.tasks?.some((task) => task.task_id === selectedTaskId.value)) {
      selectedTaskId.value = latest
    }
  },
  { immediate: true },
)

const selected = computed<LogCollectTaskEvidence | null>(() => {
  const tasks = props.evidence.tasks ?? []
  return tasks.find((task) => task.task_id === selectedTaskId.value) ?? tasks[0] ?? null
})

function formatDate(value: string | null | undefined): string {
  if (!value) return '-'
  return new Date(value).toLocaleString('zh-CN')
}

function formatBytes(value: number | null | undefined): string {
  if (value === null || value === undefined) return '-'
  if (value < 1024) return `${value} B`
  const units = ['KiB', 'MiB', 'GiB', 'TiB']
  let size = value / 1024
  let index = 0
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024
    index += 1
  }
  return `${size.toFixed(1)} ${units[index]}`
}

function humanIps(ips: string[] | undefined): string {
  if (!ips || ips.length === 0) return '未指定'
  if (ips.includes('all')) return '全部节点'
  return ips.join('、')
}

function statusTagType(status: string): 'success' | 'warning' | 'danger' | 'info' {
  if (status === 'completed') return 'success'
  if (status === 'failed') return 'danger'
  if (status === 'cancelled') return 'info'
  return 'warning'
}

function integrityText(state: string | null): string {
  if (state === 'verified') return '已校验（节点源包与平台结果一致）'
  if (state === 'unverified') return '未校验（缺少基线摘要，只有传输层校验）'
  return '-'
}

function taskOptionLabel(task: LogCollectTaskEvidence, index: number): string {
  const time = task.created_at ? new Date(task.created_at).toLocaleString('zh-CN') : '未知时间'
  return `#${index + 1} · ${time} · ${task.status_label}`
}

const effectiveTask = computed(() => selected.value)
const probeEvent = computed(() => props.evidence.probes?.[0] ?? null)
const recommended = computed(() => effectiveTask.value?.recommended_path ?? null)
const hasError = computed(
  () =>
    Boolean(effectiveTask.value?.error?.code) ||
    Boolean(effectiveTask.value?.error?.message) ||
    Boolean(effectiveTask.value?.delivery?.error),
)
</script>

<template>
  <div class="lc-panel">
    <div class="lc-summary">
      <el-tag v-if="evidence.case_source === 'log_collect'" type="success" size="small">
        日志收集入口创建
      </el-tag>
      <span class="lc-summary-text">
        共 <strong>{{ evidence.task_total }}</strong> 次采集任务<span v-if="evidence.tasks.length < evidence.task_total">
          （显示最近 {{ evidence.tasks.length }} 次）</span
        >
      </span>
      <el-select
        v-if="evidence.tasks.length > 1"
        v-model="selectedTaskId"
        size="small"
        style="width: 320px; margin-left: auto"
      >
        <el-option
          v-for="(task, index) in evidence.tasks"
          :key="task.task_id"
          :label="taskOptionLabel(task, index)"
          :value="task.task_id"
        />
      </el-select>
    </div>

    <el-empty v-if="!effectiveTask" description="该工单没有日志收集任务" />

    <template v-else>
      <!-- 1. 用户选项 -->
      <h4 class="lc-section-title">用户选项</h4>
      <el-descriptions :column="2" border size="small">
        <el-descriptions-item label="故障场景" :span="2">
          <template v-if="effectiveTask.modules.length > 0">
            <el-tag
              v-for="item in effectiveTask.modules"
              :key="item.value"
              size="small"
              type="warning"
              style="margin: 0 6px 4px 0"
            >
              {{ item.label }}
              <span v-if="item.duration_hint" class="lc-tag-sub">· {{ item.duration_hint }}</span>
            </el-tag>
          </template>
          <span v-else class="lc-muted">-</span>
        </el-descriptions-item>
        <el-descriptions-item label="时间范围">
          {{ effectiveTask.date_from || '-' }} ~ {{ effectiveTask.date_to || '-' }}
          <span v-if="effectiveTask.day_span" class="lc-muted">（{{ effectiveTask.day_span }} 天）</span>
        </el-descriptions-item>
        <el-descriptions-item label="发起节点">
          <code>{{ effectiveTask.origin_node_ip || '-' }}</code>
        </el-descriptions-item>
        <el-descriptions-item label="目标主机">
          {{ humanIps(effectiveTask.ips) }}
        </el-descriptions-item>
        <el-descriptions-item label="交付路径">
          {{ effectiveTask.upload_mode_label || '-' }}
          <el-tag v-if="effectiveTask.remote_url" size="small" style="margin-left: 6px">
            自定义接收地址
          </el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="额外采集路径">
          {{ effectiveTask.extra_paths.length > 0 ? effectiveTask.extra_paths.join('、') : '未指定' }}
        </el-descriptions-item>
      </el-descriptions>

      <!-- 2. 环境与探测 -->
      <h4 class="lc-section-title">环境与探测</h4>
      <el-descriptions :column="2" border size="small">
        <el-descriptions-item label="节点架构">{{ effectiveTask.arch || '未上报' }}</el-descriptions-item>
        <el-descriptions-item label="acli 版本">{{ effectiveTask.acli_version || '未上报' }}</el-descriptions-item>
        <el-descriptions-item label="探测时间">
          {{ formatDate(probeEvent?.created_at) }}
        </el-descriptions-item>
        <el-descriptions-item label="可采节点数">
          {{ effectiveTask.probe?.hosts_count ?? '-' }}
        </el-descriptions-item>
        <el-descriptions-item label="日志服务器可达">
          <template v-if="effectiveTask.probe?.log_server_reachable === true">
            <el-tag type="success" size="small">可达</el-tag>
            <span class="lc-muted">
              HTTP {{ effectiveTask.probe?.http_code ?? '-' }}
              <span v-if="effectiveTask.probe?.latency_ms"> · {{ effectiveTask.probe.latency_ms }} ms</span>
            </span>
          </template>
          <template v-else-if="effectiveTask.probe?.log_server_reachable === false">
            <el-tag type="danger" size="small">不可达</el-tag>
          </template>
          <span v-else class="lc-muted">未探测</span>
        </el-descriptions-item>
        <el-descriptions-item label="建议路径">
          <template v-if="recommended">
            <el-tag
              size="small"
              :type="recommended.value === 'acli_direct' ? 'success' : recommended.value === 'bridge_relay' ? 'warning' : 'info'"
            >
              {{ recommended.label }}
            </el-tag>
            <span class="lc-muted lc-reason">{{ recommended.reason }}</span>
          </template>
          <span v-else class="lc-muted">-</span>
        </el-descriptions-item>
        <el-descriptions-item label="场景目录来源">
          {{ effectiveTask.probe?.catalog_source || '-' }}
        </el-descriptions-item>
        <el-descriptions-item label="报告基线指纹">
          <code v-if="effectiveTask.baseline_report_hash" class="lc-mono">
            {{ effectiveTask.baseline_report_hash }}
          </code>
          <span v-else class="lc-muted">无（未带基线，交付无法做端到端校验）</span>
        </el-descriptions-item>
      </el-descriptions>

      <!-- 3. 交付与产物 -->
      <h4 class="lc-section-title">交付与产物</h4>
      <el-descriptions :column="2" border size="small">
        <el-descriptions-item label="采集状态">
          <el-tag :type="statusTagType(effectiveTask.status)" size="small">
            {{ effectiveTask.status_label }}
          </el-tag>
          <span class="lc-muted">进度 {{ effectiveTask.progress }}%</span>
        </el-descriptions-item>
        <el-descriptions-item label="交付状态">
          {{ effectiveTask.delivery_status_label || '-' }}
          <span v-if="effectiveTask.delivery?.attempts" class="lc-muted">
            · 尝试 {{ effectiveTask.delivery.attempts }} 次
          </span>
        </el-descriptions-item>
        <el-descriptions-item label="完整性">
          <el-tag :type="effectiveTask.integrity_state === 'verified' ? 'success' : 'warning'" size="small">
            {{ effectiveTask.integrity_state || 'unknown' }}
          </el-tag>
          <span class="lc-muted lc-reason">{{ integrityText(effectiveTask.integrity_state) }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="交付依据">
          {{ effectiveTask.delivery_evidence === 'acli_self_reported' ? 'acli 自报（平台无独立回执）' : '平台核验' }}
          <div v-if="effectiveTask.delivery_evidence_note" class="lc-muted lc-reason">
            {{ effectiveTask.delivery_evidence_note }}
          </div>
        </el-descriptions-item>
        <el-descriptions-item label="节点侧包" :span="2">
          <div v-if="effectiveTask.archive?.path">
            <code class="lc-mono">{{ effectiveTask.archive.path }}</code>
            <div class="lc-muted">
              {{ formatBytes(effectiveTask.archive.size) }} · sha256
              {{ effectiveTask.archive.sha256 || '-' }}
            </div>
          </div>
          <span v-else class="lc-muted">无（P1 直传或未产出）</span>
        </el-descriptions-item>
        <el-descriptions-item label="平台侧对象" :span="2">
          <div v-if="effectiveTask.object?.key">
            <code class="lc-mono">{{ effectiveTask.object.key }}</code>
            <div class="lc-muted">
              {{ formatBytes(effectiveTask.object.size) }} · sha256 {{ effectiveTask.object.sha256 || '-' }}
            </div>
          </div>
          <span v-else class="lc-muted">无（P1 直传平台不留副本）</span>
        </el-descriptions-item>
        <el-descriptions-item label="代传分片">
          <template v-if="effectiveTask.relay?.chunk_total">
            {{ effectiveTask.relay.chunk_index ?? 0 }} / {{ effectiveTask.relay.chunk_total }}
            <span class="lc-muted">
              · {{ formatBytes(effectiveTask.relay.chunk_size) }}/片 · 重建 {{ effectiveTask.relay.restarts ?? 0 }} 次
            </span>
          </template>
          <span v-else class="lc-muted">-</span>
        </el-descriptions-item>
        <el-descriptions-item label="时间线">
          创建 {{ formatDate(effectiveTask.created_at) }}
          <div class="lc-muted">
            完成 {{ formatDate(effectiveTask.finished_at) }} · 更新 {{ formatDate(effectiveTask.updated_at) }}
          </div>
        </el-descriptions-item>
      </el-descriptions>

      <!-- 4. 异常 -->
      <template v-if="hasError">
        <h4 class="lc-section-title">异常</h4>
        <el-descriptions :column="1" border size="small">
          <el-descriptions-item v-if="effectiveTask.error?.code || effectiveTask.error?.message" label="任务错误">
            <el-tag v-if="effectiveTask.error?.code" type="danger" size="small">
              {{ effectiveTask.error.code }}
            </el-tag>
            <span class="lc-error">{{ effectiveTask.error?.message || '-' }}</span>
          </el-descriptions-item>
          <el-descriptions-item v-if="effectiveTask.delivery?.error" label="交付错误">
            <span class="lc-error">{{ effectiveTask.delivery.error }}</span>
          </el-descriptions-item>
          <el-descriptions-item v-if="effectiveTask.relay?.last_error" label="代传错误">
            <span class="lc-error">{{ effectiveTask.relay.last_error }}</span>
          </el-descriptions-item>
        </el-descriptions>
      </template>

      <!-- 5. 探测历史（原始事实，供深挖） -->
      <el-collapse v-if="evidence.probes?.length" class="lc-probes">
        <el-collapse-item :title="`下发前探测记录（最近 ${evidence.probes.length} 条）`" name="probes">
          <div v-for="(probe, index) in evidence.probes" :key="index" class="lc-probe-item">
            <div class="lc-probe-head">
              <el-tag size="small" :type="probe.level === 'error' ? 'danger' : 'info'">{{ probe.event }}</el-tag>
              <span class="lc-muted">{{ formatDate(probe.created_at) }}</span>
            </div>
            <pre class="lc-probe-body">{{ JSON.stringify(probe.extra, null, 2) }}</pre>
          </div>
        </el-collapse-item>
      </el-collapse>
    </template>
  </div>
</template>

<style scoped>
.lc-panel {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.lc-summary {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 8px;
  flex-wrap: wrap;
}

.lc-summary-text {
  font-size: 13px;
  color: #606266;
}

.lc-section-title {
  margin: 16px 0 8px;
  font-size: 14px;
  font-weight: 600;
  color: #303133;
}

.lc-muted {
  color: #909399;
  font-size: 12px;
  margin-left: 6px;
}

.lc-reason {
  display: inline-block;
  max-width: 100%;
}

.lc-tag-sub {
  opacity: 0.8;
  font-size: 11px;
}

.lc-mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  word-break: break-all;
}

.lc-error {
  color: #f56c6c;
  margin-left: 6px;
  word-break: break-word;
}

.lc-probes {
  margin-top: 16px;
}

.lc-probe-item {
  margin-bottom: 12px;
}

.lc-probe-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
}

.lc-probe-body {
  margin: 0;
  padding: 10px;
  background: #f5f7fa;
  border-radius: 4px;
  font-size: 12px;
  max-height: 240px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
