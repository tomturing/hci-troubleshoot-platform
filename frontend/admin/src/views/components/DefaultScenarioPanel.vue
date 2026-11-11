<script setup lang="ts">
/**
 * 默认场景清单编辑器（按 acli 模块）。
 *
 * 平台只保存被改过的模块；未改过的模块展示随镜像发布的快照。
 * 保存后，默认场景采集会按平台清单逐模块覆写节点同名清单文件，
 * 并在任务终态逐槽位还原。
 */
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  listDefaultManifests,
  resetDefaultManifest,
  saveDefaultManifest,
  validateDefaultManifest,
  type DefaultScenarioItem,
  type DefaultScenarioManifest,
  type DefaultScenarioValidation,
} from '@/api/logCollectDefaultScenario'

const manifests = ref<DefaultScenarioManifest[]>([])
const selected = ref('')
const items = ref<DefaultScenarioItem[]>([])
const loading = ref(false)
const saving = ref(false)
const validation = ref<DefaultScenarioValidation | null>(null)
const previewVisible = ref(false)

const current = computed(
  () => manifests.value.find((item) => item.module === selected.value) ?? null,
)

async function load() {
  loading.value = true
  try {
    const data = await listDefaultManifests()
    manifests.value = data.manifests
    if (!selected.value || !manifests.value.some((item) => item.module === selected.value)) {
      selected.value = manifests.value[0]?.module ?? ''
    }
    applyCurrent()
  } catch (error: any) {
    ElMessage.error(error?.message || '加载默认场景清单失败')
  } finally {
    loading.value = false
  }
}

// 深拷贝一份再编辑：直接改列表里的对象会让"取消"无从谈起。
function applyCurrent() {
  items.value = (current.value?.items ?? []).map((item) => ({ ...item }))
  validation.value = null
}

function addItem() {
  items.value.push({ action: 'copy', path: '', category: 'log', note: '' })
}

function removeItem(index: number) {
  items.value.splice(index, 1)
}

async function validate(): Promise<boolean> {
  try {
    validation.value = await validateDefaultManifest(items.value)
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || error?.message || '校验失败')
    return false
  }
  if (!validation.value?.ok) {
    ElMessage.warning(validation.value?.issues.join('；') || '清单校验未通过')
    return false
  }
  previewVisible.value = true
  return true
}

async function save() {
  if (!selected.value || !(await validate())) return
  saving.value = true
  try {
    await saveDefaultManifest(selected.value, items.value)
    ElMessage.success(`已保存「${current.value?.label ?? selected.value}」的默认清单`)
    await load()
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || error?.message || '保存失败')
  } finally {
    saving.value = false
  }
}

async function reset() {
  if (!selected.value) return
  try {
    await ElMessageBox.confirm(
      `恢复后「${current.value?.label ?? selected.value}」将重新使用平台镜像快照的默认清单，已有历史任务不受影响。`,
      '恢复默认清单',
      { type: 'warning' },
    )
  } catch {
    return
  }
  try {
    await resetDefaultManifest(selected.value)
    ElMessage.success('已恢复为默认清单')
    await load()
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || error?.message || '恢复失败')
  }
}

onMounted(load)
</script>

<template>
  <div v-loading="loading" class="default-scenario-panel">
    <el-alert
      type="info"
      :closable="false"
      show-icon
      title="默认场景的默认收集清单可编辑"
      description="改动会对后续采集生效：平台按模块下发同名清单文件到节点，采集结束后逐槽位还原。未改动的模块继续使用平台默认快照。"
    />

    <div class="ds-toolbar">
      <span class="ds-label">默认场景</span>
      <el-select v-model="selected" style="width: 280px" @change="applyCurrent">
        <el-option
          v-for="manifest in manifests"
          :key="manifest.module"
          :label="`${manifest.label}（${manifest.module}）`"
          :value="manifest.module"
        />
      </el-select>
      <el-tag v-if="current?.customized" type="warning">已自定义 · v{{ current.revision }}</el-tag>
      <el-tag v-else type="info">平台默认快照</el-tag>
      <span v-if="current" class="ds-meta">
        {{ current.item_count }} 条 · 摘要 {{ current.sha256.slice(0, 12) }}
      </span>
    </div>

    <div class="ds-hint">
      路径为该模块在节点上的日志或配置文件；动作与过滤条件沿用平台既有场景校验。
      保存前会展示服务端真实序列化产物，编辑器里看到什么就下发什么。
    </div>

    <div class="ds-table-head">
      <span>归类</span><span>动作</span><span>路径</span><span>分组备注</span><span />
    </div>
    <div v-for="(item, index) in items" :key="index" class="ds-row">
      <el-select v-model="item.category" size="small">
        <el-option label="日志" value="log" />
        <el-option label="配置" value="config" />
        <el-option label="其他文件" value="file" />
      </el-select>
      <el-select v-model="item.action" size="small">
        <el-option label="复制" value="copy" />
        <el-option label="命令" value="command" />
        <el-option label="脚本" value="script" />
      </el-select>
      <el-input v-model="item.path" size="small" placeholder="日志或配置文件的绝对路径 / 通配路径" />
      <el-input v-model="item.note" size="small" placeholder="可选：分组备注" />
      <el-button size="small" @click="removeItem(index)">移除</el-button>
    </div>
    <el-button class="ds-add" @click="addItem">新增收集项</el-button>

    <div class="ds-actions">
      <el-button @click="validate">校验并预览</el-button>
      <el-button type="primary" :loading="saving" :disabled="!selected" @click="save">保存清单</el-button>
      <el-button :disabled="!current?.customized" @click="reset">恢复默认</el-button>
    </div>

    <div v-if="validation && !validation.ok" class="ds-validation ds-validation-error">
      <div v-for="(issue, index) in validation.issues" :key="index">{{ issue }}</div>
    </div>

    <el-dialog v-model="previewVisible" title="将下发的清单（只读）" width="720px">
      <pre class="ds-preview">{{ validation?.preview_text }}</pre>
      <template #footer>
        <el-button @click="previewVisible = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.default-scenario-panel {
  padding: 8px 4px 24px;
}
.ds-toolbar,
.ds-actions {
  display: flex;
  align-items: center;
  gap: 12px;
  margin: 16px 0;
  flex-wrap: wrap;
}
.ds-label {
  font-weight: 600;
}
.ds-meta,
.ds-hint {
  color: #6b7280;
  font-size: 12px;
}
.ds-hint {
  line-height: 1.7;
}
.ds-table-head,
.ds-row {
  display: grid;
  grid-template-columns: 110px 110px minmax(240px, 1fr) minmax(160px, 0.7fr) 72px;
  gap: 8px;
  align-items: center;
  margin-bottom: 8px;
}
.ds-table-head {
  color: #6b7280;
  font-size: 12px;
  margin-top: 16px;
}
.ds-validation-error {
  color: #b91c1c;
  font-size: 13px;
}
.ds-preview {
  max-height: 420px;
  overflow: auto;
  background: #f6f8fa;
  padding: 12px;
  border-radius: 6px;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
