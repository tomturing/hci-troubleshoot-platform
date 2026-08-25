<script setup lang="ts">
/** QKV 后处理编辑器：复用 QFK 的“取值 → 判断/产出”词汇。
 * QKV produces 已经是 JSON 路径投影出的具体值，因此这里不再显示 JSON 路径、trim、大小写等重复操作。
 */
import { computed } from 'vue'
import { Delete, Plus } from '@element-plus/icons-vue'

type ProcessingSpec = Record<string, any>
const props = defineProps<{ modelValue?: ProcessingSpec[]; produces?: ProcessingSpec[] }>()
const emit = defineEmits<{ 'update:modelValue': [value: ProcessingSpec[]] }>()
const specs = computed(() => props.modelValue || [])
const inputOptions = computed(() => {
  const names = new Set<string>()
  for (const item of props.produces || []) {
    const value = String(item?.alias || item?.name || '').trim().toUpperCase()
    if (value) names.add(`{{${value}}}`)
  }
  return [...names].sort()
})
function inputOptionsFor(index: number): string[] {
  const names = new Set(inputOptions.value)
  for (const item of specs.value.slice(0, index)) {
    if (item?.mode === 'derive' && item?.name) names.add(`{{${String(item.name).toUpperCase()}}}`)
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
const matcherTypes = [
  { label: '关键字匹配', value: 'keyword' }, { label: '正则表达式', value: 'regex' },
  { label: '状态判定', value: 'state' }, { label: '数值阈值', value: 'threshold' },
  { label: '首末差值', value: 'delta' }, { label: '变化趋势', value: 'trend' }, { label: '存在性判定', value: 'exists' },
]
function update(index: number, patch: ProcessingSpec): void { emit('update:modelValue', specs.value.map((item, i) => i === index ? { ...item, ...patch } : item)) }
function add(): void {
  emit('update:modelValue', [...specs.value, { mode: 'derive', input: inputOptions.value[0] || '', name: '', type: 'string', extract: { type: 'feature', feature: 'vm_name', cardinality: 'exactly_one' } }])
}
function remove(index: number): void { emit('update:modelValue', specs.value.filter((_, i) => i !== index)) }
function setMode(index: number, mode: string): void {
  if (mode === 'assert') update(index, { mode, name: undefined, type: undefined, extract: undefined, match: { type: 'threshold', expected: true, operator: '>', value: 90, aggregation: 'first_number' } })
  else update(index, { mode, match: undefined, name: '', type: 'string', extract: { type: 'feature', feature: 'vm_name', cardinality: 'exactly_one' } })
}
function setMatchType(index: number, type: string): void {
  const current = specs.value[index]?.match || {}
  const next: ProcessingSpec = { ...current, type, expected: true }
  if (['threshold', 'delta'].includes(type)) Object.assign(next, { operator: '>', value: 90 })
  if (type === 'trend') Object.assign(next, { direction: 'increasing', value: 0 })
  if (['keyword', 'regex', 'state'].includes(type)) next.pattern = ''
  update(index, { match: next })
}
</script>

<template>
  <div class="qkv-output-processing-editor">
    <div class="processing-editor-header"><div><strong>输出后处理</strong><span>沿用 QFK 的处理单元；QKV 只增加特征提取和分割</span></div><el-button text type="primary" size="small" :icon="Plus" @click="add">添加处理</el-button></div>
    <el-alert v-if="specs.length === 0" type="info" :closable="false" title="可选：从 DESCRIPTION 提取 VM_NAME，或对已有具体值执行 QFK 判断。" />
    <div v-for="(item, index) in specs" :key="index" class="processing-card">
      <div class="processing-card-title"><span>处理单元 {{ index + 1 }}</span><el-button text type="danger" size="small" :icon="Delete" @click="remove(index)">删除</el-button></div>
      <div class="processing-grid">
        <label>处理方式<el-select :model-value="item.mode || 'derive'" size="small" @change="(v: string) => setMode(index, v)"><el-option label="派生变量" value="derive" /><el-option label="断言判断" value="assert" /></el-select></label>
        <label class="wide">输入变量<el-select :model-value="item.input" filterable size="small" placeholder="选择已有具体值" @change="(v: string) => update(index, { input: v })"><el-option v-for="value in inputOptionsFor(index)" :key="value" :label="value" :value="value" /></el-select></label>
        <template v-if="item.mode !== 'assert'">
          <label>变量名<el-input :model-value="item.name" placeholder="VM_NAME" size="small" @input="(v: string) => update(index, { name: v.toUpperCase() })" /></label>
          <label>变量类型<el-select :model-value="item.type || 'string'" size="small" @change="(v: string) => update(index, { type: v })"><el-option label="字符串" value="string" /><el-option label="整数" value="integer" /><el-option label="数字" value="number" /><el-option label="百分比" value="percentage" /><el-option label="数组" value="array" /></el-select></label>
          <label>提取方式<el-select :model-value="item.extract?.type || 'feature'" size="small" @change="(v: string) => update(index, { extract: { ...(item.extract || {}), type: v } })"><el-option label="特征提取" value="feature" /><el-option label="分割" value="split" /></el-select></label>
          <label v-if="item.extract?.type !== 'split'" class="wide">特征<el-select :model-value="item.extract?.feature" size="small" @change="(v: string) => update(index, { extract: { ...item.extract, feature: v } })"><el-option v-for="option in featureOptions" :key="option.value" :label="option.label" :value="option.value" /></el-select></label>
          <label v-else>分隔符<el-input :model-value="item.extract?.separator" placeholder=",、→" size="small" @input="(v: string) => update(index, { extract: { ...item.extract, separator: v } })" /></label>
          <label>基数<el-select :model-value="item.extract?.cardinality || 'exactly_one'" size="small" @change="(v: string) => update(index, { extract: { ...item.extract, cardinality: v } })"><el-option label="必须唯一" value="exactly_one" /><el-option label="第一项" value="first" /><el-option label="最后一项" value="last" /><el-option label="全部项（数组）" value="all" /></el-select></label>
          <div class="field-hint wide">QKV 已从 JSON 路径取得具体值；不再重复配置 JSON 路径、去空白或大小写转换。AI 兜底沿用 QFK 的 `extract.ai_extract.instruction`，仅在特征/分割确定性提取失败后执行。</div>
        </template>
        <template v-else>
          <label>判断类型<el-select :model-value="item.match?.type || 'threshold'" size="small" @change="(v: string) => setMatchType(index, v)"><el-option v-for="option in matcherTypes" :key="option.value" :label="option.label" :value="option.value" /></el-select></label>
          <label v-if="['keyword', 'regex', 'state'].includes(item.match?.type)" class="wide">匹配内容<el-input :model-value="item.match?.pattern" size="small" @input="(v: string) => update(index, { match: { ...item.match, pattern: v } })" /></label>
          <label v-if="['threshold', 'delta'].includes(item.match?.type)">比较符<el-select :model-value="item.match?.operator || '>'" size="small" @change="(v: string) => update(index, { match: { ...item.match, operator: v } })"><el-option v-for="operator in ['>', '>=', '<', '<=', '==', '!=']" :key="operator" :label="operator" :value="operator" /></el-select></label>
          <label v-if="['threshold', 'delta', 'trend'].includes(item.match?.type)">比较值<el-input :model-value="item.match?.value" size="small" @input="(v: string) => update(index, { match: { ...item.match, value: v } })" /></label>
          <div class="field-hint wide">断言直接复用 QFK Matcher：对 QKV 已投影的具体值执行关键字、状态、阈值、差值、趋势或存在性判断。</div>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
.qkv-output-processing-editor { margin-top: 8px; padding: 10px; border: 1px solid var(--el-border-color); background: var(--el-fill-color-lighter); }
.processing-editor-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
.processing-editor-header span { margin-left: 8px; color: var(--el-text-color-secondary); font-size: 12px; }
.processing-card { margin-top: 8px; padding: 10px; background: var(--el-bg-color); border: 1px solid var(--el-border-color-light); }
.processing-card-title { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; font-size: 13px; }
.processing-grid { display: grid; grid-template-columns: repeat(3, minmax(140px, 1fr)); gap: 8px 12px; }
.processing-grid label { display: flex; flex-direction: column; gap: 4px; color: var(--el-text-color-secondary); font-size: 12px; }
.wide { grid-column: span 2; }
.field-hint { color: var(--el-text-color-secondary); font-size: 12px; line-height: 1.5; }
@media (max-width: 900px) { .processing-grid { grid-template-columns: 1fr; } .wide { grid-column: auto; } }
</style>
