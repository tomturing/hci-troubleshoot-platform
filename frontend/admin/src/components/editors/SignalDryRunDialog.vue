<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { InfoFilled, WarningFilled } from '@element-plus/icons-vue'

type SignalLike = Record<string, any>
type VerificationScope = 'signal' | 'ai_step'
type DatasetSource = 'pasted' | 'fixture' | 'replay'

const props = defineProps<{
  modelValue: boolean
  supportId?: string
  kbdRevision?: number | null
  signal?: SignalLike | null
  signalIndex?: number | null
}>()

const emit = defineEmits<{ 'update:modelValue': [value: boolean] }>()

const verificationScope = ref<VerificationScope>('signal')
const source = ref<DatasetSource>('pasted')
const sampleInput = ref('')
const previewRequested = ref(false)

const signalId = computed(() => String(props.signal?.id || `sig_${(props.signalIndex ?? 0) + 1}`))
const instruction = computed(() => String(props.signal?.acquire?.args?.instruction || '未命名 Signal'))
const tool = computed(() => String(props.signal?.acquire?.tool || '未选择工具'))
const isQkv = computed(() => tool.value.startsWith('qkv'))
const hasAiProcessing = computed(() => {
  const visit = (value: unknown): boolean => {
    if (!value || typeof value !== 'object') return false
    if (Array.isArray(value)) return value.some(visit)
    const object = value as Record<string, unknown>
    if (object.ai_processing || object.ai_extract) return true
    return Object.values(object).some(visit)
  }
  return visit(props.signal?.match) || visit(props.signal?.orchestrate?.produces) || visit(props.signal?.orchestrate?.output_processing)
})
const inputLabel = computed(() => isQkv.value ? '已投影变量 JSON records' : '完整 stdout / stderr')
const inputPlaceholder = computed(() => isQkv.value
  ? '[\n  {"description": "...", "host": "..."}\n]'
  : '粘贴当前 Signal 的完整 stdout / stderr 输出')
const sourceDescription = computed(() => ({
  pasted: '临时样本只用于本次预览，不写入 KBD 或现场证据。',
  fixture: '将从已加载的 hci-sim fixture 读取当前 Signal 的独立样本。',
  replay: '将从不可变 exec_id/artifact_id 回放记录读取当前 Signal 的独立样本。',
})[source.value])
const previewStatus = computed(() => {
  if (!previewRequested.value) return '暂无运行数据'
  if (!sampleInput.value.trim() && source.value === 'pasted') return '请提供试运行输入'
  return '等待 dry-run 服务执行'
})

function close(): void {
  emit('update:modelValue', false)
}

function requestPreview(): void {
  previewRequested.value = true
}

watch(() => props.modelValue, (visible) => {
  if (!visible) return
  verificationScope.value = 'signal'
  source.value = 'pasted'
  sampleInput.value = ''
  previewRequested.value = false
})

watch([sampleInput, source, verificationScope], () => {
  previewRequested.value = false
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
          <el-select v-model="source">
            <el-option label="临时样本（用户输入）" value="pasted" />
            <el-option label="hci-sim fixture（待选择）" value="fixture" />
            <el-option label="现场回放（待选择）" value="replay" />
          </el-select>
          <div class="field-hint">{{ sourceDescription }}</div>
        </el-form-item>

        <el-form-item v-if="source === 'pasted'" :label="`试运行输入（${inputLabel}）`">
          <el-input v-model="sampleInput" type="textarea" :rows="9" :placeholder="inputPlaceholder" resize="vertical" class="sample-input" />
        </el-form-item>

        <el-alert
          v-else
          type="info"
          :closable="false"
          show-icon
          title="数据集选择器将在 dry-run 服务接入后加载当前 Signal 的 fixture 或回放记录。"
        />
      </el-form>

      <section class="preview-panel" aria-label="配置效果预览">
        <div class="preview-heading">
          <span>配置效果预览</span>
          <el-tag size="small" type="info" effect="plain">独立数据集</el-tag>
        </div>
        <div class="preview-empty">
          <el-icon><WarningFilled /></el-icon>
          <strong>{{ previewStatus }}</strong>
          <p v-if="!previewRequested">提供一组输入后执行预览。结果会绑定当前 KBD revision、Signal 和处理范围。</p>
          <p v-else-if="sampleInput.trim() || source !== 'pasted'">预览执行链尚未接入。不会以浏览器规则或示例数据伪造 PASS/FAIL 结果。</p>
          <p v-else>临时样本为空，无法生成配置效果。</p>
        </div>
        <dl class="preview-context">
          <dt>处理对象</dt><dd>{{ signalId }}</dd>
          <dt>输入类型</dt><dd>{{ inputLabel }}</dd>
          <dt>调用链</dt><dd>生成后返回 trace_id</dd>
          <dt>保存目标</dt><dd>Bundle Draft verification_assets</dd>
        </dl>
      </section>
    </div>

    <template #footer>
      <div class="dialog-footer">
        <span>保存功能依赖 Bundle Draft 的 `verification_assets` 后端契约。</span>
        <div>
          <el-button @click="close">取消</el-button>
          <el-button type="primary" plain @click="requestPreview">解析预览</el-button>
          <el-tooltip content="等待 dry-run 与 Bundle Draft 验证资产接口接入" placement="top">
            <span><el-button type="primary" disabled>保存到 Bundle 草稿</el-button></span>
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
.preview-empty .el-icon { margin-bottom: 9px; color: var(--el-color-warning); font-size: 24px; }
.preview-empty strong { color: var(--el-text-color-primary); font-size: 13px; }
.preview-empty p { margin: 7px 0 0; color: var(--el-text-color-secondary); font-size: 12px; line-height: 1.55; }
.preview-context { display: grid; grid-template-columns: 74px minmax(0, 1fr); gap: 7px 8px; margin: 0 12px 14px; padding-top: 12px; border-top: 1px dashed var(--el-border-color); font-size: 12px; }
.preview-context dt { color: var(--el-text-color-secondary); }
.preview-context dd { margin: 0; overflow-wrap: anywhere; color: var(--el-text-color-regular); font-family: var(--el-font-family); }
.dialog-footer { display: flex; align-items: center; justify-content: space-between; gap: 12px; width: 100%; color: var(--el-text-color-secondary); font-size: 12px; text-align: left; }
.dialog-footer > div { display: flex; flex: 0 0 auto; gap: 8px; }
@media (max-width: 720px) { .dialog-grid { grid-template-columns: 1fr; } .dialog-footer { align-items: flex-start; flex-direction: column; } .dialog-footer > div { align-self: flex-end; flex-wrap: wrap; justify-content: flex-end; } }
</style>
