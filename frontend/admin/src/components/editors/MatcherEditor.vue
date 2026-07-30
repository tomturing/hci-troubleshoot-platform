<script setup lang="ts">
/**
 * MatcherEditor - 判定器可视化编辑器
 *
 * 用于 QFK 工具的 matcher 字段编辑。
 * 支持 8 种判定类型：
 *   - keyword: 关键字匹配（pattern, mode, expected）
 *   - regex: 正则表达式（pattern, expected）
 *   - state: 状态值匹配（pattern, expected）
 *   - threshold: 数值阈值（operator, value, expected）
 *   - delta: 多样本首末差值（metric, operator, value, minimum_samples）
 *   - trend: 多样本趋势（metric, direction, value, minimum_samples）
 *   - json_path: JSON 路径取值（path, expected_value, expected）
 *   - exists: 存在性判定（expected）
 *
 * 使用方式（v-model 双向绑定对象）：
 *   <MatcherEditor v-model="matcherData" />
 */

import { computed, watch } from 'vue'
import { InfoFilled } from '@element-plus/icons-vue'

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
    // 切换类型时重置参数
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
      newMatcher.metric = ''
      newMatcher.operator = '>'
      newMatcher.value = 0
      newMatcher.minimum_samples = 2
    } else if (type === 'trend') {
      newMatcher.metric = ''
      newMatcher.direction = 'increasing'
      newMatcher.value = 0
      newMatcher.minimum_samples = 3
    } else if (type === 'json_path') {
      newMatcher.path = ''
      newMatcher.expected_value = null
    }
    matcher.value = newMatcher
  },
})

// keyword 模式的 pattern（支持数组，用逗号分隔输入）
const keywordPatternsStr = computed({
  get: () => {
    const p = matcher.value.pattern
    if (Array.isArray(p)) return p.join(', ')
    return p || ''
  },
  set: (val: string) => {
    if (val.includes(',')) {
      matcher.value = {
        ...matcher.value,
        pattern: val.split(',').map((s: string) => s.trim()).filter(Boolean),
      }
    } else {
      matcher.value = { ...matcher.value, pattern: val.trim() }
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
  { label: '关键字匹配', value: 'keyword', desc: '在输出中搜索关键字' },
  { label: '正则表达式', value: 'regex', desc: '用正则匹配输出' },
  { label: '状态判定', value: 'state', desc: '匹配特定状态值' },
  { label: '数值阈值', value: 'threshold', desc: '数值比较判定' },
  { label: '首末差值', value: 'delta', desc: '周期日志计数器差值' },
  { label: '变化趋势', value: 'trend', desc: '周期日志连续趋势' },
  { label: 'JSON 路径', value: 'json_path', desc: '从 JSON 中提取值比较' },
  { label: '存在性判定', value: 'exists', desc: '检查输出是否非空' },
]
const matcherTypeOptions = computed(() => {
  if (!props.allowedTypes?.length) return allMatcherTypeOptions
  return allMatcherTypeOptions.filter((item) => props.allowedTypes?.includes(item.value))
})
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
              <br/><b>keyword</b> — 关键字匹配，支持多关键字（or/and/not 模式）。
              <br/><b>regex</b> — 正则表达式匹配。
              <br/><b>state</b> — 匹配特定状态值（如 running、stopped）。
              <br/><b>threshold</b> — 数值阈值比较（支持 &gt; &gt;= &lt; &lt;= == !=）。
              <br/><b>delta</b> — 按 metric 提取多次采样，比较末值减首值。
              <br/><b>trend</b> — 按 metric 判断连续上升、下降或稳定。
              <br/><b>json_path</b> — 从 JSON 输出中提取字段值比较。
              <br/><b>exists</b> — 检查输出是否非空。
              <br/><b>expected</b> — 期望结果：true=期望匹配（异常判定），false=期望不匹配（健康判定）。
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

      <!-- keyword 类型参数 -->
      <template v-if="matcherType === 'keyword'">
        <el-form-item label="关键字">
          <el-input
            v-model="keywordPatternsStr"
            placeholder="输入关键字，多个用逗号分隔"
            spellcheck="false"
          />
          <div class="field-hint">多个关键字时，下方匹配模式决定组合逻辑</div>
        </el-form-item>
        <el-form-item label="匹配模式">
          <el-radio-group v-model="matcher.mode">
            <el-radio-button value="or">任一匹配 (OR)</el-radio-button>
            <el-radio-button value="and">全部匹配 (AND)</el-radio-button>
            <el-radio-button value="not">均不匹配 (NOT)</el-radio-button>
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
        <el-form-item label="指标字段">
          <el-input v-model="matcher.metric" placeholder="日志可填，如 rx_missed_errors；普通命令可留空" />
        </el-form-item>
        <el-form-item label="聚合方式">
          <el-select v-model="matcher.aggregation" style="width: 100%;">
            <el-option label="首个数值" value="first_number" />
            <el-option label="最后数值" value="last_number" />
            <el-option label="最大值" value="max" />
            <el-option label="最小值" value="min" />
            <el-option label="求和" value="sum" />
            <el-option label="非空行数" value="line_count" />
            <el-option label="命令耗时秒" value="duration_seconds" />
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
        <el-form-item label="指标字段"><el-input v-model="matcher.metric" placeholder="如 rx_missed_errors" /></el-form-item>
        <el-form-item label="运算符">
          <el-select v-model="matcher.operator" style="width: 100%;">
            <el-option v-for="opt in operatorOptions" :key="opt.value" :label="opt.label" :value="opt.value" />
          </el-select>
        </el-form-item>
        <el-form-item label="差值阈值"><el-input-number v-model="matcher.value" :precision="2" style="width: 100%;" /></el-form-item>
        <el-form-item label="最少样本"><el-input-number v-model="matcher.minimum_samples" :min="2" :max="10000" style="width: 100%;" /></el-form-item>
      </template>

      <template v-else-if="matcherType === 'trend'">
        <el-form-item label="指标字段"><el-input v-model="matcher.metric" placeholder="如 rx_missed_errors" /></el-form-item>
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

      <!-- json_path 类型参数 -->
      <template v-else-if="matcherType === 'json_path'">
        <el-form-item label="JSON 路径">
          <el-input
            v-model="matcher.path"
            placeholder="如：data.status 或 result[0].code"
            spellcheck="false"
          />
          <div class="field-hint">使用点号分隔路径，数组用 [index] 访问</div>
        </el-form-item>
        <el-form-item label="期望值">
          <el-input
            v-model="matcher.expected_value"
            placeholder="期望提取到的值"
            spellcheck="false"
          />
        </el-form-item>
      </template>

      <!-- exists 类型：无额外参数 -->
      <template v-else-if="matcherType === 'exists'">
        <el-form-item label="说明">
          <el-tag type="info" effect="plain">检查输出是否非空</el-tag>
        </el-form-item>
      </template>

      <!-- 公共：期望结果 -->
      <el-form-item label="期望结果">
        <el-switch
          v-model="matcher.expected"
          active-text="符合期望"
          inactive-text="不符合期望"
          active-color="#f56c6c"
          inactive-color="#67c23a"
        />
        <div class="field-hint">
          {{ matcher.expected ? '期望匹配成功 → 异常判定' : '期望匹配失败 → 健康判定' }}
        </div>
      </el-form-item>
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
