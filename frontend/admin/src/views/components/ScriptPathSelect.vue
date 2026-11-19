<script setup lang="ts">
/**
 * 「动作=脚本」时的路径选择器：下拉选平台脚本，最后一项切自由输入。
 *
 * 为什么要抽组件：默认场景清单与自定义场景编辑器都要用同一套交互，
 * 两份实现必然在"自动输入""已停用""值不在库里"这些边界上漂移。
 *
 * 语义：
 * - 选项来自「收集脚本」库，值是落盘文件名（即 spc 的 path）；
 * - 库里的脚本被停用时仍展示但置灰，管理员能看见"为什么不能选"；
 * - 「自动输入」进入手填模式，用于引用节点自带脚本（如 vs_collect_upgrade_info.py）；
 * - 当前值不在库中（历史数据/自由输入）时自动进入手填模式，不把值吞掉。
 */
import { onMounted, ref, watch } from 'vue'

import { listScripts, type LogCollectScriptSummary } from '@/api/logCollectScript'

const props = defineProps<{ modelValue: string; interpreter?: string }>()
const emit = defineEmits<{
  (event: 'update:modelValue', value: string): void
  (event: 'update:interpreter', value: string): void
}>()

const CUSTOM_VALUE = '__custom__'

const options = ref<LogCollectScriptSummary[]>([])
const loading = ref(false)
const loaded = ref(false)
const customMode = ref(false)

function optionLabel(row: LogCollectScriptSummary): string {
  const version = row.customized ? ` · v${row.revision}` : ''
  const disabled = row.enabled ? '' : ' · 已停用'
  return `${row.code}${version}${disabled}`
}

async function load() {
  loading.value = true
  try {
    const data = await listScripts()
    options.value = data.scripts.filter((row) => row.ref_usable)
    loaded.value = true
    if (props.modelValue && !options.value.some((row) => row.code === props.modelValue)) {
      customMode.value = true
    }
  } catch {
    // 列表拉取失败不阻断编辑：保留当前值，管理员仍可手填后保存。
  } finally {
    loading.value = false
  }
}

function onVisible(visible: boolean) {
  if (visible) void load()
}

function onChange(value: string) {
  if (value === CUSTOM_VALUE) {
    customMode.value = true
    emit('update:modelValue', '')
    return
  }
  customMode.value = false
  emit('update:modelValue', value)
  // 解释器必须跟着脚本走：选了 .sh 却留 python，acli 会用错解释器执行并失败。
  const picked = options.value.find((row) => row.code === value)
  if (picked) emit('update:interpreter', picked.interpreter)
}

function backToList() {
  customMode.value = false
  emit('update:modelValue', '')
}

watch(
  () => props.modelValue,
  (value) => {
    if (!loaded.value || !value) return
    if (options.value.some((row) => row.code === value)) customMode.value = false
  },
)

onMounted(load)
</script>

<template>
  <div class="script-path-select">
    <el-select
      v-if="!customMode"
      :model-value="modelValue"
      size="small"
      filterable
      clearable
      :loading="loading"
      placeholder="选择平台脚本，或选「自动输入」"
      @visible-change="onVisible"
      @change="onChange"
    >
      <el-option
        v-for="row in options"
        :key="row.code"
        :label="optionLabel(row)"
        :value="row.code"
        :disabled="!row.enabled"
      />
      <el-option label="自动输入（手填节点上已有的文件名）" :value="CUSTOM_VALUE" />
    </el-select>
    <div v-else class="sps-custom">
      <el-input
        :model-value="modelValue"
        size="small"
        placeholder="手填节点 script 目录下已有的脚本文件名"
        @update:model-value="emit('update:modelValue', $event)"
      />
      <el-button size="small" text @click="backToList">从列表选择</el-button>
    </div>
  </div>
</template>

<style scoped>
.script-path-select {
  width: 100%;
}
.sps-custom {
  display: flex;
  gap: 6px;
  align-items: center;
}
</style>
