<script setup lang="ts">
/** Matcher 与 Produces 共用的 ValueExtract 联合编辑器。 */
import { computed, watch } from 'vue'
import TextExtractEditor from './TextExtractEditor.vue'

const props = withDefaults(defineProps<{
  modelValue?: Record<string, any>
  defaultValueMode?: string
  embedded?: boolean
  showTitle?: boolean
  consumerKind?: 'matcher' | 'produce'
}>(), { defaultValueMode: 'string', embedded: false, showTitle: true })
const emit = defineEmits<{ 'update:modelValue': [value: Record<string, any>] }>()

const extract = computed({
  get: () => props.modelValue || completeExtract(),
  set: (value: Record<string, any>) => emit('update:modelValue', value),
})
const mode = computed(() => {
  if (extract.value.type === 'json') return 'json'
  // 不含列解析配置的 text extract 表示“完整行”：它可以配置候选关键字，
  // 但不会截列。文本行列模式必须显式包含受控 parser/columns。
  const isTextSelection = Boolean(
    extract.value.columns?.length
    || extract.value.parser
    || extract.value.header
    || extract.value.value_key
    || extract.value.delimiter
  )
  return isTextSelection ? 'text' : 'complete'
})
const isNumericConsumer = computed(() => props.consumerKind === 'matcher' && ['number', 'integer'].includes(props.defaultValueMode))

function completeExtract(): Record<string, any> {
  return {
    type: 'text', rows: { mode: 'all' }, cardinality: 'all', source: 'stdout',
    value_mode: props.defaultValueMode,
  }
}
function setMode(next: string) {
  const aiExtract = extract.value.ai_extract
  if (next === 'json') {
    extract.value = {
      type: 'json', path: '', cardinality: 'exactly_one', source: 'stdout',
      value_mode: props.defaultValueMode,
    }
  } else if (next === 'text') {
    extract.value = {
      type: 'text',
      rows: { mode: 'all' },
      parser: 'whitespace_table',
      columns: [{ key: 'VALUE', selector: { by: 'index', index: 1 }, value_mode: props.defaultValueMode }],
      value_key: 'VALUE',
      cardinality: 'exactly_one', source: 'stdout',
      value_mode: props.defaultValueMode,
    }
  } else extract.value = completeExtract()
  if (next !== 'json' && aiExtract) extract.value = { ...extract.value, ai_extract: aiExtract }
}
function setField(key: string, value: any) {
  extract.value = { ...extract.value, [key]: value }
}
function setExtract(value: Record<string, any>) {
  extract.value = value
}
function setAiInstruction(value: string) {
  const next = { ...extract.value }
  const instruction = value.trim()
  if (instruction) next.ai_extract = { instruction }
  else delete next.ai_extract
  extract.value = next
}

watch(() => props.modelValue, value => {
  if (!value || !['text', 'json'].includes(String(value.type))) extract.value = completeExtract()
}, { immediate: true })
</script>

<template>
  <div class="value-extract-editor" :class="{ embedded }">
    <div v-if="showTitle" class="extract-order-title">第一步：取值</div>
    <el-form label-position="left" label-width="96px" size="small">
      <el-form-item label="取值方式">
        <el-radio-group :model-value="mode" @change="setMode">
          <el-radio-button value="complete">完整行</el-radio-button>
          <el-radio-button value="text">文本行列</el-radio-button>
          <el-radio-button value="json">JSON 路径</el-radio-button>
        </el-radio-group>
      </el-form-item>
    </el-form>
    <TextExtractEditor
      v-if="mode === 'text' || mode === 'complete'"
      :model-value="extract"
      :default-value-mode="defaultValueMode"
      :whole-line-only="mode === 'complete'"
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
    <el-form v-if="mode !== 'json'" label-position="left" label-width="96px" size="small" class="ai-extract-form">
      <el-form-item label="AI 提取">
        <el-input
          :model-value="extract.ai_extract?.instruction || ''"
          type="textarea"
          :rows="2"
          maxlength="1000"
          show-word-limit
          placeholder="可选，例如：提取其中的第一个 IP 地址"
          @input="setAiInstruction"
        />
      </el-form-item>
      <div class="field-hint">
        可选。平台先按当前取值配置从完整输出确定候选行，再让 AI 从候选原文中提取值；AI 返回值和引用行必须可逐字回查，否则本次信号失败。
        <template v-if="isNumericConsumer">当前为数值判断：threshold 需要一个数；delta/trend 需要 AI 按日志出现顺序返回数值数组，第二步只做确定性比较。</template>
        <template v-else>当前为文本判断/产出：AI 仅提供已溯源的取值证据，命中结论仍由确定性规则决定。</template>
      </div>
    </el-form>
  </div>
</template>

<style scoped>
.value-extract-editor { width: 100%; padding: 12px; border: 1px solid var(--el-border-color-light); border-radius: 6px; background: var(--el-fill-color-blank); }
.value-extract-editor.embedded { padding: 0; border: 0; border-radius: 0; background: transparent; }
.extract-order-title { margin-bottom: 12px; font-weight: 600; color: var(--el-text-color-primary); }
.field-hint { font-size: 12px; color: var(--el-text-color-secondary); line-height: 1.55; }
.ai-extract-form { margin-top: 12px; }
</style>
