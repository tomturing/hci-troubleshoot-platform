<script setup lang="ts">
/**
 * 日志收集「收集脚本」管理面板。
 *
 * 心智模型照抄默认场景：左侧是随镜像发布的平台默认快照（只读），
 * 编辑任一行 → 生成新 revision；恢复默认 → 回到快照。差别只有两点：
 * 1. 脚本有完整版本历史（默认场景只留最新一版）；
 * 2. 允许新建平台自有脚本（设备上原本没有的文件）。
 *
 * 为什么编辑用抽屉而不是弹窗：脚本动辄几百行，弹窗高度会把代码压到不可读。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import {
  createScript,
  deleteScript,
  getScript,
  getScriptRevision,
  importScript,
  listScripts,
  listScriptRevisions,
  resetScript,
  saveScript,
  setScriptEnabled,
  validateScript,
  type LogCollectScriptRevision,
  type LogCollectScriptSummary,
  type LogCollectScriptValidation,
} from '@/api/logCollectScript'

const emit = defineEmits<{ (event: 'changed'): void }>()

const loading = ref(false)
const saving = ref(false)
const scripts = ref<LogCollectScriptSummary[]>([])
const baselineTotal = ref(0)
const baselineSource = ref<'demo' | 'env' | 'missing' | string>('demo')

const editorVisible = ref(false)
const creating = ref(false)
const form = reactive({
  code: '',
  name: '',
  content: '',
  change_note: '',
  interpreter: '',
  source: 'manual' as 'manual' | 'import',
})
const currentRow = ref<LogCollectScriptSummary | null>(null)
const validation = ref<LogCollectScriptValidation | null>(null)

const historyVisible = ref(false)
const revisions = ref<LogCollectScriptRevision[]>([])
const historyCode = ref('')
const revisionViewer = reactive({ visible: false, title: '', content: '' })

const editorTitle = computed(() => {
  if (creating.value) return form.source === 'import' ? '导入脚本' : '新增脚本'
  return `编辑脚本 · ${form.code}`
})
const customizedCount = computed(() => scripts.value.filter((row) => row.customized).length)

function formatBytes(value: number): string {
  if (!value) return '0 B'
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
  return `${(value / 1024 / 1024).toFixed(2)} MB`
}

function rowState(row: LogCollectScriptSummary): { text: string; type: 'info' | 'success' | 'warning' } {
  if (!row.in_baseline) return { text: `平台新增 · v${row.revision}`, type: 'success' }
  if (row.customized) return { text: `已自定义 · v${row.revision}`, type: 'warning' }
  return { text: '平台默认快照', type: 'info' }
}

async function load() {
  loading.value = true
  try {
    const data = await listScripts()
    scripts.value = data.scripts
    baselineTotal.value = data.baseline_total
    baselineSource.value = data.baseline_source
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || error?.message || '加载脚本列表失败')
  } finally {
    loading.value = false
  }
}

function resetForm() {
  form.code = ''
  form.name = ''
  form.content = ''
  form.change_note = ''
  form.interpreter = ''
  form.source = 'manual'
  validation.value = null
  currentRow.value = null
}

function openCreate() {
  resetForm()
  creating.value = true
  editorVisible.value = true
}

function openImport() {
  resetForm()
  creating.value = true
  form.source = 'import'
  editorVisible.value = true
  triggerFilePick()
}

function triggerFilePick() {
  const input = document.createElement('input')
  input.type = 'file'
  input.accept = '.py,.sh,.bash,.txt'
  input.onchange = async () => {
    const file = input.files?.[0]
    if (!file) return
    if (file.size > 4 * 1024 * 1024) {
      ElMessage.error('脚本超过 4MB，不支持导入')
      return
    }
    const text = await file.text()
    form.code = file.name
    form.name = file.name
    form.content = text
    validation.value = null
  }
  input.click()
}

async function openEdit(row: LogCollectScriptSummary) {
  if (row.binary) {
    ElMessage.warning('该文件是二进制或子目录附件，不支持在线编辑')
    return
  }
  resetForm()
  creating.value = false
  currentRow.value = row
  form.code = row.code
  form.name = row.name
  form.interpreter = row.interpreter
  try {
    const detail = await getScript(row.code)
    form.content = detail.content ?? ''
    if (detail.content === null) {
      // 厂商源码不进平台（公开仓库合规），内置脚本只在库里留元数据；
      // 要改它必须由管理员提供内容，否则等于"平台什么都没下发"。
      ElMessage.info('平台不保存内置脚本原文，请在下方粘贴或导入内容后再保存')
    }
    editorVisible.value = true
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || error?.message || '读取脚本内容失败')
  }
}

async function runValidate() {
  validation.value = await validateScript({
    code: form.code,
    content: form.content,
    interpreter: form.interpreter || null,
  })
  return validation.value
}

async function submit() {
  if (!form.code.trim()) {
    ElMessage.warning('请填写文件名')
    return
  }
  if (!creating.value && !form.change_note.trim()) {
    ElMessage.warning('请填写本次变更说明（会进入版本历史）')
    return
  }
  saving.value = true
  try {
    const payload = {
      code: form.code.trim(),
      content: form.content,
      name: form.name || null,
      interpreter: form.interpreter || null,
      change_note: form.change_note || null,
    }
    if (creating.value) {
      if (form.source === 'import') await importScript({ ...payload, source: 'import' })
      else await createScript(payload)
      ElMessage.success('已导入脚本')
    } else {
      await saveScript(payload)
      ElMessage.success('已保存新版本')
    }
    editorVisible.value = false
    await load()
    emit('changed')
  } catch (error: any) {
    const detail = error?.response?.data?.detail
    ElMessage.error(typeof detail === 'string' ? detail : error?.message || '保存失败')
  } finally {
    saving.value = false
  }
}

async function toggle(row: LogCollectScriptSummary) {
  try {
    await setScriptEnabled(row.code, !row.enabled)
    await load()
    emit('changed')
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || '启停失败')
  }
}

async function restore(row: LogCollectScriptSummary) {
  try {
    await ElMessageBox.confirm(
      `恢复后「${row.code}」将重新使用平台默认快照内容，已保存的 ${row.revision} 个版本会保留在历史里。`,
      '恢复默认',
      { type: 'warning' },
    )
  } catch {
    return
  }
  try {
    await resetScript(row.code)
    ElMessage.success('已恢复为平台默认')
    await load()
    emit('changed')
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || '恢复失败')
  }
}

async function remove(row: LogCollectScriptSummary) {
  try {
    await ElMessageBox.confirm(`确定删除平台新增脚本「${row.code}」？该操作不可撤销。`, '删除脚本', {
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    await deleteScript(row.code)
    ElMessage.success('已删除')
    await load()
    emit('changed')
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || '删除失败')
  }
}

async function openHistory(row: LogCollectScriptSummary) {
  historyCode.value = row.code
  historyVisible.value = true
  revisions.value = []
  try {
    revisions.value = await listScriptRevisions(row.code)
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || '读取版本历史失败')
  }
}

async function viewRevision(revision: LogCollectScriptRevision) {
  try {
    const detail = await getScriptRevision(historyCode.value, revision.revision)
    revisionViewer.title = `${historyCode.value} · v${revision.revision}`
    revisionViewer.content = detail.content ?? ''
    revisionViewer.visible = true
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || '读取版本内容失败')
  }
}

onMounted(load)
</script>

<template>
  <div v-loading="loading" class="script-panel">
    <el-card shadow="never">
      <template #header>
        <div class="sp-head">
          <span>收集脚本</span>
          <el-tag size="small" type="info">平台默认 {{ baselineTotal }} 个</el-tag>
          <el-tag v-if="baselineSource === 'demo'" size="small" type="warning">
            基线为演示数据（生产由环境注入真实清单）
          </el-tag>
          <el-tag v-else-if="baselineSource === 'missing'" size="small" type="danger">基线未配置</el-tag>
          <el-tag v-if="customizedCount" size="small" type="warning">已自定义 {{ customizedCount }} 个</el-tag>
          <div class="sp-actions">
            <el-button size="small" @click="load">刷新</el-button>
            <el-button size="small" @click="openImport">导入脚本</el-button>
            <el-button size="small" type="primary" @click="openCreate">新增脚本</el-button>
          </div>
        </div>
      </template>

      <div class="sp-tip">
        内置脚本可编辑（覆写后下发时覆盖节点同名文件，任务结束还原）；恢复默认即退回随镜像发布的快照。
        场景清单里「动作=脚本」的路径从这里选择。
      </div>

      <el-table :data="scripts" size="small" stripe max-height="620">
        <el-table-column prop="code" label="文件名" min-width="220" show-overflow-tooltip />
        <el-table-column prop="name" label="展示名" min-width="160" show-overflow-tooltip />
        <el-table-column prop="interpreter" label="解释器" width="90" />
        <el-table-column label="大小" width="100">
          <template #default="{ row }">{{ formatBytes(row.content_bytes) }}</template>
        </el-table-column>
        <el-table-column label="版本" width="170">
          <template #default="{ row }">
            <el-tag size="small" :type="rowState(row).type">{{ rowState(row).text }}</el-tag>
            <el-tag v-if="row.binary" size="small" type="danger" class="sp-gap">二进制</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="更新时间" width="170">
          <template #default="{ row }">{{ row.updated_at ? row.updated_at.replace('T', ' ').slice(0, 19) : '—' }}</template>
        </el-table-column>
        <el-table-column label="启用" width="70">
          <template #default="{ row }">
            <el-switch :model-value="row.enabled" size="small" @change="toggle(row)" />
          </template>
        </el-table-column>
        <el-table-column label="操作" width="250" fixed="right">
          <template #default="{ row }">
            <el-button size="small" text type="primary" :disabled="row.binary" @click="openEdit(row)">
              编辑
            </el-button>
            <el-button size="small" text @click="openHistory(row)">版本历史</el-button>
            <el-button
              v-if="row.in_baseline"
              size="small"
              text
              :disabled="!row.customized"
              @click="restore(row)"
            >
              恢复默认
            </el-button>
            <el-button v-else size="small" text type="danger" @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-drawer v-model="editorVisible" :title="editorTitle" size="72%" destroy-on-close>
      <el-form label-width="90px" class="sp-form">
        <el-form-item label="文件名">
          <el-input v-model="form.code" :disabled="!creating" placeholder="例如 pl_disk_check.py" />
          <div class="sp-hint">
            落盘到节点 script 目录的文件名，也是场景清单里「脚本」动作的路径；带 pl_ 前缀可避免与内置脚本混淆。
          </div>
        </el-form-item>
        <el-form-item label="展示名">
          <el-input v-model="form.name" maxlength="64" placeholder="可选，便于识别" />
        </el-form-item>
        <el-form-item label="解释器">
          <el-tag size="small">{{ form.interpreter || '按后缀自动识别' }}</el-tag>
          <span class="sp-hint inline">.py → python，.sh → bash；与后缀不一致会被拒绝</span>
        </el-form-item>
        <el-form-item label="变更说明">
          <el-input
            v-model="form.change_note"
            maxlength="200"
            :placeholder="creating ? '可选，默认「初始版本」' : '必填，会写入版本历史'"
          />
        </el-form-item>
      </el-form>

      <el-input v-model="form.content" type="textarea" :rows="24" class="sp-editor" spellcheck="false" />

      <div v-if="validation" class="sp-validation" :class="{ ok: validation.ok }">
        <template v-if="validation.ok">
          校验通过：{{ formatBytes(validation.content_bytes) }}，sha256 {{ validation.content_sha256.slice(0, 16) }}…
        </template>
        <template v-else>
          <div v-for="(issue, index) in validation.issues" :key="index">{{ issue }}</div>
        </template>
      </div>

      <template #footer>
        <el-button @click="editorVisible = false">关闭</el-button>
        <el-button @click="runValidate">校验</el-button>
        <el-button type="primary" :loading="saving" @click="submit">
          {{ creating ? '保存' : '保存新版本' }}
        </el-button>
      </template>
    </el-drawer>

    <el-drawer v-model="historyVisible" :title="`版本历史 · ${historyCode}`" size="60%" destroy-on-close>
      <el-table :data="revisions" size="small" max-height="560">
        <el-table-column label="版本" width="80">
          <template #default="{ row }">v{{ row.revision }}</template>
        </el-table-column>
        <el-table-column label="时间" width="170">
          <template #default="{ row }">
            {{ row.created_at ? row.created_at.replace('T', ' ').slice(0, 19) : '—' }}
          </template>
        </el-table-column>
        <el-table-column prop="created_by" label="操作人" width="120" show-overflow-tooltip />
        <el-table-column prop="change_note" label="变更说明" min-width="200" show-overflow-tooltip />
        <el-table-column label="大小" width="90">
          <template #default="{ row }">{{ formatBytes(row.content_bytes) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="90">
          <template #default="{ row }">
            <el-button size="small" text type="primary" @click="viewRevision(row)">查看</el-button>
          </template>
        </el-table-column>
      </el-table>
      <div v-if="!revisions.length" class="sp-empty">该脚本仍是平台默认快照，尚无平台保存的版本。</div>
    </el-drawer>

    <el-dialog v-model="revisionViewer.visible" :title="revisionViewer.title" width="80%" destroy-on-close>
      <pre class="sp-preview">{{ revisionViewer.content }}</pre>
      <template #footer>
        <el-button @click="revisionViewer.visible = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.script-panel {
  padding-top: 14px;
}
.sp-head {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 14px;
  font-weight: 600;
}
.sp-actions {
  margin-left: auto;
  display: flex;
  gap: 8px;
  font-weight: 400;
}
.sp-tip {
  color: #6b7280;
  font-size: 12px;
  line-height: 1.7;
  margin-bottom: 10px;
}
.sp-gap {
  margin-left: 6px;
}
.sp-form {
  margin-bottom: 8px;
}
.sp-hint {
  color: #909399;
  font-size: 12px;
  line-height: 1.6;
  margin-top: 4px;
}
.sp-hint.inline {
  margin: 0 0 0 8px;
}
.sp-editor :deep(textarea) {
  font-family: 'JetBrains Mono', Consolas, Menlo, monospace;
  font-size: 12px;
}
.sp-validation {
  margin-top: 8px;
  color: #b91c1c;
  font-size: 13px;
}
.sp-validation.ok {
  color: #15803d;
}
.sp-empty {
  color: #909399;
  font-size: 13px;
  padding: 12px 4px;
}
.sp-preview {
  max-height: 480px;
  overflow: auto;
  background: #0f172a;
  color: #e2e8f0;
  padding: 12px;
  border-radius: 4px;
  font-size: 12px;
  line-height: 1.6;
}
</style>
