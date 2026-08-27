<script setup lang="ts">
/**
 * QKV 产出变量处理编辑器。
 * QKV 的输入已经是 JSON 投影后的具体值；AI 是确定性取值后的可选再加工。
 */
import { computed } from 'vue'
import { Delete, InfoFilled, Plus, VideoPlay } from '@element-plus/icons-vue'
import MatcherEditor from './MatcherEditor.vue'

type ProcessingSpec = Record<string, any>
const props = defineProps<{ modelValue?: ProcessingSpec[]; produces?: ProcessingSpec[] }>()
const emit = defineEmits<{ 'update:modelValue': [value: ProcessingSpec[]]; 'dry-run': [processingIndex: number | null] }>()
const specs = computed(() => props.modelValue || [])
const inputOptions = computed(() => {
  const names = new Set<string>()
  for (const item of props.produces || []) {
    const value = String(item?.alias || item?.name || '').trim().toUpperCase()
    if (value) names.add('{{' + value + '}}')
  }
  return [...names].sort()
})
function inputOptionsFor(index: number): string[] {
  const names = new Set(inputOptions.value)
  for (const item of specs.value.slice(0, index)) {
    if (item?.mode === 'derive' && item?.name) names.add('{{' + String(item.name).toUpperCase() + '}}')
  }
  return [...names].sort()
}
const featureOptions = [
  { label: '虚拟机名称', value: 'vm_name' }, { label: '主机名称', value: 'host' },
  { label: '磁盘名称', value: 'disk_name' }, { label: '网口名称', value: 'interface_name' },
  { label: '错误码', value: 'error_code' }, { label: '源主机', value: 'source_host' },
  { label: '目标主机', value: 'destination_host' }, { label: '百分比', value: 'percent.current' },
  { label: '数字', value: 'number' }, { label: '源主机 → 目标主机', value: 'change_pair' },
]
const matcherTypes = ['keyword', 'regex', 'state', 'threshold', 'delta', 'trend', 'exists']
function defaultExtract(): ProcessingSpec { return { type: 'feature', feature: 'vm_name', cardinality: 'exactly_one' } }
function defaultAssert(): ProcessingSpec { return { type: 'threshold', expected: true, operator: '>', value: 90, aggregation: 'first_number' } }
function update(index: number, patch: ProcessingSpec): void { emit('update:modelValue', specs.value.map((item, i) => i === index ? { ...item, ...patch } : item)) }
function add(): void { emit('update:modelValue', [...specs.value, { mode: 'derive', input: inputOptions.value[0] || '', name: '', type: 'string', extract: defaultExtract() }]) }
function remove(index: number): void { emit('update:modelValue', specs.value.filter((_, i) => i !== index)) }
function setMode(index: number, mode: string): void {
  if (mode === 'assert') update(index, { mode, name: undefined, type: undefined, extract: undefined, match: defaultAssert() })
  else update(index, { mode, match: undefined, name: '', type: 'string', extract: defaultExtract() })
}
function setExtractType(index: number, type: string): void {
  const current = specs.value[index]?.extract || defaultExtract()
  if (type === 'ai') {
    update(index, { extract: { type: 'feature', feature: current.feature || 'vm_name', cardinality: current.cardinality || 'exactly_one', ai_processing: { contract_version: 1, mode: 'extract', instruction: current.ai_processing?.instruction || '', output_type: 'string' } } })
    return
  }
  const next: ProcessingSpec = { type, cardinality: current.cardinality || (type === 'split' ? 'all' : 'exactly_one') }
  if (type === 'feature') next.feature = current.feature || 'vm_name'
  if (type === 'split') next.separator = current.separator || ','
  update(index, { extract: next })
}
function setExtractField(index: number, key: string, value: any): void { update(index, { extract: { ...(specs.value[index]?.extract || defaultExtract()), [key]: value } }) }
function aiProcessingMode(item: ProcessingSpec): string { return item.extract?.ai_processing?.mode || 'extract' }
function setAiProcessingMode(index: number, mode: string): void {
  const current = specs.value[index]?.extract || defaultExtract()
  const aiProcessing = current.ai_processing || {}
  update(index, {
    extract: {
      ...current,
      ai_processing: { contract_version: 1, ...aiProcessing, mode, output_type: aiProcessing.output_type || 'string' },
    },
  })
}
function setAiInstruction(index: number, instruction: string): void {
  const current = specs.value[index]?.extract || defaultExtract()
  update(index, { extract: { ...current, ai_processing: { contract_version: 1, ...(current.ai_processing || {}), mode: aiProcessingMode(specs.value[index]), instruction } } })
}
function setAiOutputField(index: number, key: string, value: string): void {
  const current = specs.value[index]?.extract || defaultExtract()
  update(index, { extract: { ...current, ai_processing: { contract_version: 1, ...(current.ai_processing || {}), [key]: value } } })
}
function extractionMode(item: ProcessingSpec): string { return item.extract?.ai_processing ? 'ai' : (item.extract?.type || 'feature') }
function setMatch(index: number, match: ProcessingSpec): void { update(index, { match }) }
</script>

<template>
  <div class="qkv-output-processing-editor">
    <div class="processing-header">
      <div class="processing-title"><el-icon><InfoFilled /></el-icon><span>产出变量处理</span></div>
      <div class="processing-header-actions">
        <el-button text type="primary" size="small" :icon="VideoPlay" @click="emit('dry-run', specs.length ? specs.length - 1 : null)">试运行</el-button>
        <el-button text type="primary" size="small" :icon="Plus" @click="add">添加处理</el-button>
      </div>
    </div>
    <el-alert v-if="specs.length === 0" type="info" :closable="false" title="可选：对 QKV 产出变量进一步处理，包括派生变量和断言判断。" />
    <div v-for="(item, index) in specs" :key="index" class="processing-unit">
      <div class="unit-header"><span>处理单元 {{ index + 1 }}</span><span><el-button text type="primary" size="small" :icon="VideoPlay" @click="emit('dry-run', index)">试运行</el-button><el-button text type="danger" size="small" :icon="Delete" @click="remove(index)">删除</el-button></span></div>
      <template v-if="item.mode !== 'assert'">
        <section class="processing-step">
          <div class="step-header"><span class="stage-number">1</span><div><strong>派生变量</strong><small>从已有具体值提取并写入变量池</small></div></div>
          <div class="processing-grid derive-grid">
            <label>处理方式<el-select class="processing-control" popper-class="processing-select-popper" :model-value="item.mode || 'derive'" size="small" @change="(value: string) => setMode(index, value)"><el-option label="派生变量" value="derive" /><el-option label="断言判断" value="assert" /></el-select></label>
            <label>输入变量<el-select class="processing-control" popper-class="processing-select-popper" :model-value="item.input" filterable size="small" placeholder="选择已有具体值" @change="(value: string) => update(index, { input: value })"><el-option v-for="value in inputOptionsFor(index)" :key="value" :label="value" :value="value" /></el-select></label>
            <label>输出变量<el-input :model-value="item.name" placeholder="如 VM_NAME" size="small" @input="(value: string) => update(index, { name: value.toUpperCase() })" /></label>
          </div>
          <div class="processing-grid derive-grid second-row">
            <label>提取方式<el-select class="processing-control" popper-class="processing-select-popper" :model-value="extractionMode(item)" size="small" @change="(value: string) => setExtractType(index, value)"><el-option label="特征" value="feature" /><el-option label="分隔" value="split" /><el-option label="AI" value="ai" /></el-select></label>
            <label v-if="extractionMode(item) === 'feature'">特征名<el-select class="processing-control" popper-class="processing-select-popper" :model-value="item.extract?.feature" size="small" @change="(value: string) => setExtractField(index, 'feature', value)"><el-option v-for="option in featureOptions" :key="option.value" :label="option.label" :value="option.value" /></el-select></label>
            <label v-else-if="extractionMode(item) === 'split'">分隔符<el-input class="processing-control" :model-value="item.extract?.separator" placeholder="例如：（）、<>、【】、：" size="small" @input="(value: string) => setExtractField(index, 'separator', value)" /></label>
            <template v-else>
              <label>AI 处理方式<el-radio-group :model-value="aiProcessingMode(item)" size="small" @change="(value: string) => setAiProcessingMode(index, value)"><el-radio-button value="extract">原文取值</el-radio-button><el-radio-button value="derive">智能推导</el-radio-button></el-radio-group></label>
              <label class="ai-prompt">处理说明 *<el-input :model-value="item.extract?.ai_processing?.instruction" type="textarea" :rows="2" maxlength="1000" show-word-limit placeholder="描述如何对上一步确定性输出进行再加工" @input="(value: string) => setAiInstruction(index, value)" /></label>
              <label>输出类型<el-select class="processing-control" :model-value="item.extract?.ai_processing?.output_type || 'string'" size="small" @change="(value: string) => setAiOutputField(index, 'output_type', value)"><el-option label="布尔值（1/0）" value="boolean" /><el-option label="数值" value="number" /><el-option label="文本" value="string" /><el-option label="数组" value="array" /></el-select></label>
              <label v-if="item.extract?.ai_processing?.output_type === 'array'">数组元素类型<el-select class="processing-control" :model-value="item.extract?.ai_processing?.item_type || 'string'" size="small" @change="(value: string) => setAiOutputField(index, 'item_type', value)"><el-option label="布尔值" value="boolean" /><el-option label="数值" value="number" /><el-option label="文本" value="string" /></el-select></label>
            </template>
            <label>变量类型<el-select class="processing-control" popper-class="processing-select-popper" :model-value="item.type || 'string'" size="small" @change="(value: string) => update(index, { type: value })"><el-option label="字符串" value="string" /><el-option label="整数" value="integer" /><el-option label="数字" value="number" /><el-option label="百分比" value="percentage" /><el-option label="布尔值" value="boolean" /><el-option label="数组" value="array" /></el-select></label>
          </div>
          <div class="step-output final"><span>最终输出</span><code>{{ item.name || '变量名' }} → 写入变量池</code></div>
        </section>
      </template>
      <template v-else>
        <section class="processing-step">
          <div class="step-header"><span class="stage-number">1</span><div><strong>断言判断</strong><small>对 QKV 已取得的具体值复用 QFK Matcher</small></div></div>
          <div class="processing-grid assert-grid">
            <label>处理方式<el-select class="processing-control" popper-class="processing-select-popper" :model-value="item.mode" size="small" @change="(value: string) => setMode(index, value)"><el-option label="派生变量" value="derive" /><el-option label="断言判断" value="assert" /></el-select></label>
            <label>输入变量<el-select class="processing-control" popper-class="processing-select-popper" :model-value="item.input" filterable size="small" placeholder="选择已有具体值" @change="(value: string) => update(index, { input: value })"><el-option v-for="value in inputOptionsFor(index)" :key="value" :label="value" :value="value" /></el-select></label>
          </div>
          <div class="matcher-heading">判断类型及要求</div>
          <MatcherEditor :model-value="item.match || defaultAssert()" :allowed-types="matcherTypes" embedded :show-header="false" :show-extract="false" :show-step-title="false" @update:model-value="(value: ProcessingSpec) => setMatch(index, value)" />
          <div class="step-output final"><span>最终输出</span><code>True / False</code></div>
        </section>
      </template>
    </div>
    <el-empty v-if="specs.length === 0" :image-size="48" description="暂无处理单元，请添加处理" />
  </div>
</template>

<style scoped>
.qkv-output-processing-editor { width: 100%; margin-top: 12px; padding: 14px; border: 1px solid var(--el-border-color); border-radius: 6px; background: var(--el-fill-color-extra-light); }
.processing-header, .unit-header, .step-header, .step-output { display: flex; align-items: center; }
.processing-header, .unit-header { justify-content: space-between; gap: 12px; }
.processing-header-actions { display: flex; align-items: center; gap: 8px; }
.processing-title { display: flex; align-items: center; gap: 6px; color: var(--el-text-color-primary); font-weight: 600; }
.processing-title .el-icon { color: var(--el-color-primary); }
.processing-unit { margin-top: 12px; padding: 12px; border: 1px solid var(--el-border-color-light); border-radius: 6px; background: var(--el-fill-color-blank); }
.unit-header { margin-bottom: 10px; font-size: 13px; font-weight: 600; }
.processing-step { padding: 12px; border: 1px solid var(--el-color-primary-light-8); border-radius: 6px; background: var(--el-fill-color-extra-light); }
.step-header { gap: 9px; margin-bottom: 12px; }
.step-header > div { min-width: 0; }
.step-header small { display: block; margin-top: 3px; color: var(--el-text-color-secondary); font-size: 12px; font-weight: 400; }
.stage-number { display: inline-flex; align-items: center; justify-content: center; flex: 0 0 auto; width: 22px; height: 22px; border-radius: 50%; background: var(--el-color-primary); color: #fff; font-size: 12px; font-weight: 600; }
.processing-grid { display: grid; gap: 10px 14px; }
.derive-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.assert-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.second-row { margin-top: 10px; }
.processing-grid label { display: flex; flex-direction: column; gap: 5px; color: var(--el-text-color-secondary); font-size: 12px; }
.processing-control, .processing-grid :deep(.el-select), .processing-grid :deep(.el-input), .ai-prompt { width: 100%; }
.processing-grid :deep(.el-select__selected-item), .processing-grid :deep(.el-input__inner) { font-size: 13px; }
.ai-prompt { grid-column: span 2; }
.matcher-heading { margin: 16px 0 8px; color: var(--el-text-color-primary); font-size: 13px; font-weight: 600; }
.step-output { justify-content: space-between; gap: 8px; margin-top: 12px; padding-top: 10px; border-top: 1px dashed var(--el-border-color); color: var(--el-text-color-secondary); font-size: 12px; }
.step-output code { color: var(--el-color-primary); font-family: inherit; }
@media (max-width: 900px) { .derive-grid, .assert-grid { grid-template-columns: 1fr; } .ai-prompt { grid-column: auto; } }
</style>
