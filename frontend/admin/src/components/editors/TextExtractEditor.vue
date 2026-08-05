<script setup lang="ts">
/** QFK 声明式文本取值编辑器：行选择与列选择正交。 */
import { computed, watch } from 'vue'
import { formatKeywordInput, parseKeywordInput } from '../../utils/keywordInput'

const props = withDefaults(defineProps<{
  modelValue?: Record<string, any>
  defaultValueMode?: string
  wholeLineOnly?: boolean
}>(), { defaultValueMode: 'string', wholeLineOnly: false })

const emit = defineEmits<{ 'update:modelValue': [value: Record<string, any>] }>()

const extract = computed({
  get: () => props.modelValue || { type: 'text', rows: { mode: 'all' }, cardinality: 'exactly_one', source: 'stdout' },
  set: (value: Record<string, any>) => emit('update:modelValue', value),
})
const rows = computed(() => extract.value.rows || { mode: 'all' })
const columns = computed(() => Array.isArray(extract.value.columns) ? extract.value.columns : [])
const columnMode = computed(() => columns.value.length ? 'columns' : 'whole')

function setExtract(value: Record<string, any>) {
  extract.value = { ...value, type: 'text' }
}
function setField(key: string, value: any) {
  setExtract({ ...extract.value, [key]: value })
}
function removeField(key: string) {
  const value = { ...extract.value }
  delete value[key]
  setExtract(value)
}
function setRowsField(key: string, value: any) {
  setField('rows', { ...rows.value, [key]: value })
}
function listText(value: any): string {
  return Array.isArray(value) ? value.join('\n') : ''
}
function setStringList(target: Record<string, any>, key: string, value: string) {
  target[key] = value.split('\n').map(item => item.trim()).filter(Boolean)
  setExtract({ ...extract.value })
}
function setKeywordList(key: 'include' | 'exclude', value: string) {
  setRowsField(key, parseKeywordInput(value))
}
function setRowMode(mode: string) {
  if (mode === 'keywords') {
    setField('rows', {
      mode,
      scope: 'same_record',
      include: [],
      exclude: [],
      include_mode: 'all',
      exclude_mode: 'any',
      case_sensitive: true,
    })
  } else if (mode === 'indices') {
    setField('rows', { mode, basis: 'data', indices: [1], ranges: [] })
  } else {
    setField('rows', { mode: 'all' })
  }
}
function indexText(): string {
  return (rows.value.indices || []).join(', ')
}
function setIndices(value: string) {
  const indices = value.split(',').map(item => Number(item.trim())).filter(item => Number.isInteger(item) && item > 0)
  setRowsField('indices', [...new Set(indices)])
}
function rangeText(): string {
  return (rows.value.ranges || []).map((item: any) => `${item.start}-${item.end}`).join('\n')
}
function setRanges(value: string) {
  const ranges = value.split('\n').map(item => item.trim()).filter(Boolean).map(item => {
    const match = item.match(/^(\d+)\s*-\s*(\d+)$/)
    return match ? { start: Number(match[1]), end: Number(match[2]) } : null
  }).filter(Boolean)
  setRowsField('ranges', ranges)
}
function setColumnMode(mode: string) {
  if (mode === 'whole') {
    const value = { ...extract.value }
    delete value.parser
    delete value.header
    delete value.columns
    delete value.value_key
    delete value.delimiter
    setExtract(value)
    return
  }
  setExtract({
    ...extract.value,
    parser: 'whitespace_table',
    columns: [{ key: 'VALUE', selector: { by: 'index', index: 1 }, value_mode: props.defaultValueMode }],
    value_key: 'VALUE',
  })
}
function setParser(parser: string) {
  const value: Record<string, any> = { ...extract.value, parser }
  if (parser === 'delimited_table') value.delimiter ||= ':'
  else delete value.delimiter
  setExtract(value)
}
function setHeaderEnabled(enabled: boolean) {
  if (enabled) setField('header', { mode: 'contains', required: [], case_sensitive: false })
  else removeField('header')
}
function setHeaderCaseSensitive(value: boolean) {
  setField('header', { ...extract.value.header, case_sensitive: value })
}
function addColumn() {
  const next = columns.value.length + 1
  setField('columns', [
    ...columns.value,
    { key: `VALUE_${next}`, selector: { by: 'index', index: next }, value_mode: props.defaultValueMode },
  ])
}
function removeColumn(index: number) {
  const next = columns.value.filter((_: any, itemIndex: number) => itemIndex !== index)
  const value: Record<string, any> = { ...extract.value, columns: next }
  if (!next.some((item: any) => item.key === value.value_key)) value.value_key = next[0]?.key
  setExtract(value)
}
function updateColumn(index: number, patch: Record<string, any>) {
  const next = columns.value.map((item: any, itemIndex: number) => itemIndex === index ? { ...item, ...patch } : item)
  setField('columns', next)
}
function updateSelector(index: number, patch: Record<string, any>) {
  updateColumn(index, { selector: { ...columns.value[index].selector, ...patch } })
}
function setSelectorMode(index: number, by: string) {
  updateColumn(index, {
    selector: by === 'header' ? { by, name: '', aliases: [] } : { by, index: index + 1 },
  })
}
watch(() => props.modelValue, value => {
  if (!value || value.type !== 'text') setExtract({ type: 'text', rows: { mode: 'all' }, cardinality: 'exactly_one', source: 'stdout' })
}, { immediate: true })
</script>

<template>
  <div class="text-extract-editor">
    <el-form label-position="left" label-width="96px" size="small">
      <el-form-item label="行选择">
        <el-radio-group :model-value="rows.mode" @change="setRowMode">
          <el-radio-button value="all">全部行</el-radio-button>
          <el-radio-button value="keywords">按关键字</el-radio-button>
          <el-radio-button v-if="!wholeLineOnly" value="indices">按行号</el-radio-button>
        </el-radio-group>
      </el-form-item>
      <template v-if="rows.mode === 'keywords'">
        <el-form-item label="包含关键字">
          <el-input :model-value="formatKeywordInput(rows.include)" type="textarea" :rows="3" placeholder="每行一个关键字" @input="(value: string) => setKeywordList('include', value)" />
        </el-form-item>
        <el-form-item label="排除关键字">
          <el-input :model-value="formatKeywordInput(rows.exclude)" type="textarea" :rows="3" placeholder="每行一个关键字" @input="(value: string) => setKeywordList('exclude', value)" />
        </el-form-item>
        <el-form-item label="包含关系">
          <el-select :model-value="rows.include_mode || 'all'" @change="(value: string) => setRowsField('include_mode', value)">
            <el-option label="全部满足（AND）" value="all" /><el-option label="任一满足（OR）" value="any" />
          </el-select>
        </el-form-item>
        <el-form-item label="排除关系">
          <el-select :model-value="rows.exclude_mode || 'any'" @change="(value: string) => setRowsField('exclude_mode', value)">
            <el-option label="任一出现即排除（OR）" value="any" /><el-option label="全部同时出现才排除（AND）" value="all" />
          </el-select>
          <el-switch class="case-switch" :model-value="rows.case_sensitive ?? true" active-text="区分大小写" @change="(value: boolean) => setRowsField('case_sensitive', value)" />
        </el-form-item>
        <div class="field-hint">同一行先满足包含条件，再确认没有触发排除条件；每行一个字面量，中英文逗号属于关键字内容。</div>
      </template>
      <template v-else-if="rows.mode === 'indices'">
        <el-form-item label="行号基准">
          <el-select :model-value="rows.basis || 'data'" @change="(value: string) => setRowsField('basis', value)">
            <el-option label="数据行（不含表头）" value="data" /><el-option label="非空行" value="non_empty" /><el-option label="物理行（含空行/表头）" value="physical" />
          </el-select>
        </el-form-item>
        <el-form-item label="行号列表"><el-input :model-value="indexText()" placeholder="如 1, 3, 8（从 1 开始）" @input="setIndices" /></el-form-item>
        <el-form-item label="行号范围"><el-input :model-value="rangeText()" type="textarea" :rows="2" placeholder="每行一个范围，如 5-7" @input="setRanges" /></el-form-item>
      </template>

      <el-form-item v-if="!wholeLineOnly" label="列选择">
        <el-radio-group :model-value="columnMode" @change="setColumnMode">
          <el-radio-button value="whole">整行</el-radio-button><el-radio-button value="columns">一列或多列</el-radio-button>
        </el-radio-group>
      </el-form-item>
      <template v-if="!wholeLineOnly && columnMode === 'columns'">
        <el-form-item label="表格解析">
          <el-select :model-value="extract.parser || 'whitespace_table'" @change="setParser">
            <el-option label="空白分列" value="whitespace_table" /><el-option label="单字符分隔" value="delimited_table" />
          </el-select>
          <el-input v-if="extract.parser === 'delimited_table'" class="delimiter-input" maxlength="1" :model-value="extract.delimiter" placeholder=":" @input="(value: string) => setField('delimiter', value)" />
        </el-form-item>
        <el-form-item label="识别表头">
          <el-switch :model-value="Boolean(extract.header)" @change="setHeaderEnabled" />
        </el-form-item>
        <template v-if="extract.header">
          <el-form-item label="表头特征">
            <el-input :model-value="listText(extract.header.required)" type="textarea" :rows="2" placeholder="每行一个必含列名，如 Filesystem、Use%" @input="(value: string) => setStringList(extract.header, 'required', value)" />
          </el-form-item>
          <el-form-item label="表头大小写"><el-switch :model-value="extract.header.case_sensitive" active-text="区分大小写" @change="setHeaderCaseSensitive" /></el-form-item>
        </template>

        <div class="columns-editor">
          <div v-for="(column, index) in columns" :key="index" class="column-card">
            <el-input :model-value="column.key" placeholder="稳定 key，如 USE_PERCENT" @input="(value: string) => updateColumn(index, { key: value.toUpperCase() })" />
            <el-select :model-value="column.selector.by" @change="(value: string) => setSelectorMode(index, value)">
              <el-option label="按列号" value="index" /><el-option label="按表头列名" value="header" />
            </el-select>
            <el-input-number v-if="column.selector.by === 'index'" :model-value="column.selector.index" :min="1" @change="(value: number) => updateSelector(index, { index: value })" />
            <template v-else>
              <el-input :model-value="column.selector.name" placeholder="列名，如 Used / Use%" @input="(value: string) => updateSelector(index, { name: value })" />
              <el-input :model-value="(column.selector.aliases || []).join(', ')" placeholder="别名，逗号分隔（可选）" @input="(value: string) => updateSelector(index, { aliases: value.split(',').map(item => item.trim()).filter(Boolean) })" />
            </template>
            <el-select :model-value="column.value_mode || 'string'" @change="(value: string) => updateColumn(index, { value_mode: value })">
              <el-option label="文本" value="string" /><el-option label="整数" value="integer" /><el-option label="数字（35%→35）" value="number" /><el-option label="布尔值" value="boolean" />
            </el-select>
            <el-button text type="danger" @click="removeColumn(index)">删除</el-button>
          </div>
          <el-button text type="primary" @click="addColumn">+ 添加列</el-button>
        </div>
        <el-form-item label="主值列">
          <el-select :model-value="extract.value_key" clearable placeholder="Matcher/标量变量必须选择" @change="(value: string) => setField('value_key', value)">
            <el-option v-for="column in columns" :key="column.key" :label="column.key" :value="column.key" />
          </el-select>
        </el-form-item>
      </template>

      <el-form-item label="结果数量">
        <el-select :model-value="extract.cardinality || 'exactly_one'" @change="(value: string) => setField('cardinality', value)">
          <el-option label="必须唯一" value="exactly_one" /><el-option label="第一行" value="first" /><el-option label="最后一行" value="last" /><el-option label="全部行" value="all" />
        </el-select>
      </el-form-item>
      <el-form-item label="输出来源">
        <el-select :model-value="extract.source || 'stdout'" @change="(value: string) => setField('source', value)"><el-option label="stdout" value="stdout" /><el-option label="stderr" value="stderr" /></el-select>
      </el-form-item>
    </el-form>
    <div class="field-hint">{{ wholeLineOnly ? '关键字只负责筛选候选记录，结果保留完整一行，不截取列。' : '关键字/行号负责选行，表头/列号负责选列；行列提取完成后才进入 Matcher 或变量写入。所有行号、列号均从 1 开始。' }}</div>
  </div>
</template>

<style scoped>
.text-extract-editor { width: 100%; }
.case-switch { margin-left: 12px; }
.delimiter-input { width: 72px; margin-left: 8px; }
.columns-editor { display: flex; flex-direction: column; gap: 8px; margin: 0 0 12px 96px; }
.column-card { display: grid; grid-template-columns: minmax(120px, 1fr) minmax(110px, 140px) minmax(120px, 1fr) minmax(130px, 1fr) auto; gap: 8px; align-items: center; padding: 10px; border: 1px solid var(--el-border-color-light); border-radius: 6px; }
.field-hint { font-size: 12px; color: var(--el-text-color-secondary); line-height: 1.55; }
</style>
