<script setup lang="ts">
/**
 * 自定义场景（acli `-c`）管理面板。
 *
 * 第一性原理：`-c` 的物理形态是"节点上一个 JSON 文件"，所以这里维护的不是文案，
 * 而是一份会被下发的**路径清单**。三条由此衍生的硬规则：
 *  1. 只允许字面绝对路径或 `{date}` 占位：config 的 paths 不支持通配符，
 *     `*` 会被 `os.path.exists` 判假后静默跳过——收不到且没有任何报错；
 *  2. 保存前必须试算（条数/字节数），让"会不会超限"在保存前可见；
 *  3. 编辑不覆盖历史：后端每次生成新 revision，本页只展示当前最新版。
 *
 * 为什么暂不提供"从清单勾选"：候选清单必须来自被采集集群的 `spc/*.json`，
 * 由 bridge probe 读取；在 PR3 接入前，手工录入 + 批量粘贴是唯一不撒谎的交互。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import {
  createCustomScenario,
  deleteCustomScenario,
  listCustomScenarios,
  previewConfigText,
  updateCustomScenario,
  validateCustomScenario,
  type CustomScenario,
  type CustomScenarioCategory,
  type CustomScenarioItem,
  type CustomScenarioReviewState,
} from '@/api/logCollectCustomScenario'

const rows = ref<CustomScenario[]>([])
const loading = ref(false)
const saving = ref(false)
const drawerVisible = ref(false)
const editing = ref<CustomScenario | null>(null)

const form = reactive({
  code: '',
  label: '',
  summary: '',
  applies_to: '',
  sort_order: 100,
  enabled: true,
  review_state: 'pending_review' as CustomScenarioReviewState,
})

const items = ref<CustomScenarioItem[]>([])
const bulkText = ref('')
const bulkError = ref('')

const isEdit = computed(() => Boolean(editing.value))
const previewText = computed(() => previewConfigText(items.value))
const previewBytes = computed(() => new TextEncoder().encode(previewText.value).length)

const CATEGORY_TEXT: Record<CustomScenarioCategory, string> = {
  log: '日志',
  config: '配置',
  file: '其他文件',
}

async function load() {
  loading.value = true
  try {
    rows.value = await listCustomScenarios()
  } catch (error: any) {
    ElMessage.error(`加载自定义场景失败：${error?.message ?? error}`)
  } finally {
    loading.value = false
  }
}

onMounted(load)

function resetForm() {
  Object.assign(form, {
    code: '',
    label: '',
    summary: '',
    applies_to: '',
    sort_order: 100,
    enabled: true,
    review_state: 'pending_review' as CustomScenarioReviewState,
  })
  items.value = []
  bulkText.value = ''
  bulkError.value = ''
}

function openCreate() {
  editing.value = null
  resetForm()
  drawerVisible.value = true
}

function openEdit(row: CustomScenario) {
  editing.value = row
  Object.assign(form, {
    code: row.code,
    label: row.label,
    summary: row.summary ?? '',
    applies_to: row.applies_to ?? '',
    sort_order: row.sort_order,
    enabled: row.enabled,
    review_state: row.review_state,
  })
  items.value = row.items.map((item) => ({ ...item }))
  bulkText.value = ''
  bulkError.value = ''
  drawerVisible.value = true
}

/** 与 bridge / 后端同源的轻校验：前端只为即时反馈，真防线在服务端与 bridge。 */
function pathError(raw: string): string {
  const value = raw.trim()
  if (!value) return '路径不能为空'
  if (!value.startsWith('/')) return '必须是绝对路径（以 / 开头）'
  if (value.includes('..')) return '不允许包含 ..'
  if (value.includes(',')) return '不能包含逗号'
  const probe = value.replace(/\{date\}/g, 'D')
  if (!/^\/[A-Za-z0-9_./-]+$/.test(probe)) return '只允许字母、数字、下划线、点、斜杠、短横线与 {date}'
  return ''
}

function addBulk() {
  bulkError.value = ''
  const candidates = bulkText.value
    .split(/[\n,]/)
    .map((item) => item.trim())
    .filter(Boolean)
  if (!candidates.length) return

  const existing = new Set(items.value.map((item) => item.path))
  const rejected: string[] = []
  let added = 0
  for (const candidate of candidates) {
    const reason = pathError(candidate)
    if (reason) {
      rejected.push(`${candidate}：${reason}`)
      continue
    }
    if (existing.has(candidate)) continue
    existing.add(candidate)
    items.value.push({ path: candidate, category: 'log', note: null })
    added += 1
  }
  bulkText.value = ''
  if (rejected.length) {
    bulkError.value = `已忽略 ${rejected.length} 条：${rejected.slice(0, 3).join('；')}`
  }
  if (added === 0 && !rejected.length) {
    bulkError.value = '没有新增路径（可能全部重复）'
  }
}

function removeItem(index: number) {
  items.value.splice(index, 1)
}

async function submit() {
  const label = form.label.trim()
  if (!label) {
    ElMessage.warning('场景名称必填')
    return
  }
  if (!isEdit.value && !/^[a-z][a-z0-9_]{1,31}$/.test(form.code.trim())) {
    ElMessage.warning('场景标识需为小写字母开头，仅含小写字母、数字与下划线')
    return
  }
  if (!items.value.length) {
    ElMessage.warning('至少添加一条收集路径')
    return
  }

  const payload = {
    code: form.code.trim(),
    label,
    summary: form.summary.trim() || null,
    applies_to: form.applies_to.trim() || null,
    items: items.value,
    enabled: form.enabled,
    sort_order: form.sort_order,
    review_state: form.review_state,
  }

  saving.value = true
  try {
    // 保存前试算：把"多大一份配置"变成保存前就能看到的事实，而不是保存失败后再猜
    const validation = await validateCustomScenario(payload)
    if (validation.config_bytes > 256 * 1024) {
      ElMessage.error(`配置过大（${validation.config_bytes} 字节），请精简路径后再保存`)
      return
    }
    if (isEdit.value && editing.value) {
      await updateCustomScenario(editing.value.code, payload)
      ElMessage.success(`已保存，当前为第 ${editing.value.revision + 1} 版`)
    } else {
      await createCustomScenario(payload)
      ElMessage.success('已创建自定义场景')
    }
    drawerVisible.value = false
    await load()
  } catch (error: any) {
    ElMessage.error(`保存失败：${error?.response?.data?.detail ?? error?.message ?? error}`)
  } finally {
    saving.value = false
  }
}

async function toggle(row: CustomScenario) {
  try {
    await updateCustomScenario(row.code, { enabled: !row.enabled })
    await load()
  } catch (error: any) {
    ElMessage.error(`操作失败：${error?.response?.data?.detail ?? error?.message ?? error}`)
  }
}

async function remove(row: CustomScenario) {
  try {
    await ElMessageBox.confirm(
      `删除「${row.label}」后不可恢复；若该场景已被历史任务引用，平台会拒绝删除并要求改为停用。`,
      '确认删除',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await deleteCustomScenario(row.code)
    ElMessage.success('已删除')
    await load()
  } catch (error: any) {
    ElMessage.error(`删除失败：${error?.response?.data?.detail ?? error?.message ?? error}`)
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
  <div class="custom-scenario">
    <el-alert type="info" :closable="false" show-icon class="cs-intro">
      <template #title>自定义场景 = 一份下发给 acli 的路径清单（<code>-c</code>）</template>
      <p>
        客户在面板上选择后，平台会把这组路径写入节点并以 <code>-c</code> 引用。
        <b>它只追加，不替代专项</b>：acli 的 config.modules 永不生效，专项日志仍会照常收集。
      </p>
      <p>
        只支持<b>字面绝对路径</b>与 <code>{date}</code> 占位；不支持通配符 ——
        <code>*</code> 会被插件静默跳过，任务仍显示成功但少收了内容。
      </p>
    </el-alert>

    <el-card shadow="never">
      <div class="cs-toolbar">
        <el-input v-model="bulkText" placeholder="快速粘贴多条路径（逗号或换行分隔）" style="width: 320px" />
        <el-button @click="load">刷新</el-button>
        <div class="cs-toolbar-right">
          <el-button type="primary" @click="openCreate">新增自定义场景</el-button>
        </div>
      </div>

      <el-table
        v-loading="loading"
        :data="rows"
        row-key="code"
        size="small"
        border
        empty-text="还没有自定义场景：客户面板只会显示默认场景"
      >
        <el-table-column label="场景名称" min-width="200">
          <template #default="{ row }">
            <div class="cs-name">{{ row.label }}</div>
            <div class="cs-sub">{{ row.summary || '未填写释义' }}</div>
          </template>
        </el-table-column>
        <el-table-column label="标识（只读）" width="180">
          <template #default="{ row }">
            <code class="cs-code">{{ row.code }}</code>
          </template>
        </el-table-column>
        <el-table-column label="路径数" width="90">
          <template #default="{ row }">{{ row.item_count }}</template>
        </el-table-column>
        <el-table-column label="版本" width="80">
          <template #default="{ row }">v{{ row.revision }}</template>
        </el-table-column>
        <el-table-column label="状态" width="80">
          <template #default="{ row }">
            <el-tag :type="row.enabled ? 'success' : 'info'" size="small">
              {{ row.enabled ? '启用' : '停用' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="最后修改" width="150">
          <template #default="{ row }">
            <div class="cs-sub">{{ formatTime(row.updated_at) }}</div>
            <div class="cs-sub">{{ row.updated_by || '—' }}</div>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="150" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openEdit(row)">编辑</el-button>
            <el-button link size="small" @click="toggle(row)">{{ row.enabled ? '停用' : '启用' }}</el-button>
            <el-button link type="danger" size="small" @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-drawer
      v-model="drawerVisible"
      :title="isEdit ? `编辑：${form.code}` : '新增自定义场景'"
      size="62%"
      destroy-on-close
    >
      <div class="cs-editor">
        <div class="cs-form">
          <el-form label-position="top" size="small">
            <el-form-item label="场景名称（客户勾选项第一行）">
              <el-input v-model="form.label" maxlength="64" placeholder="例如：存储链路完整取证" />
            </el-form-item>
            <el-form-item label="场景标识（创建后不可改）">
              <el-input v-model="form.code" :disabled="isEdit" placeholder="例如：cs_storage_full" />
            </el-form-item>
            <el-form-item label="一句话释义（客户勾选项第二行）">
              <el-input v-model="form.summary" maxlength="200" placeholder="例如：卡慢盘 + 存储挂起时使用" />
            </el-form-item>
            <el-form-item label="适用说明（选填）">
              <el-input v-model="form.applies_to" maxlength="200" placeholder="例如：仅适用 aSV 6.8 及以上" />
            </el-form-item>
            <el-form-item label="排序 / 状态 / 审核">
              <div class="cs-row">
                <el-input-number v-model="form.sort_order" :min="0" :max="9999" :step="10" controls-position="right" />
                <el-switch v-model="form.enabled" active-text="启用" />
                <el-radio-group v-model="form.review_state">
                  <el-radio value="pending_review">待审核</el-radio>
                  <el-radio value="approved">已审核</el-radio>
                </el-radio-group>
              </div>
            </el-form-item>
          </el-form>
        </div>

        <div class="cs-items">
          <div class="cs-items-head">
            <span>收集路径（{{ items.length }} 条）</span>
            <span class="cs-sub">预计下发 {{ previewBytes }} 字节</span>
          </div>
          <div class="cs-bulk">
            <el-input
              v-model="bulkText"
              type="textarea"
              :rows="2"
              placeholder="每行一条绝对路径，或用英文逗号分隔；支持 {date} 占位"
            />
            <el-button type="primary" plain @click="addBulk">批量添加</el-button>
          </div>
          <div v-if="bulkError" class="cs-error">{{ bulkError }}</div>

          <el-table :data="items" size="small" border empty-text="还没有路径" max-height="320">
            <el-table-column label="路径" min-width="260">
              <template #default="{ row, $index }">
                <el-input v-model="row.path" size="small" @change="$index >= 0 && (row.path = row.path.trim())" />
              </template>
            </el-table-column>
            <el-table-column label="分类" width="120">
              <template #default="{ row }">
                <el-select v-model="row.category" size="small">
                  <el-option v-for="(text, value) in CATEGORY_TEXT" :key="value" :label="text" :value="value" />
                </el-select>
              </template>
            </el-table-column>
            <el-table-column label="备注" width="160">
              <template #default="{ row }">
                <el-input v-model="row.note" size="small" maxlength="100" placeholder="选填" />
              </template>
            </el-table-column>
            <el-table-column label="" width="60">
              <template #default="{ $index }">
                <el-button link type="danger" size="small" @click="removeItem($index)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>

          <div class="cs-preview">
            <div class="cs-items-head"><span>下发内容预览（平台固定只写 paths）</span></div>
            <pre>{{ previewText }}</pre>
          </div>
        </div>
      </div>

      <template #footer>
        <el-button @click="drawerVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submit">保存</el-button>
      </template>
    </el-drawer>
  </div>
</template>

<style scoped>
.custom-scenario {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.cs-intro p {
  margin: 4px 0 0;
  font-size: 12px;
  line-height: 1.6;
}
.cs-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}
.cs-toolbar-right {
  margin-left: auto;
}
.cs-name {
  font-weight: 600;
}
.cs-sub {
  font-size: 11.5px;
  color: var(--el-text-color-secondary);
  line-height: 1.5;
}
.cs-code {
  font-family: Consolas, Monaco, monospace;
  font-size: 11.5px;
  background: var(--el-fill-color-light);
  padding: 1px 6px;
  border-radius: 3px;
}
.cs-editor {
  display: grid;
  grid-template-columns: 300px 1fr;
  gap: 16px;
}
.cs-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.cs-items-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 12px;
  font-weight: 600;
  margin-bottom: 8px;
}
.cs-bulk {
  display: flex;
  gap: 8px;
  align-items: flex-start;
  margin-bottom: 6px;
}
.cs-error {
  color: var(--el-color-danger);
  font-size: 11.5px;
  line-height: 1.5;
  margin-bottom: 6px;
}
.cs-preview {
  margin-top: 14px;
}
.cs-preview pre {
  margin: 0;
  padding: 10px;
  background: #1e1f22;
  color: #abb2bf;
  border-radius: 4px;
  font-family: Consolas, Monaco, monospace;
  font-size: 11.5px;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 120px;
  overflow: auto;
}
</style>
