<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { InfoFilled } from '@element-plus/icons-vue'

type SignalLike = Record<string, any>
type VerificationScope = 'signal' | 'ai_step'
type DatasetSource = 'pasted' | 'fixture' | 'replay'

interface PreviewResult {
  trace_id: string
  dataset_id: string
  config_revision: string
  status: 'PASS' | 'FAIL' | 'UNKNOWN'
  value?: unknown
  matcher?: Record<string, unknown>
  evidence?: string
  evidence_lines?: number[]
  derivation?: Record<string, unknown>
  error_code?: string | null
  error_message?: string | null
  ai_raw_response?: Record<string, unknown> | null
}

const props = defineProps<{
  modelValue: boolean
  supportId?: string
  kbdRevision?: number | null
  signal?: SignalLike | null
  signalIndex?: number | null
  processingIndex?: number | null
}>()

const emit = defineEmits<{ 'update:modelValue': [value: boolean] }>()

const verificationScope = ref<VerificationScope>('signal')
const source = ref<DatasetSource>('pasted')
const sampleInput = ref('')
const previewRequested = ref(false)
const previewLoading = ref(false)
const previewResult = ref<PreviewResult | null>(null)
const lastDryRunRequest = ref<Record<string, unknown> | null>(null)
const saveLoading = ref(false)
const datasetLoading = ref(false)
const datasets = ref<Array<{ dataset_id: string; source_type: DatasetSource; source_ref: string; payload: unknown }>>([])
const selectedDatasetId = ref('')

const signalId = computed(() => String(props.signal?.id || `sig_${(props.signalIndex ?? 0) + 1}`))
const instruction = computed(() => String(props.signal?.acquire?.args?.instruction || '未命名 Signal'))
const tool = computed(() => String(props.signal?.acquire?.tool || '未选择工具'))
const isQkv = computed(() => tool.value.startsWith('qkv'))
const hasAiProcessing = computed(() => {
  if (isQkv.value) {
    const processing = props.signal?.orchestrate?.output_processing
    const target = Array.isArray(processing) && typeof props.processingIndex === 'number' ? processing[props.processingIndex] : null
    return Boolean(target?.mode === 'derive' && (target?.extract?.ai_processing || target?.extract?.ai_extract))
  }
  return Boolean(props.signal?.match?.extract?.ai_processing || props.signal?.match?.extract?.ai_extract)
})
const inputLabel = computed(() => isQkv.value ? '已投影变量 JSON records' : '完整 stdout / stderr')
const inputPlaceholder = computed(() => isQkv.value
  ? '[\n  {"description": "...", "host": "..."}\n]'
  : '粘贴当前 Signal 的完整 stdout / stderr 输出')
const sourceDescription = computed(() => ({
  pasted: '临时样本只用于本次预览，不写入 KBD 或现场证据。',
  fixture: '将从已发布 Bundle 的仿真测试资产读取当前 Signal 的独立样本。',
  replay: '现场回放尚未接入，不会读取或伪造现场制品。',
})[source.value])
const previewStatus = computed(() => {
  if (!previewRequested.value) return '暂无运行数据'
  if (previewLoading.value) return '正在执行只读处理链'
  if (previewResult.value) return previewResult.value.status
  if (!sampleInput.value.trim() && source.value === 'pasted') return '请提供试运行输入'
  return '试运行未完成'
})
const canSave = computed(() => previewResult.value?.status === 'PASS')
const selectedDataset = computed(() => datasets.value.find(item => item.dataset_id === selectedDatasetId.value) || null)
const resultExplanation = computed(() => {
  const result = previewResult.value
  if (!result) return ''
  if (result.status === 'PASS') return result.evidence || '处理结果满足当前 Signal 判定条件。'
  if (result.status === 'FAIL') return result.evidence || '处理结果不满足当前 Signal 判定条件。'
  return result.error_message || result.evidence || 'AI 未能可靠处理，无法给出业务结论。'
})
const resultOutput = computed(() => {
  const result = previewResult.value
  if (!result || result.value === undefined || result.value === null) return '—'
  return typeof result.value === 'string' ? result.value : JSON.stringify(result.value)
})
const rawAiResponse = computed(() => {
  const result = previewResult.value
  return result?.ai_raw_response || null
})

function close(): void {
  emit('update:modelValue', false)
}

function canonicalize(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalize)
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value as Record<string, unknown>).sort(([left], [right]) => left.localeCompare(right)).map(([key, item]) => [key, canonicalize(item)]))
  }
  return value
}

async function canonicalHash(value: unknown): Promise<string> {
  // 与服务端一致的稳定草稿身份；服务端仍会自行复算，前端值不具备信任权。
  const text = JSON.stringify(canonicalize(value))
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text))
  return `sha256:${Array.from(new Uint8Array(digest)).map(byte => byte.toString(16).padStart(2, '0')).join('')}`
}

async function requestPreview(): Promise<void> {
  previewRequested.value = true
  previewResult.value = null
  if (!props.signal || !props.supportId || !props.kbdRevision) {
    ElMessage.warning('当前 KBD 草稿身份不完整，无法试运行')
    return
  }
  if (source.value !== 'pasted' && !selectedDataset.value) {
    ElMessage.warning('请先选择服务端提供的数据集')
    return
  }
  if (source.value === 'replay') {
    ElMessage.warning('现场回放暂不支持')
    return
  }
  if (source.value === 'pasted' && !sampleInput.value.trim()) return
  let payload: string | Array<Record<string, unknown>> = selectedDataset.value?.payload as string | Array<Record<string, unknown>> || sampleInput.value
  if (isQkv.value) {
    try {
      const parsed = JSON.parse(sampleInput.value)
      if (!Array.isArray(parsed) || parsed.some(item => !item || typeof item !== 'object' || Array.isArray(item))) throw new Error()
      payload = parsed as Array<Record<string, unknown>>
    } catch {
      ElMessage.error('QKV 输入必须是 JSON records 数组')
      return
    }
  }
  previewLoading.value = true
  try {
    const revision = await canonicalHash(props.signal)
    const dryRunRequest = {
      draft_revision: revision,
      scope: isQkv.value ? 'qkv_variable_processing' : 'qfk_execution_result',
      unit_ref: { signal_id: signalId.value, ...(isQkv.value && typeof props.processingIndex === 'number' ? { processing_index: props.processingIndex } : {}) },
      verification_scope: verificationScope.value,
      dataset: { dataset_id: selectedDataset.value?.dataset_id || crypto.randomUUID(), source_type: source.value, source_ref: selectedDataset.value?.source_ref || 'user-input', payload },
      signal: props.signal, support_id: props.supportId, kbd_revision: props.kbdRevision,
    }
    const response = await fetch('/api/v1/signals/dry-run', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(dryRunRequest),
    })
    const body = await response.json().catch(() => ({}))
    if (!response.ok) {
      const detail = body?.detail
      const errorCode = typeof detail === 'object' ? String(detail?.code || '') : ''
      const errorMessage = typeof detail === 'object' ? String(detail?.message || '') : String(detail || '')
      previewResult.value = {
        trace_id: typeof detail === 'object' ? String(detail?.trace_id || '') : '',
        dataset_id: String(dryRunRequest.dataset && (dryRunRequest.dataset as Record<string, unknown>).dataset_id),
        config_revision: revision,
        status: 'UNKNOWN',
        error_code: errorCode || null,
        error_message: errorMessage || `HTTP ${response.status}`,
      }
      throw new Error(errorMessage || `HTTP ${response.status}`)
    }
    previewResult.value = body as PreviewResult
    lastDryRunRequest.value = dryRunRequest
  } catch (error) {
    if (!previewResult.value) {
      previewResult.value = {
        trace_id: '', dataset_id: '', config_revision: '', status: 'UNKNOWN',
        error_message: error instanceof Error ? error.message : '试运行请求失败',
      }
    }
    ElMessage.error(error instanceof Error ? error.message : '试运行请求失败')
  } finally {
    previewLoading.value = false
  }
}

async function saveToBundle(): Promise<void> {
  if (!canSave.value || !lastDryRunRequest.value || !props.supportId) return
  saveLoading.value = true
  try {
    const listed = await fetch(`/api/hci-sim/v1/control-plane/bundles?support_id=${encodeURIComponent(props.supportId)}`)
    const listBody = await listed.json().catch(() => ({}))
    if (!listed.ok) throw new Error(`Bundle 控制面 HTTP ${listed.status}`)
    // Bug #2 修复：按 kbd_revision 过滤 draft，避免跨版本遗留 draft 误触发 length > 1
    const targetRevision = typeof props.kbdRevision === 'number' ? props.kbdRevision : null
    let drafts = Array.isArray(listBody.bundles)
      ? listBody.bundles.filter((item: Record<string, unknown>) =>
          item.status === 'draft' && (!targetRevision || item.kbd_revision === targetRevision)
        )
      : []
    if (drafts.length > 1) throw new Error('当前 KBD 版本存在多个 Draft，请在 Bundle 工厂完成整理后再保存')
    if (drafts.length === 0) {
      // published Bundle 不能直接追加资产；先让 Gateway 根据当前 KBD C1 权威快照创建唯一 Draft。
      const created = await fetch('/api/hci-sim/v1/control-plane/bundles', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Idempotency-Key': `signal-dry-run-draft:${props.supportId}:${previewResult.value?.trace_id || 'current'}` },
        // Bug #3 修复：透传 kbd_revision，确保新建 Draft 绑定正确的 KBD 版本
        body: JSON.stringify({
          support_id: props.supportId,
          ...(typeof props.kbdRevision === 'number' ? { kbd_revision: props.kbdRevision } : {}),
        }),
      })
      const createdBody = await created.json().catch(() => ({}))
      if (!created.ok) throw new Error(String(createdBody?.detail || `创建 Bundle Draft HTTP ${created.status}`))
      const createdBundle = createdBody?.bundle
      if (!createdBundle || createdBundle.status !== 'draft' || !createdBundle.digest) throw new Error('Bundle 控制面未返回可写入的 Draft')
      drafts = [createdBundle]
    }
    const response = await fetch(`/api/v1/signals/dry-run/bundles/${encodeURIComponent(String(drafts[0].digest))}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ dry_run: lastDryRunRequest.value }),
    })
    const body = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(String(body?.detail || `Bundle 控制面 HTTP ${response.status}`))
    ElMessage.success('已生成包含验证资产的新 Bundle Draft')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '保存 Bundle 草稿失败')
  } finally {
    saveLoading.value = false
  }
}

async function loadDatasets(): Promise<void> {
  datasets.value = []
  selectedDatasetId.value = ''
  if (source.value === 'pasted' || source.value === 'replay' || !props.supportId) return
  datasetLoading.value = true
  try {
    const listed = await fetch('/api/hci-sim/v1/control-plane/bundles?support_id=' + encodeURIComponent(props.supportId))
    const listBody = await listed.json().catch(() => ({}))
    if (!listed.ok) throw new Error('Bundle 控制面 HTTP ' + listed.status)
    const published = Array.isArray(listBody.bundles)
      ? listBody.bundles.filter((item: Record<string, unknown>) => item.status === 'published' && (!props.kbdRevision || item.kbd_revision === props.kbdRevision))
      : []
    // Bug #5 修复：多个 published 时取最新一个（List API 已按 updated_at DESC 排序），而非直接报错
    if (published.length === 0) throw new Error('当前 KBD 没有已发布 Bundle，请先发布一个 Bundle 后再使用仿真测试数据源')
    const digest = String(published[0].digest)
    const response = await fetch('/api/hci-sim/v1/control-plane/bundles/' + encodeURIComponent(digest) + '/dry-run-datasets?signal_id=' + encodeURIComponent(signalId.value) + '&source_type=' + source.value)
    const body = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(String(body?.detail || '数据集 HTTP ' + response.status))
    datasets.value = Array.isArray(body.datasets) ? body.datasets : []
    if (datasets.value.length === 1) selectedDatasetId.value = datasets.value[0].dataset_id
    if (!datasets.value.length) ElMessage.info('当前已发布 Bundle 没有该来源的 PASS 验证资产')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '加载试运行数据集失败')
  } finally {
    datasetLoading.value = false
  }
}

watch(() => props.modelValue, (visible) => {
  if (!visible) return
  verificationScope.value = 'signal'
  source.value = 'pasted'
  sampleInput.value = ''
  previewRequested.value = false
  previewResult.value = null
  lastDryRunRequest.value = null
  datasets.value = []
  selectedDatasetId.value = ''
})

watch([sampleInput, source, verificationScope], () => {
  previewRequested.value = false
  previewResult.value = null
  lastDryRunRequest.value = null
})

watch(source, () => { void loadDatasets() })
watch(selectedDatasetId, () => {
  if (selectedDataset.value && source.value !== 'pasted') {
    sampleInput.value = typeof selectedDataset.value.payload === 'string'
      ? selectedDataset.value.payload
      : JSON.stringify(selectedDataset.value.payload, null, 2)
  }
})
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    class="signal-dry-run-dialog"
    width="min(900px, calc(100vw - 32px))"
    top="6vh"
    :close-on-click-modal="false"
    @update:model-value="(value: boolean) => emit('update:modelValue', value)"
  >
    <template #header>
      <div class="dialog-heading">
        <div>
          <div class="dialog-title">试运行 · {{ signalId }} {{ instruction }}</div>
          <div class="dialog-subtitle">已绑定 KBD {{ supportId || '—' }} / rev.{{ kbdRevision || '草稿' }} · {{ tool }}</div>
        </div>
      </div>
    </template>

    <div class="bound-context">
      <el-icon><InfoFilled /></el-icon>
      <span>当前窗口只验证此 Signal。切换到其它 Signal 后，输入、结果和 Bundle 资产彼此隔离。</span>
    </div>

    <div class="dialog-grid">
      <el-form label-position="top" class="dry-run-form">
        <el-form-item label="验证范围">
          <el-radio-group v-model="verificationScope">
            <el-radio-button value="signal">整个 Signal</el-radio-button>
            <el-radio-button value="ai_step" :disabled="!hasAiProcessing">当前 AI 处理</el-radio-button>
          </el-radio-group>
          <div class="field-hint">
            {{ verificationScope === 'signal' ? '执行输入适配、取值、AI 处理（如启用）和最终 Matcher/变量产出。' : '只检查当前 Signal 中已配置的 AI 取值或智能推导步骤。' }}
          </div>
        </el-form-item>

        <el-form-item label="输入来源">
          <el-select v-model="source" :loading="datasetLoading">
            <el-option label="临时样本（用户输入）" value="pasted" />
            <el-option label="仿真测试（bundle资产）" value="fixture" />
            <el-option label="现场回放（暂不支持）" value="replay" disabled />
          </el-select>
          <div class="field-hint">{{ sourceDescription }}</div>
        </el-form-item>

        <el-form-item v-if="source !== 'pasted'" label="服务端数据集">
          <el-select v-model="selectedDatasetId" :loading="datasetLoading" placeholder="选择已发布 Bundle 中的 PASS 资产">
            <el-option v-for="item in datasets" :key="item.dataset_id" :label="item.dataset_id + ' · ' + item.source_ref" :value="item.dataset_id" />
          </el-select>
        </el-form-item>

        <el-form-item v-if="source === 'pasted'" :label="`试运行输入（${inputLabel}）`">
          <el-input v-model="sampleInput" type="textarea" :rows="9" :placeholder="inputPlaceholder" resize="vertical" class="sample-input" />
        </el-form-item>

        <el-alert
          v-else
          type="info"
          :closable="false"
          show-icon
          title="数据集来自已发布 Bundle 的服务端验证资产，浏览器不能伪造输入。"
        />
      </el-form>

      <section class="preview-panel" aria-label="AI 处理结果" aria-live="polite">
        <div class="preview-heading"><span>AI 处理结果</span></div>
        <div v-if="!previewResult" class="preview-empty">
          <strong>{{ previewStatus }}</strong>
          <p>提供一组输入后执行预览。</p>
        </div>
        <template v-else>
          <div class="result-conclusion" :class="`result-${previewResult.status.toLowerCase()}`">
            <span>最终结论</span>
            <strong>{{ previewResult.status }}</strong>
            <small>{{ resultExplanation }}</small>
          </div>
          <dl class="preview-context result-context">
            <div><dt>输出值</dt><dd><code>{{ resultOutput }}</code><small>AI 返回的业务结果</small></dd></div>
            <div><dt>证据行</dt><dd>{{ previewResult.evidence_lines?.length ? previewResult.evidence_lines.map(line => `line:${line}`).join(' · ') : '—' }}</dd></div>
            <div><dt>处理说明</dt><dd>{{ resultExplanation }}</dd></div>
          </dl>
          <details class="raw-response">
            <summary><strong>AI 原始响应详情</strong><span>展开查看原始 JSON</span></summary>
            <pre v-if="rawAiResponse">{{ JSON.stringify(rawAiResponse, null, 2) }}</pre>
            <p v-else class="raw-missing">本次处理未调用 AI，或服务未返回可审计的原始响应。</p>
            <div class="raw-meta"><span>trace_id</span><code>{{ previewResult.trace_id || '—' }}</code><span v-if="previewResult.error_code">error_code</span><code v-if="previewResult.error_code">{{ previewResult.error_code }}</code></div>
          </details>
        </template>
      </section>
    </div>

    <template #footer>
      <div class="dialog-footer">
        <span>保存功能依赖 Bundle Draft 的 `verification_assets` 后端契约。</span>
        <div>
          <el-button @click="close">取消</el-button>
          <el-button type="primary" plain :loading="previewLoading" @click="requestPreview">解析预览</el-button>
          <el-tooltip content="服务端重新验证通过后追加为新的 Bundle Draft" placement="top">
            <span><el-button type="primary" :loading="saveLoading" :disabled="!canSave" @click="saveToBundle">保存到 Bundle 草稿</el-button></span>
          </el-tooltip>
        </div>
      </div>
    </template>
  </el-dialog>
</template>

<style scoped>
.dialog-heading { min-width: 0; padding-right: 24px; }
.dialog-title { color: var(--el-text-color-primary); font-size: 16px; font-weight: 600; line-height: 1.35; }
.dialog-subtitle { margin-top: 4px; color: var(--el-text-color-secondary); font-size: 12px; }
.bound-context { display: flex; align-items: flex-start; gap: 8px; margin-bottom: 16px; padding: 10px 12px; border: 1px solid var(--el-color-primary-light-7); border-radius: 6px; background: var(--el-color-primary-light-9); color: var(--el-color-primary-dark-2); font-size: 12px; }
.bound-context .el-icon { flex: 0 0 auto; margin-top: 2px; }
.dialog-grid { display: grid; grid-template-columns: minmax(0, 1.25fr) minmax(260px, .75fr); gap: 16px; }
.dry-run-form { min-width: 0; }
.dry-run-form :deep(.el-form-item) { margin-bottom: 16px; }
.dry-run-form :deep(.el-select) { width: 100%; }
.field-hint { margin-top: 6px; color: var(--el-text-color-secondary); font-size: 12px; line-height: 1.5; }
.sample-input :deep(textarea) { font-family: var(--el-font-family); font-size: 12px; line-height: 1.55; }
.preview-panel { min-width: 0; border: 1px solid var(--el-border-color); border-radius: 6px; background: var(--el-fill-color-extra-light); overflow: hidden; }
.preview-heading { display: flex; justify-content: space-between; align-items: center; gap: 8px; padding: 11px 12px; border-bottom: 1px solid var(--el-border-color-light); background: var(--el-bg-color); font-size: 13px; font-weight: 600; }
.preview-empty { display: grid; justify-items: center; padding: 30px 18px 18px; text-align: center; }
.preview-empty strong { color: var(--el-text-color-primary); font-size: 13px; }
.preview-empty p { margin: 7px 0 0; color: var(--el-text-color-secondary); font-size: 12px; line-height: 1.55; }
.result-conclusion { padding: 18px 16px 15px; border-bottom: 1px solid var(--el-border-color-light); }
.result-conclusion > span { display: block; color: var(--el-text-color-secondary); font-size: 12px; }
.result-conclusion strong { display: block; margin: 6px 0 4px; font: 600 28px/1.15 ui-monospace, SFMono-Regular, monospace; letter-spacing: .2px; }
.result-conclusion small { display: block; color: var(--el-text-color-regular); font-size: 12px; line-height: 1.5; }
.result-pass { background: var(--el-color-success-light-9); }
.result-pass strong { color: var(--el-color-success); }
.result-fail { background: var(--el-color-danger-light-9); }
.result-fail strong { color: var(--el-color-danger); }
.result-unknown { background: var(--el-color-warning-light-9); }
.result-unknown strong { color: var(--el-color-warning-dark-2); }
.preview-context { display: grid; gap: 0; margin: 0 12px 12px; font-size: 12px; }
.preview-context > div { display: grid; grid-template-columns: 60px minmax(0, 1fr); gap: 8px; padding: 10px 0; border-bottom: 1px solid var(--el-border-color-lighter); }
.preview-context > div:last-child { border-bottom: 0; }
.preview-context dt { color: var(--el-text-color-secondary); }
.preview-context dd { margin: 0; overflow-wrap: anywhere; color: var(--el-text-color-regular); line-height: 1.5; }
.preview-context dd small { display: block; margin-top: 2px; color: var(--el-text-color-secondary); font-size: 11px; }
.raw-response { margin: 8px 12px 14px; border: 1px solid var(--el-border-color); border-radius: 5px; background: var(--el-bg-color); }
.raw-response summary { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 11px 12px; cursor: pointer; list-style: none; }
.raw-response summary::-webkit-details-marker { display: none; }
.raw-response summary strong { font-size: 12px; font-weight: 600; }
.raw-response summary span { color: var(--el-text-color-secondary); font-size: 11px; }
.raw-response pre { max-height: 220px; margin: 0 10px 10px; padding: 10px; overflow: auto; border: 1px solid var(--el-border-color-light); border-radius: 4px; background: var(--el-fill-color-extra-light); font: 11px/1.55 ui-monospace, SFMono-Regular, monospace; text-align: left; white-space: pre-wrap; overflow-wrap: anywhere; }
.raw-missing { margin: 0 10px 10px; color: var(--el-text-color-secondary); font-size: 12px; line-height: 1.5; }
.raw-meta { display: grid; grid-template-columns: 72px minmax(0, 1fr); gap: 6px 8px; margin: 0 10px 11px; padding-top: 9px; border-top: 1px dashed var(--el-border-color); font-size: 11px; }
.raw-meta span { color: var(--el-text-color-secondary); }
.raw-meta code { color: var(--el-text-color-regular); overflow-wrap: anywhere; }
.dialog-footer { display: flex; align-items: center; justify-content: space-between; gap: 12px; width: 100%; color: var(--el-text-color-secondary); font-size: 12px; text-align: left; }
.dialog-footer > div { display: flex; flex: 0 0 auto; gap: 8px; }
@media (max-width: 720px) { .dialog-grid { grid-template-columns: 1fr; } .dialog-footer { align-items: flex-start; flex-direction: column; } .dialog-footer > div { align-self: flex-end; flex-wrap: wrap; justify-content: flex-end; } }
</style>
