<script setup lang="ts">
/**
 * 日志收集场景维护（管理台）。
 *
 * 默认场景是 acli -m 的固定枚举闭集，能力边界由集群决定，平台只维护文案与清单。
 * 一次只编辑一个场景，因此用二级页签做选择器；不做搜索、筛选、场景列表，
 * 也不提供新增（枚举由集群定义，平台造不出新场景）。
 *
 * 切换页签前必须拦截未保存改动：概要与清单是两次独立提交，
 * 若不拦截，用户改完短名直接点另一个场景，改动会静默丢失。
 */
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import CustomScenarioPanel from './components/CustomScenarioPanel.vue'
import DefaultScenarioPanel from './components/DefaultScenarioPanel.vue'
import LogUploadPanel from './components/LogUploadPanel.vue'
import {
  listScenarios,
  updateScenario,
  type LogCollectScenario,
  type ScenarioUpdatePayload,
} from '@/api/logCollectScenario'

const activeTab = ref<'upload' | 'default' | 'custom'>('default')

const rows = ref<LogCollectScenario[]>([])
const loading = ref(false)
const selectedModule = ref('')
const savingSummary = ref(false)
const summaryDirty = ref(false)
const manifestDirty = ref(false)

const current = computed(
  () => rows.value.find((row) => row.value === selectedModule.value) ?? null,
)
const isDirty = computed(() => summaryDirty.value || manifestDirty.value)

const form = reactive({
  label: '',
  summary: '',
  sort_order: 100,
  enabled: true,
})
let formSnapshot = ''

function snapshotForm() {
  formSnapshot = JSON.stringify({ ...form })
  summaryDirty.value = false
}

function applyCurrent() {
  const row = current.value
  if (!row) return
  Object.assign(form, {
    label: row.label,
    summary: row.summary ?? '',
    sort_order: row.sort_order,
    enabled: row.enabled,
  })
  snapshotForm()
}

watch(
  form,
  () => {
    summaryDirty.value = JSON.stringify({ ...form }) !== formSnapshot
  },
  { deep: true },
)

async function load() {
  loading.value = true
  try {
    rows.value = await listScenarios()
    if (!selectedModule.value || !rows.value.some((row) => row.value === selectedModule.value)) {
      selectedModule.value = rows.value[0]?.value ?? ''
    }
    applyCurrent()
  } catch (error: any) {
    ElMessage.error(`加载场景失败：${error?.message ?? error}`)
  } finally {
    loading.value = false
  }
}

onMounted(load)

async function selectModule(value: string | number) {
  const name = String(value)
  if (name === selectedModule.value) return
  if (isDirty.value) {
    try {
      await ElMessageBox.confirm('当前场景有未保存的修改，切换后将丢失。', '未保存的修改', {
        type: 'warning',
      })
    } catch {
      return
    }
  }
  selectedModule.value = name
  manifestDirty.value = false
  applyCurrent()
}

function replaceRow(updated: LogCollectScenario) {
  const index = rows.value.findIndex((item) => item.value === updated.value)
  if (index >= 0) rows.value[index] = updated
}

async function saveSummary() {
  const row = current.value
  if (!row) return
  const label = form.label.trim()
  const summary = form.summary.trim()
  if (!label) {
    ElMessage.warning('场景短名不能为空')
    return
  }
  if (!summary) {
    ElMessage.warning('一句话释义不能为空')
    return
  }
  const patch: ScenarioUpdatePayload = {
    label,
    summary,
    sort_order: form.sort_order,
    enabled: form.enabled,
  }
  savingSummary.value = true
  try {
    const updated = await updateScenario(row.value, patch)
    replaceRow(updated)
    applyCurrent()
    ElMessage.success('已保存概要')
  } catch (error: any) {
    ElMessage.error(`保存失败：${error?.message ?? error}`)
  } finally {
    savingSummary.value = false
  }
}

// 采集说明、典型症状、耗时提示与互斥收在弹窗里，主界面不占列。
const dialogVisible = ref(false)
const dialogSaving = ref(false)
const detailForm = reactive({
  collects: '',
  typical_symptoms: '',
  duration_hint: '',
  exclusive: false,
})
let detailSnapshot: ScenarioUpdatePayload | null = null

function openDetail() {
  const row = current.value
  if (!row) return
  Object.assign(detailForm, {
    collects: row.collects ?? '',
    typical_symptoms: row.typical_symptoms ?? '',
    duration_hint: row.duration_hint ?? '',
    exclusive: row.exclusive,
  })
  detailSnapshot = {
    collects: row.collects ?? null,
    typical_symptoms: row.typical_symptoms ?? null,
    duration_hint: row.duration_hint ?? null,
    exclusive: row.exclusive,
  }
  dialogVisible.value = true
}

function nullable(text: string): string | null {
  const trimmed = text.trim()
  return trimmed ? trimmed : null
}

async function submitDetail() {
  const row = current.value
  if (!row || !detailSnapshot) return
  const payload: ScenarioUpdatePayload = {
    collects: nullable(detailForm.collects),
    typical_symptoms: nullable(detailForm.typical_symptoms),
    duration_hint: nullable(detailForm.duration_hint),
    exclusive: detailForm.exclusive,
  }
  const patch = Object.fromEntries(
    Object.entries(payload).filter(
      ([key, value]) => (detailSnapshot as Record<string, unknown> | null)?.[key] !== value,
    ),
  ) as ScenarioUpdatePayload
  if (!Object.keys(patch).length) {
    dialogVisible.value = false
    return
  }
  dialogSaving.value = true
  try {
    const updated = await updateScenario(row.value, patch)
    replaceRow(updated)
    dialogVisible.value = false
    ElMessage.success('已保存说明')
  } catch (error: any) {
    ElMessage.error(`保存失败：${error?.message ?? error}`)
  } finally {
    dialogSaving.value = false
  }
}
</script>

<template>
  <div class="scenario-page">
    <el-tabs v-model="activeTab" class="scenario-tabs">
      <el-tab-pane label="日志上传" name="upload" lazy>
        <LogUploadPanel />
      </el-tab-pane>

      <el-tab-pane label="默认场景" name="default">
        <el-tabs
          :model-value="selectedModule"
          class="scenario-subtabs"
          @tab-change="selectModule"
        >
          <el-tab-pane
            v-for="row in rows"
            :key="row.value"
            :label="row.label"
            :name="row.value"
          />
        </el-tabs>

        <el-card v-loading="loading" shadow="never" class="scenario-card">
          <template #header>
            <div class="card-head">
              <span>场景概要</span>
              <div class="card-head-actions">
                <el-button size="small" text type="primary" @click="openDetail">编辑说明</el-button>
                <el-button
                  size="small"
                  type="primary"
                  :loading="savingSummary"
                  :disabled="!summaryDirty"
                  @click="saveSummary"
                >
                  保存概要
                </el-button>
              </div>
            </div>
          </template>

          <div class="summary-grid">
            <div class="summary-field">
              <label>枚举值</label>
              <div class="readonly">{{ current?.value }}</div>
            </div>
            <div class="summary-field">
              <label>场景短名</label>
              <el-input v-model="form.label" maxlength="64" />
            </div>
            <div class="summary-field full">
              <label>一句话释义</label>
              <el-input v-model="form.summary" maxlength="200" />
            </div>
          </div>
          <div class="summary-inline">
            <span class="field-label">排序</span>
            <el-input-number
              v-model="form.sort_order"
              :min="0"
              :max="9999"
              :step="10"
              size="small"
              controls-position="right"
            />
            <span class="field-label inline-gap">启用</span>
            <el-switch v-model="form.enabled" size="small" />
          </div>
        </el-card>

        <DefaultScenarioPanel
          :module="selectedModule"
          @update:dirty="manifestDirty = $event"
        />
      </el-tab-pane>

      <el-tab-pane label="自定义场景" name="custom" lazy>
        <CustomScenarioPanel />
      </el-tab-pane>
    </el-tabs>

    <el-dialog v-model="dialogVisible" title="编辑说明" width="640px" destroy-on-close>
      <el-form label-position="top">
        <el-form-item label="采集内容（换行分隔）">
          <el-input v-model="detailForm.collects" type="textarea" :rows="3" />
        </el-form-item>
        <el-form-item label="典型症状（换行分隔）">
          <el-input v-model="detailForm.typical_symptoms" type="textarea" :rows="3" />
        </el-form-item>
        <el-form-item label="耗时提示">
          <el-input v-model="detailForm.duration_hint" maxlength="100" />
        </el-form-item>
        <el-form-item label="属性">
          <el-checkbox v-model="detailForm.exclusive">与其他场景互斥</el-checkbox>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="dialogSaving" @click="submitDetail">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.scenario-page {
  display: flex;
  flex-direction: column;
}

/* 二级页签与一级页签同构，但用更小的字号/高度与浅底区分层级 */
.scenario-subtabs {
  margin-bottom: 14px;
}
.scenario-subtabs :deep(.el-tabs__header) {
  margin: 0;
  padding: 0 8px;
  background: #fff;
  border: 1px solid #ebeef5;
  border-top: none;
  border-radius: 0 0 4px 4px;
}
.scenario-subtabs :deep(.el-tabs__item) {
  height: 44px;
  line-height: 44px;
  font-size: 13px;
}

.scenario-card {
  border-radius: 6px;
}
.card-head {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 14px;
  font-weight: 600;
}
.card-head-actions {
  margin-left: auto;
  display: flex;
  gap: 8px;
  font-weight: 400;
}

.summary-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px 18px;
}
.summary-field {
  display: flex;
  flex-direction: column;
  gap: 5px;
}
.summary-field.full {
  grid-column: 1 / -1;
}
.summary-field label,
.field-label {
  font-size: 12px;
  color: #909399;
}
.readonly {
  font-family: 'SFMono-Regular', Consolas, monospace;
  font-size: 12.5px;
  color: #606266;
  background: #f5f7fa;
  border-radius: 3px;
  padding: 6px 9px;
}
.summary-inline {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 14px;
}
.inline-gap {
  margin-left: 18px;
}
</style>
