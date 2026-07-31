<script setup lang="ts">
/** Matcher 与 Produces 共用的 ValueExtract 联合编辑器。 */
import { computed, watch } from 'vue'
import TextExtractEditor from './TextExtractEditor.vue'

const props = withDefaults(defineProps<{
  modelValue?: Record<string, any>
  defaultValueMode?: string
}>(), { defaultValueMode: 'string' })
const emit = defineEmits<{ 'update:modelValue': [value: Record<string, any>] }>()

const extract = computed({
  get: () => props.modelValue || completeExtract(),
  set: (value: Record<string, any>) => emit('update:modelValue', value),
})
const mode = computed(() => {
  if (extract.value.type === 'json') return 'json'
  return extract.value.columns?.length ? 'text' : 'complete'
})

function completeExtract(): Record<string, any> {
  return {
    type: 'text', rows: { mode: 'all' }, cardinality: 'all', source: 'stdout',
    value_mode: props.defaultValueMode,
  }
}
function setMode(next: string) {
  if (next === 'json') {
    extract.value = {
      type: 'json', path: '', cardinality: 'exactly_one', source: 'stdout',
      value_mode: props.defaultValueMode,
    }
  } else if (next === 'text') {
    extract.value = {
      type: 'text', rows: { mode: 'all' }, cardinality: 'exactly_one', source: 'stdout',
      value_mode: props.defaultValueMode,
    }
  } else extract.value = completeExtract()
}
function setField(key: string, value: any) {
  extract.value = { ...extract.value, [key]: value }
}
function setExtract(value: Record<string, any>) {
  extract.value = value
}

watch(() => props.modelValue, value => {
  if (!value || !['text', 'json'].includes(String(value.type))) extract.value = completeExtract()
}, { immediate: true })
</script>

<template>
  <div class="value-extract-editor">
    <div class="extract-order-title">一、取值配置</div>
    <el-form label-position="left" label-width="96px" size="small">
      <el-form-item label="取值方式">
        <el-radio-group :model-value="mode" @change="setMode">
          <el-radio-button value="complete">完整输出</el-radio-button>
          <el-radio-button value="text">文本行列</el-radio-button>
          <el-radio-button value="json">JSON 路径</el-radio-button>
        </el-radio-group>
      </el-form-item>
    </el-form>
    <TextExtractEditor
      v-if="mode === 'text'"
      :model-value="extract"
      :default-value-mode="defaultValueMode"
      @update:model-value="setExtract"
    />
    <el-form v-else-if="mode === 'json'" label-position="left" label-width="96px" size="small">
      <el-form-item label="JSON 路径"><el-input :model-value="extract.path" placeholder="如 data[0].status；空表示根节点" @input="(value: string) => setField('path', value)" /></el-form-item>
      <el-form-item label="取值类型">
        <el-select :model-value="extract.value_mode || defaultValueMode" @change="(value: string) => setField('value_mode', value)">
          <el-option label="文本" value="string" /><el-option label="整数" value="integer" /><el-option label="数字" value="number" /><el-option label="布尔值" value="boolean" /><el-option label="数组" value="array" /><el-option label="对象" value="object" /><el-option label="对象数组" value="array<object>" />
        </el-select>
      </el-form-item>
      <el-form-item label="结果数量">
        <el-select :model-value="extract.cardinality || 'exactly_one'" @change="(value: string) => setField('cardinality', value)"><el-option label="必须唯一" value="exactly_one" /><el-option label="第一项" value="first" /><el-option label="最后一项" value="last" /><el-option label="全部项" value="all" /></el-select>
      </el-form-item>
      <el-form-item label="输出来源"><el-select :model-value="extract.source || 'stdout'" @change="(value: string) => setField('source', value)"><el-option label="stdout" value="stdout" /><el-option label="stderr" value="stderr" /></el-select></el-form-item>
      <div class="field-hint">只支持受控点号和数组下标，例如 <code>data[0].status</code>；不执行 jq、函数、过滤器或通配符。</div>
    </el-form>
    <el-alert v-else title="完整输出仍通过统一 Extractor 产生值，不走历史全文旁路。" type="info" :closable="false" />
  </div>
</template>

<style scoped>
.value-extract-editor { width: 100%; padding: 12px; border: 1px solid var(--el-border-color-light); border-radius: 6px; background: var(--el-fill-color-blank); }
.extract-order-title { margin-bottom: 12px; font-weight: 600; color: var(--el-text-color-primary); }
.field-hint { font-size: 12px; color: var(--el-text-color-secondary); line-height: 1.55; }
</style>
