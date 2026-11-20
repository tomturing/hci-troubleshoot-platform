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
// 目录与文件名分开存：code 是相对 script/ 的路径，内置脚本就在 vs_script/ 等子目录里。
// 只让用户填"文件名"等于永远改不了子目录脚本，只让填完整路径又容易漏校验。
const form = reactive({
  dir: '',
  fileName: '',
  name: '',
  content: '',
  change_note: '',
  interpreter: '',
  source: 'manual' as 'manual' | 'import',
})
const pickedFile = ref<{ name: string; size: number } | null>(null)
const dragging = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)
const currentRow = ref<LogCollectScriptSummary | null>(null)
const validation = ref<LogCollectScriptValidation | null>(null)

const formCode = computed(() => {
  const dir = form.dir.trim().replace(/^\/+|\/+$/g, '')
  const name = form.fileName.trim()
  if (!name) return ''
  return dir ? `${dir}/${name}` : name
})

// 目录候选来自现有脚本（含内置脚本的子目录），空值代表顶层；允许手输新目录。
const dirOptions = computed(() => {
  const dirs = new Set<string>([''])
  for (const row of scripts.value) {
    const index = row.code.lastIndexOf('/')
    if (index > 0) dirs.add(row.code.slice(0, index))
  }
  return [...dirs].map((value) => ({ value, label: value || '（顶层 script 目录）' }))
})

const historyVisible = ref(false)
const revisions = ref<LogCollectScriptRevision[]>([])
const historyCode = ref('')
const revisionViewer = reactive({ visible: false, title: '', content: '' })

const editorTitle = computed(() => {
  if (creating.value) return form.source === 'import' ? '导入脚本' : '新增脚本'
  return `编辑脚本 · ${formCode.value}`
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
  form.dir = ''
  form.fileName = ''
  form.name = ''
  form.content = ''
  form.change_note = ''
  form.interpreter = ''
  form.source = 'manual'
  pickedFile.value = null
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
  // 不再自动弹系统文件框：那个框关掉后用户就只剩"关抽屉重来"一条路，
  // 改为抽屉内常驻「点击选择文件 / 拖拽到此处」的投放区。
}

function triggerFilePick() {
  fileInput.value?.click()
}

async function applyFile(file: File) {
  if (file.size > 4 * 1024 * 1024) {
    ElMessage.error('脚本超过 4MB，不支持导入')
    return
  }
  // 不做后缀白名单：插件里大量脚本没有后缀（dpcollect / dpdoctor 之类），
  // 解释器由 shebang 或手选决定，而不是文件名猜。
  const text = await file.text()
  form.fileName = file.name
  if (!form.name.trim()) form.name = file.name
  form.content = text
  pickedFile.value = { name: file.name, size: file.size }
  validation.value = null
}

async function onFileChange(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (file) await applyFile(file)
  // 清空 value，否则再次选同一个文件不会触发 change。
  input.value = ''
}

async function onDrop(event: DragEvent) {
  dragging.value = false
  const file = event.dataTransfer?.files?.[0]
  if (file) await applyFile(file)
}

async function openEdit(row: LogCollectScriptSummary) {
  if (row.binary) {
    ElMessage.warning('该文件是二进制或子目录附件，不支持在线编辑')
    return
  }
  resetForm()
  creating.value = false
  currentRow.value = row
  const index = row.code.lastIndexOf('/')
  form.dir = index > 0 ? row.code.slice(0, index) : ''
  form.fileName = index > 0 ? row.code.slice(index + 1) : row.code
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
    code: formCode.value,
    content: form.content,
    interpreter: form.interpreter || null,
  })
  return validation.value
}

async function submit() {
  if (!form.fileName.trim()) {
    ElMessage.warning('请填写文件名')
    return
  }
  if (!form.content.trim()) {
    ElMessage.warning('请导入或粘贴脚本内容')
    return
  }
  if (!creating.value && !form.change_note.trim()) {
    ElMessage.warning('请填写本次变更说明（会进入版本历史）')
    return
  }
  saving.value = true
  try {
    const payload = {
      code: formCode.value,
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
        <el-form-item label="目录">
          <el-select
            v-model="form.dir"
            :disabled="!creating"
            filterable
            allow-create
            default-first-option
            placeholder="留空=顶层 script 目录；可手输子目录"
            class="sp-dir"
          >
            <el-option v-for="item in dirOptions" :key="item.value" :label="item.label" :value="item.value" />
          </el-select>
          <div class="sp-hint">
            相对插件 script/ 的目录，最多两层。内置脚本就在 vs_script/ 等子目录里，目录 + 文件名拼起来才是节点上的相对路径。
          </div>
        </el-form-item>
        <el-form-item label="文件名">
          <el-input v-model="form.fileName" :disabled="!creating" placeholder="例如 pl_disk_check.py 或 dpcollect" />
          <div class="sp-hint">
            与目录拼成最终相对路径（也是场景清单里「脚本」动作的 path）：{{ formCode || '（未填写）' }}。
            没有后缀也可以导入；带 pl_ 前缀可避免与内置脚本混淆。
          </div>
        </el-form-item>
        <el-form-item label="展示名">
          <el-input v-model="form.name" maxlength="64" placeholder="可选，便于识别" />
        </el-form-item>
        <el-form-item label="解释器">
          <el-select v-model="form.interpreter" class="sp-interpreter" placeholder="自动识别">
            <el-option label="自动识别（按 shebang，其次后缀）" value="" />
            <el-option label="python" value="python" />
            <el-option label="bash" value="bash" />
          </el-select>
          <span v-if="validation?.ok && validation.interpreter_source" class="sp-hint inline">
            判定依据：{{ validation.interpreter_source }}
          </span>
        </el-form-item>
        <el-form-item label="变更说明">
          <el-input
            v-model="form.change_note"
            maxlength="200"
            :placeholder="creating ? '可选，默认「初始版本」' : '必填，会写入版本历史'"
          />
        </el-form-item>
      </el-form>

      <div
        class="sp-drop"
        :class="{ active: dragging }"
        @click="triggerFilePick"
        @dragover.prevent="dragging = true"
        @dragleave.prevent="dragging = false"
        @drop.prevent="onDrop"
      >
        <template v-if="pickedFile">
          已选择 <strong>{{ pickedFile.name }}</strong>（{{ formatBytes(pickedFile.size) }}）；点击或拖拽可替换内容
        </template>
        <template v-else>点击选择文件，或把脚本拖到这里（不限后缀，最大 4MB）</template>
      </div>
      <input ref="fileInput" type="file" class="sp-file-input" @change="onFileChange" />

      <el-input v-model="form.content" type="textarea" :rows="24" class="sp-editor" spellcheck="false" />

      <div v-if="validation" class="sp-validation" :class="{ ok: validation.ok }">
        <template v-if="validation.ok">
          校验通过：{{ formatBytes(validation.content_bytes) }}，sha256 {{ validation.content_sha256.slice(0, 16) }}…
          <div v-for="(warn, index) in validation.warnings" :key="`w${index}`" class="sp-warn">{{ warn }}</div>
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
.sp-dir,
.sp-interpreter {
  width: 320px;
}
.sp-file-input {
  display: none;
}
.sp-drop {
  border: 1px dashed #c0c4cc;
  border-radius: 4px;
  padding: 12px;
  margin-bottom: 8px;
  color: #6b7280;
  font-size: 13px;
  text-align: center;
  cursor: pointer;
  transition: border-color 0.2s, background-color 0.2s;
}
.sp-drop:hover,
.sp-drop.active {
  border-color: #409eff;
  background: #ecf5ff;
  color: #409eff;
}
.sp-warn {
  margin-top: 4px;
  color: #b45309;
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
