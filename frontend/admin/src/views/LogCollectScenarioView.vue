<script setup lang="ts">
/**
 * 日志收集「故障场景」字典维护（管理台）。
 *
 * 这个页面存在的唯一理由：客户面板上那 10 个勾选项的中文名与说明**不是集群给的**，
 * 是平台自己维护的字典。集群 acli 只决定"哪些枚举真的存在"，所以这里能改文案，
 * 却改不出一个集群不支持的场景来（value 创建后锁死，新建需由支持工程师确认枚举）。
 *
 * 三条编辑口径：
 *  1. `value` 只读：它是 acli `-m` 的枚举值，也是历史任务表里存的值；
 *  2. `label` / `summary` 必填：前者是勾选框第一行，后者是第二行，缺一行用户就读不懂；
 *  3. `collects` / `typical_symptoms` 无 acli 依据，只能人工写，因此带 `review_state`
 *     标注是否已由支持工程师审核，避免把草稿当成官方释义。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import {
  createScenario,
  listScenarios,
  toggleScenario,
  updateScenario,
  type LogCollectScenario,
  type ScenarioReviewState,
  type ScenarioUpdatePayload,
} from '@/api/logCollectScenario'

const REVIEW_TEXT: Record<ScenarioReviewState, string> = {
  pending_review: '待审核',
  approved: '已审核',
}

const rows = ref<LogCollectScenario[]>([])
const loading = ref(false)
const keyword = ref('')
const onlyPending = ref(false)
const onlyDisabled = ref(false)
// 逐行保存标记：同一个表格里多行可能同时保存，用一个集合点亮各自的 loading。
const saving = reactive<Record<string, boolean>>({})

const visibleRows = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  return rows.value.filter((row) => {
    if (onlyPending.value && row.review_state !== 'pending_review') return false
    if (onlyDisabled.value && row.enabled) return false
    if (!kw) return true
    return [row.value, row.label, row.summary ?? ''].some((text) => text.toLowerCase().includes(kw))
  })
})

const enabledCount = computed(() => rows.value.filter((row) => row.enabled).length)
const pendingCount = computed(() => rows.value.filter((row) => row.review_state === 'pending_review').length)

async function load() {
  loading.value = true
  try {
    rows.value = await listScenarios()
  } catch (error: any) {
    ElMessage.error(`加载场景字典失败：${error?.message ?? error}`)
  } finally {
    loading.value = false
  }
}

onMounted(load)

/** 失败后从服务端回读全量，避免界面停留在"看起来存了其实没存"的状态。 */
async function reloadRows() {
  try {
    rows.value = await listScenarios()
  } catch {
    // 回读失败就保持现状：下次保存或刷新还会重新对齐，不必再弹一次错。
  }
}

async function commitField(
  row: LogCollectScenario,
  field: 'label' | 'summary' | 'sort_order',
) {
  const patch: ScenarioUpdatePayload = {}
  if (field === 'label') {
    const label = (row.label ?? '').trim()
    if (!label) {
      ElMessage.warning('场景短名不能为空，已恢复原值')
      await reloadRows()
      return
    }
    patch.label = label
  } else if (field === 'summary') {
    const summary = (row.summary ?? '').trim()
    if (!summary) {
      ElMessage.warning('一句话释义不能为空（勾选框第二行要显示它），已恢复原值')
      await reloadRows()
      return
    }
    patch.summary = summary
  } else {
    patch.sort_order = row.sort_order
  }

  saving[row.value] = true
  try {
    const updated = await updateScenario(row.value, patch)
    replaceRow(updated)
    ElMessage.success(`已保存 ${updated.value}`)
  } catch (error: any) {
    ElMessage.error(`保存失败：${error?.message ?? error}`)
    await reloadRows()
  } finally {
    saving[row.value] = false
  }
}

async function commitSwitch(
  row: LogCollectScenario,
  field: 'enabled' | 'exclusive',
) {
  const previous = !row[field]
  saving[row.value] = true
  try {
    const updated =
      field === 'enabled'
        ? await toggleScenario(row.value, row.enabled)
        : await updateScenario(row.value, { exclusive: row.exclusive })
    replaceRow(updated)
    if (field === 'enabled') {
      ElMessage.success(`${updated.value} 已${updated.enabled ? '启用' : '停用'}`)
    }
  } catch (error: any) {
    row[field] = previous
    ElMessage.error(`保存失败：${error?.message ?? error}`)
  } finally {
    saving[row.value] = false
  }
}

function replaceRow(updated: LogCollectScenario) {
  const index = rows.value.findIndex((item) => item.value === updated.value)
  if (index >= 0) rows.value[index] = updated
  else rows.value.push(updated)
}

// ── 弹窗编辑（collects / typical_symptoms / duration_hint 等长文本）───────────

const dialogVisible = ref(false)
const dialogMode = ref<'create' | 'edit'>('edit')
const dialogSaving = ref(false)
const form = reactive({
  value: '',
  label: '',
  summary: '',
  collects: '',
  typical_symptoms: '',
  duration_hint: '',
  exclusive: false,
  sort_order: 100,
  enabled: true,
  review_state: 'pending_review' as ScenarioReviewState,
})
/** 打开弹窗时的快照，用来算差量补丁（全量提交会覆盖别人刚改的审核状态）。 */
let snapshot: Record<string, unknown> | null = null

function resetForm() {
  snapshot = null
  Object.assign(form, {
    value: '',
    label: '',
    summary: '',
    collects: '',
    typical_symptoms: '',
    duration_hint: '',
    exclusive: false,
    sort_order: 100,
    enabled: true,
    review_state: 'pending_review' as ScenarioReviewState,
  })
}

function openCreate() {
  resetForm()
  dialogMode.value = 'create'
  dialogVisible.value = true
}

function openEdit(row: LogCollectScenario) {
  Object.assign(form, {
    value: row.value,
    label: row.label,
    summary: row.summary ?? '',
    collects: row.collects ?? '',
    typical_symptoms: row.typical_symptoms ?? '',
    duration_hint: row.duration_hint ?? '',
    exclusive: row.exclusive,
    sort_order: row.sort_order,
    enabled: row.enabled,
    review_state: row.review_state,
  })
  snapshot = {
    label: row.label,
    summary: row.summary ?? '',
    collects: row.collects ?? null,
    typical_symptoms: row.typical_symptoms ?? null,
    duration_hint: row.duration_hint ?? null,
    exclusive: row.exclusive,
    sort_order: row.sort_order,
    enabled: row.enabled,
    review_state: row.review_state,
  }
  dialogMode.value = 'edit'
  dialogVisible.value = true
}

/** 空串统一转 null：后端把 null 与 "" 存成两种值会让"有没有填"含糊不清。 */
function nullable(text: string): string | null {
  const trimmed = text.trim()
  return trimmed ? trimmed : null
}

async function submitDialog() {
  const label = form.label.trim()
  const summary = form.summary.trim()
  if (!label) {
    ElMessage.warning('场景短名必填')
    return
  }
  if (!summary) {
    ElMessage.warning('一句话释义必填（勾选框第二行要显示它）')
    return
  }
  if (dialogMode.value === 'create' && !/^[a-z][a-z0-9_]{0,31}$/.test(form.value.trim())) {
    ElMessage.warning('枚举值需为小写字母开头，仅含小写字母、数字与下划线（对应 acli -m 取值）')
    return
  }

  const payload = {
    label,
    summary,
    collects: nullable(form.collects),
    typical_symptoms: nullable(form.typical_symptoms),
    duration_hint: nullable(form.duration_hint),
    exclusive: form.exclusive,
    sort_order: form.sort_order,
    enabled: form.enabled,
    review_state: form.review_state,
  }

  // 编辑时只提交真正改过的字段：多人同时维护时不会把别人的修改覆盖回去
  let patch: ScenarioUpdatePayload = payload
  if (dialogMode.value === 'edit' && snapshot) {
    patch = Object.fromEntries(
      Object.entries(payload).filter(([key, value]) => snapshot?.[key] !== value),
    ) as ScenarioUpdatePayload
    if (!Object.keys(patch).length) {
      dialogVisible.value = false
      ElMessage.info('没有改动')
      return
    }
  }

  dialogSaving.value = true
  try {
    const saved =
      dialogMode.value === 'create'
        ? await createScenario({ value: form.value.trim(), ...payload })
        : await updateScenario(form.value, patch)
    replaceRow(saved)
    dialogVisible.value = false
    ElMessage.success(dialogMode.value === 'create' ? `已新增 ${saved.value}` : `已保存 ${saved.value}`)
    if (dialogMode.value === 'create') await load()
  } catch (error: any) {
    // 409 是 value 已存在（创建）或记录已被删除（更新），两种都需要刷新列表
    ElMessage.error(`保存失败：${error?.message ?? error}`)
  } finally {
    dialogSaving.value = false
  }
}

function formatTime(value: string | null): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}
</script>

<template>
  <div class="scenario-page">
    <el-alert type="info" :closable="false" show-icon class="scenario-intro">
      <template #title>这里是客户面板「故障场景」勾选项的文字来源</template>
      <p>
        勾选项的<b>能力边界</b>由集群 <code>acli log_collect create --help</code> 决定（集群不支持的枚举不会出现在客户端）；
        本页维护的是<b>文案</b>：短名、一句话释义、采集内容、典型症状与耗时提示。
      </p>
      <p>
        共 {{ rows.length }} 条，启用 {{ enabledCount }} 条，待审核 {{ pendingCount }} 条。
        采集内容与典型症状没有 acli 原文可依，需支持工程师审核后把「审核状态」改为已审核。
      </p>
    </el-alert>

    <el-card shadow="never" class="scenario-card">
      <div class="scenario-toolbar">
        <el-input
          v-model="keyword"
          placeholder="按枚举值 / 短名 / 释义搜索"
          clearable
          style="width: 260px"
        />
        <el-checkbox v-model="onlyPending">只看待审核</el-checkbox>
        <el-checkbox v-model="onlyDisabled">只看已停用</el-checkbox>
        <div class="scenario-toolbar-right">
          <el-button size="small" @click="load">刷新</el-button>
          <el-button size="small" type="primary" @click="openCreate">新增场景</el-button>
        </div>
      </div>

      <el-table
        v-loading="loading"
        :data="visibleRows"
        row-key="value"
        size="small"
        border
        empty-text="字典为空：客户端会回落到 bridge 的内置短名"
      >
        <el-table-column label="枚举值（只读）" width="160">
          <template #default="{ row }">
            <el-tooltip content="acli -m 的取值，也是历史任务里存的值，创建后不可修改" placement="top">
              <code class="scenario-value">{{ row.value }}</code>
            </el-tooltip>
          </template>
        </el-table-column>

        <el-table-column label="场景短名" min-width="150">
          <template #default="{ row }">
            <el-input
              v-model="row.label"
              size="small"
              maxlength="64"
              :disabled="saving[row.value]"
              @change="commitField(row, 'label')"
            />
          </template>
        </el-table-column>

        <el-table-column label="一句话释义（勾选框第二行）" min-width="240">
          <template #default="{ row }">
            <el-input
              v-model="row.summary"
              size="small"
              maxlength="200"
              :disabled="saving[row.value]"
              placeholder="未填则客户端只显示短名"
              @change="commitField(row, 'summary')"
            />
          </template>
        </el-table-column>

        <el-table-column label="排序" width="110">
          <template #default="{ row }">
            <el-input-number
              v-model="row.sort_order"
              size="small"
              :min="0"
              :max="9999"
              :step="10"
              controls-position="right"
              :disabled="saving[row.value]"
              @change="commitField(row, 'sort_order')"
            />
          </template>
        </el-table-column>

        <el-table-column label="互斥" width="80">
          <template #default="{ row }">
            <el-tooltip content="勾选后与其它场景互斥（如「不收集上述场景日志」）" placement="top">
              <el-switch
                v-model="row.exclusive"
                size="small"
                :loading="saving[row.value]"
                @change="commitSwitch(row, 'exclusive')"
              />
            </el-tooltip>
          </template>
        </el-table-column>

        <el-table-column label="启用" width="80">
          <template #default="{ row }">
            <el-switch
              v-model="row.enabled"
              size="small"
              :loading="saving[row.value]"
              @change="commitSwitch(row, 'enabled')"
            />
          </template>
        </el-table-column>

        <el-table-column label="审核状态" width="100">
          <template #default="{ row }">
            <el-tag :type="row.review_state === 'approved' ? 'success' : 'warning'" size="small">
              {{ REVIEW_TEXT[row.review_state] ?? row.review_state }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column label="最后修改" width="150">
          <template #default="{ row }">
            <span class="scenario-meta">{{ formatTime(row.updated_at) }}</span>
            <div v-if="row.updated_by" class="scenario-meta">{{ row.updated_by }}</div>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="90" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openEdit(row)">编辑说明</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog
      v-model="dialogVisible"
      :title="dialogMode === 'create' ? '新增故障场景' : `编辑：${form.value}`"
      width="640px"
      destroy-on-close
    >
      <el-form label-width="120px" label-position="top">
        <el-form-item label="枚举值（acli -m 取值，创建后不可改）">
          <el-input v-model="form.value" :disabled="dialogMode === 'edit'" placeholder="例如 vm_suspend" />
        </el-form-item>
        <el-form-item label="场景短名（必填）">
          <el-input v-model="form.label" maxlength="64" show-word-limit placeholder="例如 虚拟机挂起专项" />
        </el-form-item>
        <el-form-item label="一句话释义（必填，勾选框第二行）">
          <el-input v-model="form.summary" maxlength="200" show-word-limit placeholder="例如 挂起/恢复失败后的 VMS 进程与日志" />
        </el-form-item>
        <el-form-item label="采集内容（换行分隔）">
          <el-input v-model="form.collects" type="textarea" :rows="3" placeholder="收集哪些日志、哪个目录" />
        </el-form-item>
        <el-form-item label="典型症状（换行分隔）">
          <el-input
            v-model="form.typical_symptoms"
            type="textarea"
            :rows="3"
            placeholder="帮用户判断该不该选这一项"
          />
        </el-form-item>
        <el-form-item label="耗时提示">
          <el-input v-model="form.duration_hint" maxlength="100" placeholder="例如 单天收集时间约 4 分钟" />
        </el-form-item>
        <el-form-item label="排序（数字越小越靠前）">
          <el-input-number v-model="form.sort_order" :min="0" :max="9999" :step="10" />
        </el-form-item>
        <el-form-item label="属性">
          <el-checkbox v-model="form.exclusive">与其他场景互斥</el-checkbox>
          <el-checkbox v-model="form.enabled">在客户端展示</el-checkbox>
        </el-form-item>
        <el-form-item label="审核状态">
          <el-radio-group v-model="form.review_state">
            <el-radio value="pending_review">待审核</el-radio>
            <el-radio value="approved">已审核（支持工程师确认过文案）</el-radio>
          </el-radio-group>
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="dialogSaving" @click="submitDialog">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.scenario-page {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.scenario-intro p {
  margin: 4px 0 0;
  font-size: 12px;
  line-height: 1.6;
}

.scenario-card {
  border-radius: 6px;
}

.scenario-toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}

.scenario-toolbar-right {
  margin-left: auto;
  display: flex;
  gap: 8px;
}

.scenario-value {
  font-family: 'SFMono-Regular', Consolas, monospace;
  font-size: 12px;
  background: #f5f7fa;
  padding: 1px 6px;
  border-radius: 3px;
}

.scenario-meta {
  font-size: 12px;
  color: #909399;
}
</style>
