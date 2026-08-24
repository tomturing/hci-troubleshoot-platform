<script setup lang="ts">
/**
 * 虚拟机控制台截图审计查询（设计文档 §7.3）。
 * 仅展示脱敏摘要；原图访问必须走授权端点并单独记审计，本页面不提供原图下载。
 */
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'

const internalToken = localStorage.getItem('internalToken') || ''
const authHeader = { Authorization: `Bearer ${internalToken}` }

interface CaptureItem {
  capture_id: string
  case_id: string
  vm_id: string
  host_node_id: string
  mode: string
  status: string
  error_code: string | null
  signal_id: string | null
  source_kbd_id: string | null
  source_kbd_revision: string | null
  tool_catalog_revision: string | null
  wake_state: string
  wake_confirmed_by: string | null
  wake_confirmed_at: string | null
  vision_state: string | null
  vision_summary: string | null
  vision_confidence: number | null
  vision_model_revision: string | null
  near_black: string | null
  has_baseline: boolean
  has_recapture: boolean
  trace_id: string | null
  created_at: string
  completed_at: string | null
}

const filters = reactive({
  case_id: '',
  vm_id: '',
  status: '',
  wake_state: '',
  vision_state: '',
  trace_id: '',
})
const items = ref<CaptureItem[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const loading = ref(false)

const statusOptions = [
  'created', 'inventory_verified', 'baseline_capturing', 'baseline_captured',
  'quality_checked', 'baseline_uploaded', 'vision_analyzing', 'completed',
  'wake_confirmation_pending', 'wake_declined', 'waking', 'recapturing',
  'failed', 'expired', 'cancelled',
]
const wakeOptions = ['not_needed', 'confirmation_pending', 'confirmed', 'declined', 'non_interactive', 'timed_out', 'failed']
const visionOptions = [
  'booting', 'login_prompt', 'desktop', 'black_screen', 'kernel_panic',
  'bsod', 'installer_error', 'application_error', 'no_signal', 'unknown', 'unavailable',
]

async function loadCaptures() {
  loading.value = true
  try {
    const params = new URLSearchParams()
    for (const [key, value] of Object.entries(filters)) {
      if (value) params.set(key, value)
    }
    params.set('limit', String(pageSize.value))
    params.set('offset', String((page.value - 1) * pageSize.value))
    const resp = await fetch(`/api/v1/vm-console/captures?${params}`, { headers: authHeader })
    if (!resp.ok) {
      ElMessage.error(`查询失败：HTTP ${resp.status}`)
      return
    }
    const data = await resp.json()
    items.value = data.items || []
    total.value = data.total || 0
  } catch (e) {
    ElMessage.error(`查询失败：${e}`)
  } finally {
    loading.value = false
  }
}

function resetFilters() {
  for (const key of Object.keys(filters)) (filters as Record<string, string>)[key] = ''
  page.value = 1
  loadCaptures()
}

function statusTagType(status: string): 'success' | 'warning' | 'danger' | 'info' | 'primary' {
  if (status === 'completed') return 'success'
  if (status === 'failed' || status === 'expired' || status === 'cancelled') return 'danger'
  if (status.startsWith('wake') || status === 'recapturing') return 'warning'
  return 'info'
}

const eventsDrawerVisible = ref(false)
const eventsLoading = ref(false)
const eventsCaptureId = ref('')
const eventItems = ref<Array<{ event_id: string; event_type: string; actor: string | null; detail: Record<string, unknown>; created_at: string }>>([])

async function openEventsDrawer(captureId: string) {
  eventsCaptureId.value = captureId
  eventsDrawerVisible.value = true
  eventsLoading.value = true
  try {
    const resp = await fetch(`/api/v1/vm-console/captures/${captureId}/events`, { headers: authHeader })
    if (!resp.ok) {
      ElMessage.error(`事件查询失败：HTTP ${resp.status}`)
      return
    }
    const data = await resp.json()
    eventItems.value = data.items || []
  } catch (e) {
    ElMessage.error(`事件查询失败：${e}`)
  } finally {
    eventsLoading.value = false
  }
}

interface ReplayItem {
  name: string
  label: string
  expected_display_state: string
  expected_near_black: boolean
  parse_ok: boolean
  near_black: boolean
  png_derived: boolean
  observation_contract_ok: boolean
  near_black_matches_expectation: boolean
}
const replayItems = ref<ReplayItem[]>([])
const replayLoading = ref(false)

async function loadReplayFixtures() {
  replayLoading.value = true
  try {
    const resp = await fetch('/api/v1/vm-console/replay-fixtures', { headers: authHeader })
    if (resp.ok) {
      const data = await resp.json()
      replayItems.value = data.items || []
    }
  } catch (e) {
    ElMessage.error(`回放 Fixture 加载失败：${e}`)
  } finally {
    replayLoading.value = false
  }
}

onMounted(() => {
  loadCaptures()
  loadReplayFixtures()
})
</script>

<template>
  <div class="vm-console-audit">
    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <span>虚拟机控制台截图审计（qkv_vm_console）</span>
          <span class="hint">脱敏摘要视图；原图访问须经授权端点并单独记录审计</span>
        </div>
      </template>

      <el-form inline size="small" @submit.prevent="loadCaptures">
        <el-form-item label="工单">
          <el-input v-model="filters.case_id" placeholder="Q202608200001" clearable style="width: 160px" />
        </el-form-item>
        <el-form-item label="VMID">
          <el-input v-model="filters.vm_id" placeholder="精确数值" clearable style="width: 120px" />
        </el-form-item>
        <el-form-item label="状态">
          <el-select v-model="filters.status" clearable filterable style="width: 200px">
            <el-option v-for="s in statusOptions" :key="s" :label="s" :value="s" />
          </el-select>
        </el-form-item>
        <el-form-item label="唤醒">
          <el-select v-model="filters.wake_state" clearable style="width: 170px">
            <el-option v-for="w in wakeOptions" :key="w" :label="w" :value="w" />
          </el-select>
        </el-form-item>
        <el-form-item label="视觉状态">
          <el-select v-model="filters.vision_state" clearable style="width: 160px">
            <el-option v-for="v in visionOptions" :key="v" :label="v" :value="v" />
          </el-select>
        </el-form-item>
        <el-form-item label="Trace">
          <el-input v-model="filters.trace_id" placeholder="trace_id" clearable style="width: 200px" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="page = 1; loadCaptures()">查询</el-button>
          <el-button @click="resetFilters">重置</el-button>
        </el-form-item>
      </el-form>

      <el-table v-loading="loading" :data="items" size="small" border stripe>
        <el-table-column prop="created_at" label="创建时间" width="160" />
        <el-table-column prop="case_id" label="工单" width="150" />
        <el-table-column prop="vm_id" label="VMID" width="80" />
        <el-table-column prop="host_node_id" label="宿主机" min-width="130" show-overflow-tooltip />
        <el-table-column label="模式" width="80">
          <template #default="{ row }">{{ row.mode === 'online' ? '在线' : '离线' }}</template>
        </el-table-column>
        <el-table-column label="状态" width="170">
          <template #default="{ row }">
            <el-tag :type="statusTagType(row.status)" size="small">{{ row.status }}</el-tag>
            <div v-if="row.error_code" class="error-code">{{ row.error_code }}</div>
          </template>
        </el-table-column>
        <el-table-column label="近黑" width="70">
          <template #default="{ row }">
            <el-tag v-if="row.near_black === 'true'" type="warning" size="small">近黑</el-tag>
            <span v-else>—</span>
          </template>
        </el-table-column>
        <el-table-column label="唤醒" width="140">
          <template #default="{ row }">
            {{ row.wake_state }}
            <span v-if="row.wake_confirmed_by" class="muted">（{{ row.wake_confirmed_by }}）</span>
          </template>
        </el-table-column>
        <el-table-column label="视觉观察" min-width="220" show-overflow-tooltip>
          <template #default="{ row }">
            <template v-if="row.vision_state">
              <el-tag size="small">{{ row.vision_state }}</el-tag>
              <span class="muted"> 置信度 {{ row.vision_confidence ?? '—' }}</span>
              <div class="muted">{{ row.vision_summary }}</div>
            </template>
            <span v-else>—</span>
          </template>
        </el-table-column>
        <el-table-column label="制品" width="110">
          <template #default="{ row }">
            基线{{ row.has_baseline ? '✓' : '✗' }} 重截{{ row.has_recapture ? '✓' : '✗' }}
          </template>
        </el-table-column>
        <el-table-column prop="source_kbd_id" label="KBD 来源" width="120" show-overflow-tooltip />
        <el-table-column prop="vision_model_revision" label="模型修订" width="140" show-overflow-tooltip />
        <el-table-column prop="trace_id" label="Trace ID" width="150" show-overflow-tooltip />
        <el-table-column label="操作" width="90" fixed="right">
          <template #default="{ row }">
            <el-button text type="primary" size="small" @click="openEventsDrawer(row.capture_id)">事件流</el-button>
          </template>
        </el-table-column>
      </el-table>

      <el-pagination
        v-model:current-page="page"
        v-model:page-size="pageSize"
        :total="total"
        :page-sizes="[20, 50, 100]"
        layout="total, sizes, prev, pager, next"
        style="margin-top: 12px"
        @current-change="loadCaptures"
        @size-change="page = 1; loadCaptures()"
      />
    </el-card>

    <el-card shadow="never" style="margin-top: 16px">
      <template #header>
        <div class="card-header">
          <span>回放 Fixture（§7.2：确定性回放，不含视觉模型分类）</span>
          <el-button size="small" :loading="replayLoading" @click="loadReplayFixtures">重新回放</el-button>
        </div>
      </template>
      <el-table v-loading="replayLoading" :data="replayItems" size="small" border>
        <el-table-column prop="label" label="场景" width="160" />
        <el-table-column prop="expected_display_state" label="期望画面状态" width="140" />
        <el-table-column label="解析" width="90">
          <template #default="{ row }">{{ row.parse_ok ? '✓' : '✗（预期）' }}</template>
        </el-table-column>
        <el-table-column label="近黑判定" width="170">
          <template #default="{ row }">
            <el-tag :type="row.near_black_matches_expectation ? 'success' : 'danger'" size="small">
              {{ row.near_black ? '近黑' : '非近黑' }}{{ row.near_black_matches_expectation ? '（符合期望）' : '（不符合！）' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="PNG 隔离派生" width="120">
          <template #default="{ row }">{{ row.png_derived ? '✓' : '—' }}</template>
        </el-table-column>
        <el-table-column label="观察 Schema 契约" width="140">
          <template #default="{ row }">{{ row.observation_contract_ok ? '✓' : '✗' }}</template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-drawer v-model="eventsDrawerVisible" :title="`审计事件流（${eventsCaptureId}）`" size="46%">
      <el-timeline v-loading="eventsLoading">
        <el-timeline-item
          v-for="item in eventItems"
          :key="item.event_id"
          :timestamp="item.created_at"
          placement="top"
        >
          <div><el-tag size="small">{{ item.event_type }}</el-tag> <span class="muted">{{ item.actor || 'system' }}</span></div>
          <div v-if="item.detail && Object.keys(item.detail).length" class="muted">{{ JSON.stringify(item.detail) }}</div>
        </el-timeline-item>
        <el-empty v-if="!eventsLoading && eventItems.length === 0" description="暂无审计事件" :image-size="48" />
      </el-timeline>
    </el-drawer>
  </div>
</template>

<style scoped>
.vm-console-audit {
  padding: 16px;
}
.card-header {
  display: flex;
  align-items: baseline;
  gap: 12px;
}
.card-header .hint {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.error-code {
  font-size: 11px;
  color: var(--el-color-danger);
}
.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
