<script setup lang="ts">
/** QKV 输出后处理编辑器：采集完成后对同一 Signal 的结果做派生或断言。 */
import { computed } from 'vue'
import { Delete, Plus } from '@element-plus/icons-vue'

type ProcessingSpec = Record<string, any>

const props = defineProps<{
  modelValue?: ProcessingSpec[]
  produces?: ProcessingSpec[]
}>()

const emit = defineEmits<{
  'update:modelValue': [value: ProcessingSpec[]]
}>()

const specs = computed(() => props.modelValue || [])
const inputOptions = computed(() => {
  const names = new Set<string>()
  for (const item of props.produces || []) {
    for (const key of ['alias', 'name']) {
      const value = String(item?.[key] || '').trim().toUpperCase()
      if (value) names.add(value)
    }
  }
  return [...names].sort().map((name) => `{{${name}}}`)
})

function inputOptionsFor(index: number): string[] {
  const names = new Set(inputOptions.value)
  // 处理数组顺序就是执行顺序；仅向后暴露已经派生的局部变量。
  for (const item of specs.value.slice(0, index)) {
    if (item?.mode !== 'derive') continue
    const target = String(item?.target_variable || '').trim().toUpperCase()
    if (target) names.add(`{{${target}}}`)
  }
  return [...names].sort()
}

const featureOptions = [
  { label: '虚拟机名称', value: 'vm_name' },
  { label: '主机名称', value: 'host' },
  { label: '磁盘名称', value: 'disk_name' },
  { label: '网口名称', value: 'interface_name' },
  { label: '错误码', value: 'error_code' },
  { label: '源主机', value: 'source_host' },
  { label: '目标主机', value: 'destination_host' },
  { label: '百分比', value: 'percent.current' },
  { label: '数字', value: 'number' },
  { label: '源主机 → 目标主机', value: 'change_pair' },
]

const valueTypeOptions = [
  { label: '文本', value: 'string' },
  { label: '整数', value: 'integer' },
  { label: '数值', value: 'number' },
  { label: '百分比', value: 'percentage' },
]

function update(index: number, patch: ProcessingSpec): void {
  emit('update:modelValue', specs.value.map((item, itemIndex) => itemIndex === index ? { ...item, ...patch } : item))
}

function add(): void {
  emit('update:modelValue', [...specs.value, {
    id: `processing_${specs.value.length + 1}`,
    mode: 'derive',
    input: inputOptions.value[0] || '',
    operation: 'feature_extract',
    feature: 'vm_name',
    target_variable: '',
    value_type: 'string',
    cardinality: 'exactly_one',
    fallback: 'none',
  }])
}

function remove(index: number): void {
  emit('update:modelValue', specs.value.filter((_, itemIndex) => itemIndex !== index))
}

function setMode(index: number, mode: string): void {
  if (mode === 'assert') {
    update(index, { mode, operation: 'compare', target_variable: undefined, value_type: 'percentage', operator: '>', right: '90%' })
  } else {
    update(index, { mode, operation: 'feature_extract', value_type: 'string', cardinality: 'exactly_one', fallback: 'none' })
  }
}
</script>

<template>
  <div class="qkv-output-processing-editor">
    <div class="processing-editor-header">
      <div>
        <strong>输出后处理</strong>
        <span>采集并投影产出变量后，在同一 Signal 内继续提取或判断</span>
      </div>
      <el-button text type="primary" size="small" :icon="Plus" @click="add">添加处理</el-button>
    </div>

    <el-alert
      v-if="specs.length === 0"
      type="info"
      :closable="false"
      title="可选：例如从 DESCRIPTION 提取 VM_NAME，或判断使用率是否大于 90%。"
    />

    <div v-for="(item, index) in specs" :key="item.id || index" class="processing-card">
      <div class="processing-card-title">
        <span>处理 {{ index + 1 }}</span>
        <el-button text type="danger" size="small" :icon="Delete" @click="remove(index)">删除</el-button>
      </div>
      <div class="processing-grid">
        <label>处理 ID<el-input :model-value="item.id" size="small" @input="(v: string) => update(index, { id: v })" /></label>
        <label>处理方式
          <el-select :model-value="item.mode || 'derive'" size="small" @change="(v: string) => setMode(index, v)">
            <el-option label="派生变量" value="derive" />
            <el-option label="断言判断" value="assert" />
          </el-select>
        </label>
        <label class="wide">输入变量
          <el-select :model-value="item.input" filterable default-first-option size="small" placeholder="选择当前信号已产出变量" @change="(v: string) => update(index, { input: v })">
            <el-option v-for="value in inputOptionsFor(index)" :key="value" :label="value" :value="value" />
          </el-select>
        </label>
        <label>操作
          <el-select :model-value="item.operation" size="small" @change="(v: string) => update(index, { operation: v })">
            <el-option label="特征提取" value="feature_extract" />
            <el-option label="JSON 路径" value="json_path" />
            <el-option label="去首尾空白" value="trim" />
            <el-option label="转小写" value="lower" />
            <el-option label="转大写" value="upper" />
            <el-option label="分割" value="split" />
            <el-option v-if="item.mode === 'assert'" label="比较" value="compare" />
          </el-select>
        </label>
        <label v-if="item.operation === 'feature_extract'" class="wide">提取特征
          <el-select :model-value="item.feature" size="small" @change="(v: string) => update(index, { feature: v })">
            <el-option v-for="option in featureOptions" :key="option.value" :label="option.label" :value="option.value" />
          </el-select>
        </label>
        <label v-if="item.operation === 'json_path'">JSON 路径<el-input :model-value="item.path" placeholder="data.items.0.name" size="small" @input="(v: string) => update(index, { path: v })" /></label>
        <label v-if="item.operation === 'split'">分隔符<el-input :model-value="item.separator" placeholder="," size="small" @input="(v: string) => update(index, { separator: v })" /></label>
        <label v-if="item.mode === 'assert'">比较符
          <el-select :model-value="item.operator || '>'" size="small" @change="(v: string) => update(index, { operator: v })">
            <el-option v-for="operator in ['>', '>=', '<', '<=', '==', '!=']" :key="operator" :label="operator" :value="operator" />
          </el-select>
        </label>
        <label>值类型
          <el-select :model-value="item.value_type || 'string'" size="small" @change="(v: string) => update(index, { value_type: v })">
            <el-option v-for="option in valueTypeOptions" :key="option.value" :label="option.label" :value="option.value" />
          </el-select>
        </label>
        <label v-if="item.mode === 'assert'">比较值<el-input :model-value="item.right" placeholder="90%" size="small" @input="(v: string) => update(index, { right: v })" /></label>
        <label v-if="item.mode === 'derive'">目标变量<el-input :model-value="item.target_variable" placeholder="VM_NAME" size="small" @input="(v: string) => update(index, { target_variable: v.toUpperCase() })" /></label>
        <label v-if="item.mode === 'derive'">基数
          <el-select :model-value="item.cardinality || 'exactly_one'" size="small" @change="(v: string) => update(index, { cardinality: v })">
            <el-option label="恰好一个" value="exactly_one" />
            <el-option label="零个或多个" value="zero_or_more" />
          </el-select>
        </label>
        <label v-if="item.mode === 'derive'">兜底
          <el-select :model-value="item.fallback || 'none'" size="small" @change="(v: string) => update(index, { fallback: v })">
            <el-option label="无（严格失败）" value="none" />
            <el-option label="AI 提取" value="ai_extract" />
          </el-select>
        </label>
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
.processing-grid .wide { grid-column: span 2; }
@media (max-width: 900px) { .processing-grid { grid-template-columns: 1fr; } .processing-grid .wide { grid-column: auto; } }
</style>
