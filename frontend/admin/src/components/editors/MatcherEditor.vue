<script setup lang="ts">
/**
 * MatcherEditor - 匹配模式可视化编辑器
 *
 * 用于 QFK 工具的 matcher 字段编辑。
 * 支持 7 种判定类型：
 *   - keyword: 关键字匹配（pattern, mode）
 *   - regex: 正则表达式（pattern）
 *   - state: 状态值匹配（pattern）
 *   - threshold: 数值阈值（operator, value）
 *   - delta: 多样本首末差值（operator, value, minimum_samples）
 *   - trend: 多样本趋势（direction, value, minimum_samples）
 *   - exists: 存在性判定
 *
 * 使用方式（v-model 双向绑定对象）：
 *   <MatcherEditor v-model="matcherData" />
 */

import { computed, watch } from 'vue'
import { InfoFilled } from '@element-plus/icons-vue'
import ValueExtractEditor from './ValueExtractEditor.vue'
import { formatKeywordInput, parseKeywordInput } from '../../utils/keywordInput'

const props = defineProps<{
  modelValue: Record<string, any>
  allowedTypes?: string[]
}>()

const emit = defineEmits<{
  'update:modelValue': [value: Record<string, any>]
}>()

const matcher = computed({
  get: () => props.modelValue || { type: 'keyword', expected: true },
  set: (val) => emit('update:modelValue', val),
})

const matcherType = computed({
  get: () => matcher.value.type || 'keyword',
  set: (type: string) => {
    // Predicate 切换只重置判定字段；已经确认的 Extract 必须保留。
    const existingExtract = matcher.value.extract
    const newMatcher: Record<string, any> = { type, expected: true }
    if (type === 'keyword') {
      newMatcher.pattern = ''
      newMatcher.mode = 'or'
    } else if (type === 'regex' || type === 'state') {
      newMatcher.pattern = ''
    } else if (type === 'threshold') {
      newMatcher.operator = '>'
      newMatcher.value = 0
      newMatcher.aggregation = 'first_number'
    } else if (type === 'delta') {
      newMatcher.operator = '>'
      newMatcher.value = 0
      newMatcher.minimum_samples = 2
    } else if (type === 'trend') {
      newMatcher.direction = 'increasing'
      newMatcher.value = 0
      newMatcher.minimum_samples = 3
    }
    const numeric = ['threshold', 'delta', 'trend'].includes(type)
    newMatcher.extract = existingExtract || {
      type: 'text', rows: { mode: 'all' }, cardinality: 'all', source: 'stdout',
      value_mode: numeric ? 'number' : 'string',
    }
    matcher.value = newMatcher
  },
})

// keyword 模式的 pattern（支持数组，每行一个字面量输入）
const keywordPatternsStr = computed({
  get: () => {
    const p = matcher.value.pattern
    if (Array.isArray(p)) return formatKeywordInput(p)
    return p || ''
  },
  set: (val: string) => {
    const patterns = parseKeywordInput(val)
    matcher.value = {
      ...matcher.value,
      pattern: patterns.length > 1 ? patterns : patterns[0] || '',
    }
  },
})

// 阈值运算符选项
const operatorOptions = [
  { label: '大于 (>)', value: '>' },
  { label: '大于等于 (>=)', value: '>=' },
  { label: '小于 (<)', value: '<' },
  { label: '小于等于 (<=)', value: '<=' },
  { label: '等于 (==)', value: '==' },
  { label: '不等于 (!=)', value: '!=' },
]

const allMatcherTypeOptions = [
  { label: '关键字匹配（搜索文字）', value: 'keyword', desc: '在输出中搜索关键字' },
  { label: '正则表达式（模式匹配）', value: 'regex', desc: '用正则匹配输出' },
  { label: '状态判定（匹配状态值）', value: 'state', desc: '匹配特定状态值' },
  { label: '数值阈值（比较数字）', value: 'threshold', desc: '数值比较判定' },
  { label: '首末差值（比较变化量）', value: 'delta', desc: '周期日志计数器差值' },
  { label: '变化趋势（连续变化）', value: 'trend', desc: '周期日志连续趋势' },
  { label: '存在性判定（是否有输出）', value: 'exists', desc: '检查输出是否非空' },
]
const matcherTypeOptions = computed(() => {
  if (!props.allowedTypes?.length) return allMatcherTypeOptions
  return allMatcherTypeOptions.filter((item) => props.allowedTypes?.includes(item.value))
})
const extractDefaultValueMode = computed(() => (
  ['threshold', 'delta', 'trend'].includes(matcherType.value) ? 'number' : 'string'
))
</script>

<template>
  <div class="matcher-editor">
    <div class="section-header">
      <div class="section-title">
        <el-icon class="title-icon"><InfoFilled /></el-icon>
        <span>判定器 (matcher)</span>
        <el-tooltip placement="top" :show-after="300">
          <template #content>
            <div style="max-width: 400px; line-height: 1.6;">
              定义如何判定执行结果是否满足预期。
              <br/><b>keyword</b> — 关键字匹配，支持多关键字 AND/OR。
              <br/><b>regex</b> — 正则表达式匹配。
              <br/><b>state</b> — 匹配特定状态值（如 running、stopped）。
              <br/><b>threshold</b> — 数值阈值比较（支持 &gt; &gt;= &lt; &lt;= == !=）。
              <br/><b>delta</b> — 比较多个样本的末值减首值。
              <br/><b>trend</b> — 判断多个样本连续上升、下降或稳定。
              <br/><b>exists</b> — 检查输出是否非空。
            </div>
          </template>
          <el-icon class="help-icon"><InfoFilled /></el-icon>
        </el-tooltip>
      </div>
    </div>

    <el-form label-position="left" label-width="90px" class="matcher-form">
      <!-- 判定类型选择 -->
      <el-form-item label="判定类型">
        <el-select v-model="matcherType" style="width: 100%;">
          <el-option
            v-for="opt in matcherTypeOptions"
            :key="opt.value"
            :label="opt.label"
            :value="opt.value"
          >
            <div class="matcher-type-option">
              <span class="type-label">{{ opt.label }}</span>
              <span class="type-desc">{{ opt.desc }}</span>
            </div>
          </el-option>
        </el-select>
      </el-form-item>

      <ValueExtractEditor v-model="matcher.extract" :default-value-mode="extractDefaultValueMode" />
      <div class="predicate-order-title">二、判定配置</div>

      <!-- keyword 类型参数 -->
      <template v-if="matcherType === 'keyword'">
        <el-form-item label="关键字">
          <el-input
            v-model="keywordPatternsStr"
            type="textarea"
            :rows="3"
            placeholder="每行一个关键字"
            spellcheck="false"
          />
          <div class="field-hint">每行一个字面量；中英文逗号属于关键字内容。多个关键字时，下方组合关系决定匹配逻辑</div>
        </el-form-item>
        <el-form-item label="组合关系">
          <el-radio-group v-model="matcher.mode">
            <el-radio-button value="or">任一匹配（OR）</el-radio-button>
            <el-radio-button value="and">全部匹配（AND）</el-radio-button>
          </el-radio-group>
        </el-form-item>
      </template>

      <!-- regex 类型参数 -->
      <template v-else-if="matcherType === 'regex'">
        <el-form-item label="正则表达式">
          <el-input
            v-model="matcher.pattern"
            placeholder="如：error|failed|timeout"
            spellcheck="false"
          />
          <div class="field-hint">日志采集还会下推到 aCLI -E，请使用 Python re 与扩展正则都兼容的表达式</div>
        </el-form-item>
      </template>

      <!-- state 类型参数 -->
      <template v-else-if="matcherType === 'state'">
        <el-form-item label="期望状态">
          <el-input
            v-model="matcher.pattern"
            placeholder="如：running、stopped、active"
            spellcheck="false"
          />
        </el-form-item>
      </template>

      <!-- threshold 类型参数 -->
      <template v-else-if="matcherType === 'threshold'">
        <el-form-item label="聚合方式">
          <el-select v-model="matcher.aggregation" style="width: 100%;">
            <el-option label="首个取值" value="first_number" />
            <el-option label="最后数值（最后一个样本）" value="last_number" />
            <el-option label="最大值（所有样本）" value="max" />
            <el-option label="最小值（所有样本）" value="min" />
            <el-option label="求和（所有样本相加）" value="sum" />
            <el-option label="非空行数（统计行数）" value="line_count" />
            <el-option label="命令耗时秒（解析 real）" value="duration_seconds" />
          </el-select>
        </el-form-item>
        <el-form-item label="运算符">
          <el-select v-model="matcher.operator" style="width: 100%;">
            <el-option
              v-for="opt in operatorOptions"
              :key="opt.value"
              :label="opt.label"
              :value="opt.value"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="阈值">
          <el-input-number
            v-model="matcher.value"
            :precision="2"
            :step="1"
            style="width: 100%;"
            placeholder="输入数值阈值"
          />
        </el-form-item>
      </template>

      <template v-else-if="matcherType === 'delta'">
        <el-form-item label="运算符">
          <el-select v-model="matcher.operator" style="width: 100%;">
            <el-option v-for="opt in operatorOptions" :key="opt.value" :label="opt.label" :value="opt.value" />
          </el-select>
        </el-form-item>
        <el-form-item label="差值阈值"><el-input-number v-model="matcher.value" :precision="2" style="width: 100%;" /></el-form-item>
        <el-form-item label="最少样本"><el-input-number v-model="matcher.minimum_samples" :min="2" :max="10000" style="width: 100%;" /></el-form-item>
      </template>

      <template v-else-if="matcherType === 'trend'">
        <el-form-item label="趋势方向">
          <el-select v-model="matcher.direction" style="width: 100%;">
            <el-option label="连续上升" value="increasing" />
            <el-option label="连续下降" value="decreasing" />
            <el-option label="保持稳定" value="stable" />
          </el-select>
        </el-form-item>
        <el-form-item label="最小步长"><el-input-number v-model="matcher.value" :precision="2" :min="0" style="width: 100%;" /></el-form-item>
        <el-form-item label="最少样本"><el-input-number v-model="matcher.minimum_samples" :min="3" :max="10000" style="width: 100%;" /></el-form-item>
      </template>

      <!-- exists 类型：无额外参数 -->
      <template v-else-if="matcherType === 'exists'">
        <el-form-item label="说明">
          <el-tag type="info" effect="plain">检查输出是否非空</el-tag>
        </el-form-item>
      </template>

      <el-alert
        v-if="matcher.expected === false || matcher.mode === 'not'"
        type="warning"
        :closable="false"
        title="这是历史取反配置，平台继续兼容执行；新配置请改用明确的正向判定条件。"
      />
    </el-form>
  </div>
</template>

<style scoped>
.matcher-editor {
  border: 1px solid #e4e7ed;
  border-radius: 6px;
  padding: 16px;
  background: #fafbfc;
  margin-top: 16px;
}

.section-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
}

.section-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 14px;
  font-weight: 600;
  color: #303133;
}

.title-icon {
  color: #e6a23c;
}

.help-icon {
  color: #909399;
  cursor: help;
  font-size: 14px;
}

.matcher-form {
  margin-top: 8px;
}

.matcher-type-option {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.type-label {
  font-weight: 500;
}

.type-desc {
  font-size: 12px;
  color: #909399;
}

.field-hint {
  font-size: 12px;
  color: #909399;
  margin-top: 4px;
}
</style>
