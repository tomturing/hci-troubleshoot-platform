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

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  'saved': [bundle: Record<string, unknown>]
}>()

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

const isEditingFork = ref(false)
const forkedSourceRef = ref('')

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
const inputLabel = computed(() => isQkv.value ? '已投影变量 JSON records 或采集原始输出' : '完整 stdout / stderr')
const inputPlaceholder = computed(() => isQkv.value
  ? '[\n  {"description": "...", "host": "..."}\n] 或 {"data": [...]}'
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
  if (!sampleInput.value.trim() && (source.value === 'pasted' || isEditingFork.value)) return '请提供试运行输入'
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

function startForkEditing(): void {
  if (!selectedDataset.value) {
    ElMessage.warning('请先选择一条服务端数据集作为编辑模板')
    return
  }
  isEditingFork.value = true
  forkedSourceRef.value = String(selectedDataset.value.dataset_id || selectedDataset.value.source_ref)
  sampleInput.value = typeof selectedDataset.value.payload === 'string'
    ? selectedDataset.value.payload
    : JSON.stringify(selectedDataset.value.payload, null, 2)
  previewResult.value = null
  lastDryRunRequest.value = null
  previewRequested.value = false
}

function cancelForkEditing(): void {
  isEditingFork.value = false
  forkedSourceRef.value = ''
  if (selectedDataset.value) {
    sampleInput.value = typeof selectedDataset.value.payload === 'string'
      ? selectedDataset.value.payload
      : JSON.stringify(selectedDataset.value.payload, null, 2)
  }
  previewResult.value = null
  lastDryRunRequest.value = null
  previewRequested.value = false
}

function canonicalize(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalize)
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value as Record<string, unknown>).sort(([left], [right]) => left.localeCompare(right)).map(([key, item]) => [key, canonicalize(item)]))
  }
  return value
}

async function canonicalHash(value: unknown): Promise<string> {
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
  if (source.value !== 'pasted' && !isEditingFork.value && !selectedDataset.value) {
    ElMessage.warning('请先选择服务端提供的数据集')
    return
  }
  if (source.value === 'replay') {
    ElMessage.warning('现场回放暂不支持')
    return
  }
  const isEditing = source.value === 'pasted' || isEditingFork.value
  if (isEditing && !sampleInput.value.trim()) return

  const rawSource = isEditing
    ? sampleInput.value
    : (selectedDataset.value?.payload ?? sampleInput.value)

  let payload: string | Array<Record<string, unknown>> = rawSource as any

  if (isQkv.value) {
    try {
      const textToParse = typeof rawSource === 'string' ? rawSource : JSON.stringify(rawSource)
      let parsed = JSON.parse(textToParse)
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        if (Array.isArray(parsed.data)) {
          parsed = parsed.data
        } else if (Array.isArray(parsed.items)) {
          parsed = parsed.items
        } else {
          parsed = [parsed]
        }
      }
      if (!Array.isArray(parsed) || parsed.some(item => !item || typeof item !== 'object' || Array.isArray(item))) throw new Error()
      payload = parsed as Array<Record<string, unknown>>
    } catch {
      ElMessage.error('QKV 输入必须是合法 JSON（records 数组或包含 data/items 的返回对象）')
      return
    }
  }
  previewLoading.value = true
  try {
    const revision = await canonicalHash(props.signal)
    const effectiveSourceType = isEditingFork.value ? 'pasted' : source.value
    const effectiveSourceRef = effectiveSourceType === 'pasted' ? 'user-input' : (selectedDataset.value?.source_ref || 'user-input')
    const dryRunRequest = {
      draft_revision: revision,
      scope: isQkv.value ? 'qkv_variable_processing' : 'qfk_execution_result',
      unit_ref: { signal_id: signalId.value, ...(isQkv.value && typeof props.processingIndex === 'number' ? { processing_index: props.processingIndex } : {}) },
      verification_scope: verificationScope.value,
      dataset: {
        dataset_id: isEditingFork.value ? `fork-${crypto.randomUUID()}` : (selectedDataset.value?.dataset_id || crypto.randomUUID()),
        source_type: effectiveSourceType,
        source_ref: effectiveSourceRef,
        raw_input: rawSource,
        payload,
      },
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
    const targetRevision = typeof props.kbdRevision === 'number' ? props.kbdRevision : null
    let drafts = Array.isArray(listBody.bundles)
      ? listBody.bundles.filter((item: Record<string, unknown>) =>
          item.status === 'draft' && (!targetRevision || item.kbd_revision === targetRevision)
        )
      : []
    if (drafts.length > 1) throw new Error('当前 KBD 版本存在多个 Draft，请在 Bundle 工厂完成整理后再保存')
    if (drafts.length === 0) {
      const created = await fetch('/api/hci-sim/v1/control-plane/bundles', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Idempotency-Key': `signal-dry-run-draft:${props.supportId}:${previewResult.value?.trace_id || 'current'}` },
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
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        dry_run: lastDryRunRequest.value,
        preview_token: (previewResult.value as Record<string, unknown> | null)?.preview_token,
        preview_result: previewResult.value,
      }),
    })
    const body = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(String(body?.detail || `Bundle 控制面 HTTP ${response.status}`))
    const savedBundle = (body?.bundle || drafts[0]) as Record<string, unknown>
    const shortDigest = String(savedBundle?.digest || '').slice(0, 16)
    ElMessage.success(`已生成包含最新仿真输出与验证资产的 Bundle Draft${shortDigest ? ` (${shortDigest}...)` : ''}`)
    emit('saved', savedBundle)
    isEditingFork.value = false
    void loadDatasets()
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
    const allPublished = Array.isArray(listBody.bundles)
      ? listBody.bundles.filter((item: Record<string, unknown>) => item.status === 'published')
      : []
    if (allPublished.length === 0) {
      throw new Error('当前 KBD 没有已发布 Bundle，请先在 Bundle 工厂发布后再使用仿真测试数据源')
    }

    const revisionMatched = typeof props.kbdRevision === 'number'
      ? allPublished.filter((item: Record<string, unknown>) => item.kbd_revision === props.kbdRevision)
      : []
    const publishedCandidates = revisionMatched.length > 0 ? revisionMatched : allPublished
    let targetBundle: Record<string, unknown> | undefined = publishedCandidates[0]

    try {
      const actRes = await fetch(`/api/hci-sim/v1/control-plane/activations/${encodeURIComponent(props.supportId)}`)
      if (actRes.ok) {
        const actBody = await actRes.json().catch(() => ({}))
        const runtime = (actBody?.runtime_activation || {}) as Record<string, unknown>
        const activeDigest = String(runtime.active_digest || runtime.ActiveDigest || '')
        const activeStatus = String(runtime.status || runtime.Status || '')
        if (activeDigest && (activeStatus === 'active' || !activeStatus)) {
          const matchedActive = allPublished.find(b => String(b.digest) === activeDigest)
          if (matchedActive) {
            targetBundle = matchedActive
          }
        }
      }
    } catch {
      // ignore
    }

    if (!targetBundle || !targetBundle.digest) {
      throw new Error('未找到可用的已发布 Bundle 资产')
    }

    const digest = String(targetBundle.digest)
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
  isEditingFork.value = false
  forkedSourceRef.value = ''
  previewRequested.value = false
  previewResult.value = null
  lastDryRunRequest.value = null
  datasets.value = []
  selectedDatasetId.value = ''
})

watch([sampleInput, verificationScope], () => {
  previewRequested.value = false
  previewResult.value = null
  lastDryRunRequest.value = null
})

watch(source, () => {
  isEditingFork.value = false
  forkedSourceRef.value = ''
  previewRequested.value = false
  previewResult.value = null
  lastDryRunRequest.value = null
  void loadDatasets()
})

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
    width="min(1080px, calc(100vw - 32px))"
    top="5vh"
    :close-on-click-modal="false"
    @update:model-value="(value: boolean) => emit('update:modelValue', value)"
  >
    <template #header>
      <div class="dialog-heading">
        <div class="dialog-title">试运行 · {{ signalId }} {{ instruction }}</div>
        <div class="dialog-subtitle">已绑定 KBD {{ supportId || '—' }} / rev.{{ kbdRevision || '草稿' }} · {{ tool }}</div>
      </div>
    </template>

    <div class="bound-context">
      <el-icon><InfoFilled /></el-icon>
      <span>当前窗口只验证此 Signal。切换到其它 Signal 后，输入、结果和 Bundle 资产彼此隔离。</span>
    </div>

    <div class="dialog-grid">
      <el-form label-position="top" class="dry-run-form">
        <el-form-item label="验证范围">
          <el-radio-group v-model="verificationScope" class="scope-radio-group">
            <el-radio-button value="signal">整个 Signal</el-radio-button>
            <el-radio-button value="ai_step" :disabled="!hasAiProcessing">当前 AI 处理</el-radio-button>
          </el-radio-group>
          <div class="field-hint">
            {{ verificationScope === 'signal' ? '执行输入适配、取值、AI 处理（如启用）和最终 Matcher/变量产出。' : '只检查当前 Signal 中已配置的 AI 取值或智能推导步骤。' }}
          </div>
        </el-form-item>

        <el-form-item label="输入来源">
          <el-select v-model="source" :loading="datasetLoading" :disabled="isEditingFork">
            <el-option label="临时样本（用户输入）" value="pasted" />
            <el-option label="仿真测试（bundle资产）" value="fixture" />
            <el-option label="现场回放（暂不支持）" value="replay" disabled />
          </el-select>
          <div class="field-hint">{{ sourceDescription }}</div>
        </el-form-item>

        <el-form-item v-if="source !== 'pasted'" label="服务端数据集">
          <el-select v-model="selectedDatasetId" :loading="datasetLoading" :disabled="isEditingFork" placeholder="选择已发布 Bundle 中的 PASS 资产">
            <el-option v-for="item in datasets" :key="item.dataset_id" :label="item.dataset_id + ' · ' + item.source_ref" :value="item.dataset_id" />
          </el-select>
        </el-form-item>

        <!-- 编辑模式提示条 -->
        <div v-if="isEditingFork" class="fork-edit-banner">
          <div class="fork-banner-content">
            <el-icon class="fork-icon"><InfoFilled /></el-icon>
            <span>正在基于 <code>{{ forkedSourceRef }}</code> 编辑新内容，修改后需重新试运行通过方可保存。</span>
          </div>
          <el-button link type="primary" size="small" @click="cancelForkEditing">放弃修改</el-button>
        </div>

        <!-- 试运行输入区 -->
        <el-form-item v-if="source === 'pasted' || isEditingFork" :label="`试运行输入（${inputLabel}）`">
          <el-input
            v-model="sampleInput"
            type="textarea"
            :rows="11"
            :placeholder="inputPlaceholder"
            resize="vertical"
            class="sample-input"
          />
        </el-form-item>

        <!-- 只读预览区（当 source 为 fixture 且尚未点击编辑时） -->
        <el-form-item v-else :label="`基线资产内容预览（${inputLabel}）`">
          <el-input
            :model-value="sampleInput"
            type="textarea"
            :rows="9"
            readonly
            class="sample-input readonly-input"
          />
          <div class="field-hint">
            数据来自已发布 Bundle。如需基于此内容修改并沉淀为新资产，请点击右下角「创建新 Bundle 草稿」。
          </div>
        </el-form-item>
      </el-form>

      <section class="preview-panel" aria-label="运行结果" aria-live="polite">
        <div class="preview-heading">
          <span class="preview-title">运行结果</span>
          <span v-if="previewResult" class="preview-badge" :class="`badge-${previewResult.status.toLowerCase()}`">
            {{ previewResult.status }}
          </span>
          <span v-else-if="previewLoading" class="preview-badge badge-running">执行中</span>
          <span v-else class="preview-badge badge-idle">待运行</span>
        </div>

        <div v-if="!previewResult" class="preview-empty">
          <div class="empty-state-icon">
            <el-icon :size="28"><InfoFilled /></el-icon>
          </div>
          <strong>{{ previewStatus }}</strong>
          <p>提供一组输入后点击「试运行」获取处理结果与验证结论。</p>
        </div>

        <template v-else>
          <div class="result-conclusion" :class="`result-${previewResult.status.toLowerCase()}`">
            <div class="conclusion-meta">
              <span class="conclusion-label">最终结论</span>
              <strong class="conclusion-status">{{ previewResult.status }}</strong>
            </div>
            <div class="conclusion-text">{{ resultExplanation }}</div>
          </div>

          <dl class="preview-context result-context">
            <div class="context-row">
              <dt>输出值</dt>
              <dd>
                <div class="code-value"><code>{{ resultOutput }}</code></div>
                <small class="code-caption">AI 返回的业务结果</small>
              </dd>
            </div>
            <div class="context-row">
              <dt>证据行</dt>
              <dd>
                <div v-if="previewResult.evidence_lines?.length" class="evidence-list">
                  <span v-for="line in previewResult.evidence_lines" :key="line" class="evidence-tag">line:{{ line }}</span>
                </div>
                <span v-else class="text-muted">—</span>
              </dd>
            </div>
            <div class="context-row">
              <dt>处理说明</dt>
              <dd class="explanation-text">{{ resultExplanation }}</dd>
            </div>
          </dl>

          <details class="raw-response">
            <summary>
              <strong>AI 原始响应详情</strong>
              <span class="summary-hint">展开查看原始 JSON</span>
            </summary>
            <pre v-if="rawAiResponse">{{ JSON.stringify(rawAiResponse, null, 2) }}</pre>
            <p v-else class="raw-missing">本次处理未调用 AI，或服务未返回可审计的原始响应。</p>
            <div class="raw-meta">
              <span>trace_id</span>
              <code>{{ previewResult.trace_id || '—' }}</code>
              <template v-if="previewResult.error_code">
                <span>error_code</span>
                <code>{{ previewResult.error_code }}</code>
              </template>
            </div>
          </details>
        </template>
      </section>
    </div>

    <template #footer>
      <div class="dialog-footer">
        <span class="footer-hint">保存功能依赖 Bundle Draft 的 `verification_assets` 后端契约。</span>
        <div class="footer-actions">
          <el-button @click="close">取消</el-button>
          <el-button type="primary" plain :loading="previewLoading" @click="requestPreview">试运行</el-button>

          <!-- 场景 1：Bundle 资产只读模式 -> 显示【创建新 Bundle 草稿】 -->
          <template v-if="source !== 'pasted' && !isEditingFork">
            <el-tooltip content="将当前选中的 Bundle 资产载入编辑窗口，可二次修改并生成新 Draft" placement="top">
              <el-button type="primary" :disabled="!selectedDatasetId" @click="startForkEditing">
                创建新 Bundle 草稿
              </el-button>
            </el-tooltip>
          </template>

          <!-- 场景 2：编辑模式（临时输入或从 Bundle fork 编辑）-> 显示【保存到 Bundle 草稿】 -->
          <template v-else>
            <el-tooltip :content="canSave ? '将当前编辑并验证通过的资产保存为新的 Bundle Draft' : '请先完成试运行且结果为 PASS 后再保存'" placement="top">
              <span>
                <el-button type="primary" :loading="saveLoading" :disabled="!canSave" @click="saveToBundle">
                  保存到 Bundle 草稿
                </el-button>
              </span>
            </el-tooltip>
          </template>
        </div>
      </div>
    </template>
  </el-dialog>
</template>

<style scoped>
.dialog-heading { min-width: 0; padding-right: 24px; }
.dialog-title { color: var(--el-text-color-primary); font-size: 16px; font-weight: 600; line-height: 1.4; }
.dialog-subtitle { margin-top: 4px; color: var(--el-text-color-secondary); font-size: 12px; }

.bound-context {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 16px;
  padding: 8px 12px;
  border: 1px solid var(--el-color-primary-light-7);
  border-radius: 6px;
  background: var(--el-color-primary-light-9);
  color: var(--el-color-primary-dark-2);
  font-size: 12px;
  line-height: 1.4;
}
.bound-context .el-icon { flex: 0 0 auto; color: var(--el-color-primary); font-size: 14px; }

.dialog-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.15fr) minmax(0, 1fr);
  gap: 20px;
  align-items: stretch;
}

.dry-run-form {
  min-width: 0;
  display: flex;
  flex-direction: column;
}
.dry-run-form :deep(.el-form-item) { margin-bottom: 16px; }
.dry-run-form :deep(.el-form-item__label) { font-size: 13px; font-weight: 500; color: var(--el-text-color-primary); padding-bottom: 6px; }
.dry-run-form :deep(.el-select) { width: 100%; }

.fork-edit-banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
  padding: 8px 12px;
  border-radius: 6px;
  background: var(--el-color-warning-light-9);
  border: 1px solid var(--el-color-warning-light-7);
  font-size: 12px;
  color: var(--el-color-warning-dark-2);
  line-height: 1.4;
}
.fork-banner-content {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  flex: 1;
}
.fork-icon {
  color: var(--el-color-warning);
  font-size: 14px;
  flex-shrink: 0;
}
.fork-banner-content span {
  min-width: 0;
  word-break: break-word;
}
.fork-banner-content code {
  font-family: ui-monospace, monospace;
  background: rgba(0, 0, 0, 0.05);
  padding: 1px 4px;
  border-radius: 3px;
  max-width: 180px;
  display: inline-block;
  vertical-align: middle;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.fork-edit-banner :deep(.el-button) {
  flex-shrink: 0;
  white-space: nowrap;
  padding: 0;
  margin-left: 8px;
}
.readonly-input :deep(textarea) {
  background-color: var(--el-fill-color-light);
  color: var(--el-text-color-regular);
  cursor: default;
}
.field-hint { margin-top: 5px; color: var(--el-text-color-secondary); font-size: 12px; line-height: 1.5; }
.sample-input :deep(textarea) {
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  font-size: 12px;
  line-height: 1.55;
  background: var(--el-fill-color-blank);
  border-color: var(--el-border-color);
  border-radius: 6px;
}
.sample-input :deep(textarea:focus) {
  border-color: var(--el-color-primary);
}

.preview-panel {
  min-width: 0;
  display: flex;
  flex-direction: column;
  border: 1px solid var(--el-border-color);
  border-radius: 8px;
  background: var(--el-bg-color);
  overflow: hidden;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.03);
}

.preview-heading {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--el-border-color-lighter);
  background: var(--el-fill-color-light);
}
.preview-title { font-size: 13px; font-weight: 600; color: var(--el-text-color-primary); }

.preview-badge {
  display: inline-flex;
  align-items: center;
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 11px;
  font-weight: 600;
  line-height: 1.2;
}
.badge-idle { background: var(--el-fill-color-darker); color: var(--el-text-color-secondary); }
.badge-running { background: var(--el-color-primary-light-8); color: var(--el-color-primary); }
.badge-pass { background: var(--el-color-success-light-8); color: var(--el-color-success-dark-2); }
.badge-fail { background: var(--el-color-danger-light-8); color: var(--el-color-danger-dark-2); }
.badge-unknown { background: var(--el-color-warning-light-8); color: var(--el-color-warning-dark-2); }

.preview-empty {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 36px 20px;
  text-align: center;
  min-height: 260px;
}
.empty-state-icon {
  margin-bottom: 10px;
  color: var(--el-text-color-placeholder);
}
.preview-empty strong { color: var(--el-text-color-primary); font-size: 13px; }
.preview-empty p { margin: 6px 0 0; color: var(--el-text-color-secondary); font-size: 12px; line-height: 1.5; max-width: 260px; }

.result-conclusion {
  padding: 14px 16px;
  border-bottom: 1px solid var(--el-border-color-light);
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.conclusion-meta {
  display: flex;
  align-items: baseline;
  gap: 10px;
}
.conclusion-label { color: var(--el-text-color-secondary); font-size: 12px; }
.conclusion-status {
  font: 700 24px/1 ui-monospace, SFMono-Regular, "SF Mono", monospace;
  letter-spacing: 0.5px;
}
.conclusion-text {
  color: var(--el-text-color-regular);
  font-size: 12px;
  line-height: 1.55;
}

.result-pass {
  background: var(--el-color-success-light-9);
  border-left: 4px solid var(--el-color-success);
}
.result-pass .conclusion-status { color: var(--el-color-success); }

.result-fail {
  background: var(--el-color-danger-light-9);
  border-left: 4px solid var(--el-color-danger);
}
.result-fail .conclusion-status { color: var(--el-color-danger); }

.result-unknown {
  background: var(--el-color-warning-light-9);
  border-left: 4px solid var(--el-color-warning);
}
.result-unknown .conclusion-status { color: var(--el-color-warning-dark-2); }

.preview-context {
  display: flex;
  flex-direction: column;
  margin: 0;
  padding: 8px 14px;
  font-size: 12px;
  flex: 1;
}
.context-row {
  display: grid;
  grid-template-columns: 64px minmax(0, 1fr);
  gap: 12px;
  padding: 10px 0;
  border-bottom: 1px solid var(--el-border-color-lighter);
}
.context-row:last-child { border-bottom: 0; }
.context-row dt { color: var(--el-text-color-secondary); font-weight: 500; line-height: 1.5; }
.context-row dd { margin: 0; overflow-wrap: anywhere; color: var(--el-text-color-primary); line-height: 1.5; }

.code-value {
  display: inline-block;
  max-width: 100%;
}
.code-value code {
  padding: 2px 6px;
  border-radius: 4px;
  background: var(--el-fill-color-light);
  border: 1px solid var(--el-border-color-lighter);
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  font-size: 12px;
  color: var(--el-color-primary-dark-2);
}
.code-caption { display: block; margin-top: 3px; color: var(--el-text-color-secondary); font-size: 11px; }

.evidence-list { display: flex; flex-wrap: wrap; gap: 6px; }
.evidence-tag {
  display: inline-block;
  padding: 1px 6px;
  border-radius: 4px;
  background: var(--el-fill-color-light);
  border: 1px solid var(--el-border-color-lighter);
  color: var(--el-text-color-regular);
  font-family: ui-monospace, SFMono-Regular, monospace;
  font-size: 11px;
}
.text-muted { color: var(--el-text-color-placeholder); }
.explanation-text { color: var(--el-text-color-regular); font-size: 12px; line-height: 1.5; }

.raw-response {
  margin: 10px 14px 14px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  background: var(--el-fill-color-blank);
  overflow: hidden;
}
.raw-response summary {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 9px 12px;
  background: var(--el-fill-color-extra-light);
  cursor: pointer;
  list-style: none;
  font-size: 12px;
}
.raw-response summary::-webkit-details-marker { display: none; }
.raw-response summary strong { font-weight: 600; color: var(--el-text-color-regular); }
.summary-hint { color: var(--el-text-color-secondary); font-size: 11px; }
.raw-response pre {
  max-height: 200px;
  margin: 8px 10px 10px;
  padding: 10px;
  overflow: auto;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 4px;
  background: var(--el-fill-color-light);
  font: 11px/1.55 ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace;
  text-align: left;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.raw-missing { margin: 8px 10px 10px; color: var(--el-text-color-secondary); font-size: 12px; line-height: 1.5; }
.raw-meta {
  display: grid;
  grid-template-columns: 72px minmax(0, 1fr);
  gap: 6px 8px;
  margin: 0 10px 10px;
  padding-top: 8px;
  border-top: 1px dashed var(--el-border-color-lighter);
  font-size: 11px;
}
.raw-meta span { color: var(--el-text-color-secondary); }
.raw-meta code { color: var(--el-text-color-regular); overflow-wrap: anywhere; font-family: ui-monospace, SFMono-Regular, monospace; }

.dialog-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  width: 100%;
  text-align: left;
}
.footer-hint { color: var(--el-text-color-secondary); font-size: 12px; }
.footer-actions { display: flex; flex: 0 0 auto; gap: 10px; }

@media (max-width: 840px) {
  .dialog-grid { grid-template-columns: 1fr; gap: 16px; }
  .dialog-footer { align-items: flex-start; flex-direction: column; }
  .footer-actions { align-self: flex-end; flex-wrap: wrap; justify-content: flex-end; }
}
</style>
