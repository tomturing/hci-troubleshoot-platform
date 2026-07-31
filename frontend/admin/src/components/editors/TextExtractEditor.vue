<script setup lang="ts">
/**
 * TextExtractEditor - QFK 受控文本取值编辑器。
 *
 * Produces 与 Matcher 共用本组件和后端 ``textExtract`` 契约：界面用“筛选行 +
 * 第 N 列”的 grep/awk 心智模型，但平台只执行安全的声明式子集，不执行任意脚本。
 */
import { computed, watch } from 'vue'

const props = withDefaults(defineProps<{
  modelValue?: Record<string, any>
  valueMode?: boolean
}>(), { valueMode: false })

const emit = defineEmits<{ 'update:modelValue': [value: Record<string, any>] }>()

const extract = computed({
  get: (): Record<string, any> => ({
    type: 'text', include: [], exclude: [], include_mode: 'all', case_sensitive: true,
    column_mode: 'whole', delimiter: 'whitespace', cardinality: 'exactly_one', source: 'stdout',
    ...(props.modelValue || {}),
  }),
  set: (value) => emit('update:modelValue', value),
})

function setField(key: string, value: any) {
  extract.value = { ...extract.value, [key]: value }
}
function lineText(key: 'include' | 'exclude') {
  return (extract.value[key] || []).join('\n')
}
function setLines(key: 'include' | 'exclude', value: string) {
  setField(key, value.split('\n').map((line) => line.trim()).filter(Boolean))
}
watch(() => props.modelValue, (value) => {
  if (!value || value.type !== 'text') emit('update:modelValue', extract.value)
}, { immediate: true })
</script>

<template>
  <div class="text-extract-editor">
    <el-form label-position="left" label-width="92px" size="small">
      <el-form-item label="筛选行（包含）">
        <el-input :model-value="lineText('include')" type="textarea" :rows="2" placeholder="每行一个条件；默认全部满足（安全 grep）" @input="(value: string) => setLines('include', value)" />
      </el-form-item>
      <el-form-item label="筛选行（不含）">
        <el-input :model-value="lineText('exclude')" type="textarea" :rows="2" placeholder="可选，每行一个排除条件" @input="(value: string) => setLines('exclude', value)" />
      </el-form-item>
      <el-form-item label="提取值">
        <div class="inline-controls">
          <el-select :model-value="extract.column_mode" @update:model-value="(value) => setField('column_mode', value)">
            <el-option label="整行" value="whole" />
            <el-option label="第 N 列" value="index" />
            <el-option label="从第 N 列到末尾" value="from_index" />
          </el-select>
          <el-input-number v-if="extract.column_mode !== 'whole'" :model-value="extract.column" :min="1" :max="999" @update:model-value="(value) => setField('column', value)" />
        </div>
      </el-form-item>
      <el-form-item v-if="valueMode" label="取值类型">
        <el-select :model-value="extract.value_mode || 'number'" @update:model-value="(value) => setField('value_mode', value)">
          <el-option label="数值（54% → 54）" value="number" />
          <el-option label="整数" value="integer" />
          <el-option label="文本" value="string" />
          <el-option label="布尔值" value="boolean" />
        </el-select>
      </el-form-item>
      <el-form-item label="高级设置">
        <details class="extract-advanced">
          <summary>默认：空白分隔、区分大小写、唯一匹配、stdout</summary>
          <div class="advanced-grid">
            <span>包含关系</span><el-select :model-value="extract.include_mode" @update:model-value="(value) => setField('include_mode', value)"><el-option label="全部满足（AND）" value="all" /><el-option label="任一满足（OR）" value="any" /></el-select>
            <span>区分大小写</span><el-switch :model-value="extract.case_sensitive" @update:model-value="(value) => setField('case_sensitive', value)" />
            <span>匹配数量</span><el-select :model-value="extract.cardinality" @update:model-value="(value) => setField('cardinality', value)"><el-option label="必须唯一" value="exactly_one" /><el-option label="第一行" value="first" /><el-option label="最后一行" value="last" /><el-option label="全部行" value="all" /></el-select>
            <span>输出来源</span><el-select :model-value="extract.source" @update:model-value="(value) => setField('source', value)"><el-option label="stdout" value="stdout" /><el-option label="stderr" value="stderr" /></el-select>
            <span>分隔符</span><el-input :model-value="extract.delimiter" placeholder="whitespace 或单字符" @update:model-value="(value) => setField('delimiter', value)" />
          </div>
        </details>
      </el-form-item>
    </el-form>
    <div class="field-hint">列号从 1 开始，语义等价于安全的 <code>awk '{print $N}'</code>；平台不执行 awk、grep 或 Shell 管道。</div>
  </div>
</template>

<style scoped>
.inline-controls { display: flex; gap: 8px; width: 100%; }
.inline-controls .el-select { flex: 1; }
.extract-advanced { width: 100%; color: var(--el-text-color-regular); }
.extract-advanced summary { cursor: pointer; }
.advanced-grid { display: grid; grid-template-columns: 88px minmax(0, 1fr); gap: 8px; align-items: center; margin-top: 10px; }
.field-hint { font-size: 12px; color: var(--el-text-color-secondary); line-height: 1.55; }
</style>
