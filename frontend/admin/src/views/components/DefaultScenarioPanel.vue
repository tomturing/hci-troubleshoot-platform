<script setup lang="ts">
/**
 * 默认场景清单（按 acli 模块）。
 *
 * 模块由父页面的二级页签指定，本组件只负责当前模块的清单编辑；
 * 未改过的模块展示随镜像发布的快照，恢复默认 = 删除覆写行。
 * 编辑态通过 update:dirty 上报，供父页面在切换场景时拦截未保存改动。
 */
import { computed, onMounted, ref, watch } from 'vue'
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

const props = defineProps<{ module: string }>()
const emit = defineEmits<{ (e: 'update:dirty', value: boolean): void }>()

const manifests = ref<DefaultScenarioManifest[]>([])
const items = ref<DefaultScenarioItem[]>([])
const loading = ref(false)
const saving = ref(false)
const validation = ref<DefaultScenarioValidation | null>(null)
const previewVisible = ref(false)
let itemsSnapshot = ''

const current = computed(
  () => manifests.value.find((item) => item.module === props.module) ?? null,
)

function applyCurrent() {
  items.value = (current.value?.items ?? []).map((item) => ({ ...item }))
  itemsSnapshot = JSON.stringify(items.value)
  validation.value = null
  emit('update:dirty', false)
}

async function load() {
  if (!props.module) return
  loading.value = true
  try {
    manifests.value = (await listDefaultManifests()).manifests
    applyCurrent()
  } catch (error: any) {
    ElMessage.error(error?.message || '加载默认场景清单失败')
  } finally {
    loading.value = false
  }
}

onMounted(load)
watch(() => props.module, load)
watch(
  items,
  () => {
    emit('update:dirty', JSON.stringify(items.value) !== itemsSnapshot)
  },
  { deep: true },
)

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
  if (!props.module || !(await validate())) return
  saving.value = true
  try {
    await saveDefaultManifest(props.module, items.value)
    ElMessage.success('已保存清单')
    await load()
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || error?.message || '保存失败')
  } finally {
    saving.value = false
  }
}

async function reset() {
  if (!props.module) return
  try {
    await ElMessageBox.confirm('恢复后该场景将重新使用平台默认清单。', '恢复默认清单', {
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    await resetDefaultManifest(props.module)
    ElMessage.success('已恢复为默认清单')
    await load()
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || error?.message || '恢复失败')
  }
}
</script>

<template>
  <div v-loading="loading" class="default-scenario-panel">
    <el-card shadow="never">
      <template #header>
        <div class="ds-head">
          <span>默认收集清单</span>
          <el-tag v-if="current?.customized" type="warning" size="small">
            已自定义 · v{{ current.revision }}
          </el-tag>
          <el-tag v-else type="info" size="small">平台默认快照</el-tag>
          <span class="ds-meta">{{ current?.item_count ?? 0 }} 条</span>
          <div class="ds-actions">
            <el-button size="small" :disabled="!props.module" @click="validate">校验并预览</el-button>
            <el-button
              size="small"
              type="primary"
              :loading="saving"
              :disabled="!props.module"
              @click="save"
            >
              保存清单
            </el-button>
            <el-button size="small" :disabled="!current?.customized" @click="reset">
              恢复默认
            </el-button>
          </div>
        </div>
      </template>

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

      <div v-if="validation && !validation.ok" class="ds-validation-error">
        <div v-for="(issue, index) in validation.issues" :key="index">{{ issue }}</div>
      </div>
    </el-card>

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
  padding-top: 14px;
}
.ds-head {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 14px;
  font-weight: 600;
}
.ds-actions {
  margin-left: auto;
  display: flex;
  gap: 8px;
  font-weight: 400;
}
.ds-meta {
  color: #909399;
  font-size: 12px;
  font-weight: 400;
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
}
.ds-add {
  margin-top: 6px;
}
.ds-validation-error {
  color: #b91c1c;
  font-size: 13px;
  margin-top: 10px;
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
