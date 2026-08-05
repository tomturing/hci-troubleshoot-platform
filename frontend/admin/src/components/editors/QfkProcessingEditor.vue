<script setup lang="ts">
/**
 * QFK 执行结果处理编辑器。
 *
 * 匹配模式和产出变量统一使用“第一步：取值 → 第二步：判断/产出”的处理单元。
 * 每个产出变量拥有独立的取值配置，避免多个变量错误共享同一个提取结果。
 */
import { computed } from 'vue'
import { Delete, InfoFilled, Plus } from '@element-plus/icons-vue'

import MatcherEditor from './MatcherEditor.vue'
import ValueExtractEditor from './ValueExtractEditor.vue'

export type QfkOutputMode = 'keyword' | 'produces'

const props = defineProps<{
  mode: QfkOutputMode
  match?: Record<string, any> | null
  produces?: Array<Record<string, any>>
  allowedMatcherTypes?: string[]
}>()

const emit = defineEmits<{
  'update:mode': [value: QfkOutputMode]
  'update:match': [value: Record<string, any>]
  'update:produces': [value: Array<Record<string, any>>]
}>()

const matchValue = computed(() => props.match || defaultMatch())
const producesValue = computed(() => props.produces || [])

function defaultExtract(valueMode = 'string'): Record<string, any> {
  return {
    type: 'text',
    rows: { mode: 'all' },
    cardinality: 'exactly_one',
    source: 'stdout',
    value_mode: valueMode,
  }
}

function defaultMatch(): Record<string, any> {
  return {
    type: 'keyword',
    pattern: '',
    mode: 'or',
    expected: true,
    extract: { ...defaultExtract(), cardinality: 'all' },
  }
}

function defaultProduce(): Record<string, any> {
  return { name: '', type: 'string', extract: defaultExtract() }
}

function setMode(value: QfkOutputMode): void {
  emit('update:mode', value)
}

function setMatch(value: Record<string, any>): void {
  emit('update:match', value)
}

function setMatchExtract(value: Record<string, any>): void {
  emit('update:match', { ...matchValue.value, extract: value })
}

function updateProduce(index: number, patch: Record<string, any>): void {
  emit('update:produces', producesValue.value.map((item, itemIndex) => (
    itemIndex === index ? { ...item, ...patch } : item
  )))
}

function addProduce(): void {
  emit('update:produces', [...producesValue.value, defaultProduce()])
}

function removeProduce(index: number): void {
  emit('update:produces', producesValue.value.filter((_, itemIndex) => itemIndex !== index))
}
</script>

<template>
  <div class="qfk-processing-editor">
    <div class="processing-header">
      <div>
        <div class="processing-title">
          <el-icon><InfoFilled /></el-icon>
          <span>执行结果处理</span>
        </div>
        <div class="processing-subtitle">两种模式使用相同的取值组件、步骤顺序、字段样式和状态反馈。</div>
      </div>
      <el-radio-group :model-value="mode" size="small" @change="setMode">
        <el-radio-button value="keyword">匹配模式</el-radio-button>
        <el-radio-button value="produces">产出变量</el-radio-button>
      </el-radio-group>
    </div>

    <div class="processing-flow" aria-label="执行结果两步处理流程">
      <div class="flow-stage">
        <span class="stage-number">1</span>
        <span><strong>先取值</strong><small>从 stdout / stderr 取得明确数据</small></span>
      </div>
      <span class="flow-arrow" aria-hidden="true">→</span>
      <div class="flow-stage">
        <span class="stage-number">2</span>
        <span>
          <strong>{{ mode === 'keyword' ? '再判断' : '再产出' }}</strong>
          <small>{{ mode === 'keyword' ? '输出 True / False' : '校验后写入变量池' }}</small>
        </span>
      </div>
    </div>

    <el-alert
      class="execution-gate"
      type="warning"
      :closable="false"
      show-icon
      title="命令执行失败或超时时立即停止：不取值、不判断，也不写入变量池。"
    />

    <div v-if="mode === 'keyword'" class="processing-unit" data-output-mode="keyword">
      <div class="unit-header"><span>处理单元</span><el-tag size="small" effect="plain">匹配模式</el-tag></div>

      <section class="processing-step">
        <div class="step-header"><span class="stage-number">1</span><div><strong>第一步：取值</strong><small>取得供第二步判断使用的数据</small></div></div>
        <ValueExtractEditor
          :model-value="matchValue.extract"
          :default-value-mode="['threshold', 'delta', 'trend'].includes(matchValue.type) ? 'number' : 'string'"
          embedded
          :show-title="false"
          @update:model-value="setMatchExtract"
        />
        <div class="step-output"><span>第一步输出</span><code>交给第二步判断</code></div>
      </section>

      <div class="step-connector" aria-hidden="true">↓</div>

      <section class="processing-step">
        <div class="step-header"><span class="stage-number">2</span><div><strong>第二步：判断</strong><small>只对第一步取得的值执行确定性规则</small></div></div>
        <MatcherEditor
          :model-value="matchValue"
          :allowed-types="allowedMatcherTypes"
          embedded
          :show-extract="false"
          :show-header="false"
          :show-step-title="false"
          @update:model-value="setMatch"
        />
        <div class="step-output final"><span>最终输出</span><code>True / False</code></div>
      </section>
    </div>

    <template v-else>
      <div
        v-for="(produce, index) in producesValue"
        :key="`${produce.name || 'new'}-${index}`"
        class="processing-unit"
        data-output-mode="produces"
      >
        <div class="unit-header">
          <span>处理单元 {{ index + 1 }}</span>
          <el-button text type="danger" size="small" :icon="Delete" @click="removeProduce(index)">删除</el-button>
        </div>

        <section class="processing-step">
          <div class="step-header"><span class="stage-number">1</span><div><strong>第一步：取值</strong><small>取得供第二步产出使用的数据</small></div></div>
          <ValueExtractEditor
            :model-value="produce.extract"
            :default-value-mode="produce.type || 'string'"
            embedded
            :show-title="false"
            @update:model-value="(value: Record<string, any>) => updateProduce(index, { extract: value })"
          />
          <div class="step-output"><span>第一步输出</span><code>交给第二步产出</code></div>
        </section>

        <div class="step-connector" aria-hidden="true">↓</div>

        <section class="processing-step">
          <div class="step-header"><span class="stage-number">2</span><div><strong>第二步：产出</strong><small>校验结果数量和变量类型后写入变量池</small></div></div>
          <el-form label-position="left" label-width="96px" size="small">
            <el-form-item label="变量名">
              <el-input :model-value="produce.name" placeholder="如 KVM_PID（必填）" @input="(value: string) => updateProduce(index, { name: value })" />
            </el-form-item>
            <el-form-item label="变量类型">
              <el-select :model-value="produce.type || 'string'" @change="(value: string) => updateProduce(index, { type: value })">
                <el-option label="字符串" value="string" />
                <el-option label="整数" value="integer" />
                <el-option label="数字" value="number" />
                <el-option label="布尔值" value="boolean" />
                <el-option label="数组" value="array" />
                <el-option label="对象" value="object" />
                <el-option label="对象数组" value="array<object>" />
              </el-select>
            </el-form-item>
            <el-form-item label="变量值">
              <div class="readonly-source">使用第一步取值结果</div>
            </el-form-item>
          </el-form>
          <div class="step-output final"><span>最终输出</span><code>{{ produce.name || '变量名' }} → 写入变量池</code></div>
        </section>
      </div>

      <el-empty v-if="producesValue.length === 0" :image-size="48" description="暂无处理单元，请添加变量" />
      <el-button class="add-processing-unit" plain type="primary" :icon="Plus" @click="addProduce">
        添加变量（创建新的“取值 → 产出”处理单元）
      </el-button>
    </template>
  </div>
</template>

<style scoped>
.qfk-processing-editor {
  width: 100%;
  margin-top: 12px;
  padding: 14px;
  border: 1px solid var(--el-border-color);
  border-radius: 6px;
  background: var(--el-fill-color-extra-light);
}
.processing-header,
.unit-header,
.processing-flow,
.flow-stage,
.step-header,
.step-output {
  display: flex;
  align-items: center;
}
.processing-header,
.unit-header {
  justify-content: space-between;
  gap: 12px;
}
.processing-title {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--el-text-color-primary);
  font-weight: 600;
}
.processing-title .el-icon { color: var(--el-color-primary); }
.processing-subtitle,
.step-header small,
.flow-stage small {
  display: block;
  margin-top: 3px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  font-weight: 400;
}
.processing-flow {
  gap: 12px;
  margin: 14px 0 10px;
}
.flow-stage {
  flex: 1;
  gap: 9px;
  min-width: 0;
  padding: 10px 12px;
  border: 1px solid var(--el-color-primary-light-5);
  border-radius: 6px;
  background: var(--el-color-primary-light-9);
}
.flow-stage > span:last-child { min-width: 0; }
.flow-arrow,
.step-connector { color: var(--el-color-primary); font-weight: 600; }
.execution-gate { margin-bottom: 12px; }
.processing-unit {
  padding: 12px;
  border: 1px solid var(--el-border-color);
  border-radius: 6px;
  background: var(--el-bg-color);
}
.processing-unit + .processing-unit { margin-top: 12px; }
.unit-header {
  min-height: 28px;
  margin-bottom: 10px;
  color: var(--el-text-color-primary);
  font-weight: 600;
}
.processing-step {
  padding: 12px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  background: var(--el-bg-color);
}
.step-header {
  gap: 9px;
  margin-bottom: 12px;
}
.step-header strong { color: var(--el-text-color-primary); font-weight: 600; }
.stage-number {
  display: inline-flex;
  flex: 0 0 26px;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  border-radius: 50%;
  color: var(--el-color-white);
  background: var(--el-color-primary);
  font-size: 13px;
  font-weight: 600;
}
.step-connector {
  height: 26px;
  text-align: center;
  line-height: 26px;
}
.step-output {
  gap: 10px;
  margin-top: 10px;
  padding: 8px 10px;
  border-radius: 4px;
  color: var(--el-text-color-regular);
  background: var(--el-fill-color-light);
  font-size: 12px;
}
.step-output.final {
  border: 1px solid var(--el-color-success-light-5);
  background: var(--el-color-success-light-9);
}
.step-output code { word-break: break-all; }
.readonly-source {
  width: 100%;
  min-height: 28px;
  padding: 0 10px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 4px;
  color: var(--el-text-color-secondary);
  background: var(--el-fill-color-light);
  line-height: 26px;
}
.add-processing-unit { width: 100%; margin-top: 12px; }

@media (max-width: 720px) {
  .processing-header { align-items: flex-start; flex-direction: column; }
  .processing-flow { align-items: stretch; flex-direction: column; }
  .flow-arrow { transform: rotate(90deg); text-align: center; }
}
</style>
