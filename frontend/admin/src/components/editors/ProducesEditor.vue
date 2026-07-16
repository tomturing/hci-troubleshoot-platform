<script setup lang="ts">
/**
 * ProducesEditor - 产出变量数组可视化编辑器
 *
 * 用于 QKV 工具的 produces 字段编辑。
 * 每条产出变量包含：
 *   - name: 输出变量名（建议大写格式，如 HOST、VM_ID）
 *   - path: JSON 字段路径（支持 | 分隔多路径容错，如 host|hostname|hostid）
 *
 * 使用方式（v-model 双向绑定数组）：
 *   <ProducesEditor v-model="producesData" />
 */

import { computed } from 'vue'
import { Plus, Delete, InfoFilled } from '@element-plus/icons-vue'

const props = defineProps<{
  modelValue: Array<{ name: string; path: string }>
}>()

const emit = defineEmits<{
  'update:modelValue': [value: Array<{ name: string; path: string }>]
}>()

const produces = computed({
  get: () => props.modelValue || [],
  set: (val) => emit('update:modelValue', val),
})

/** 路径中 | 分隔符的个数 */
function pathCount(path: string): number {
  if (!path) return 0
  return path.split('|').filter(Boolean).length
}

function addItem() {
  produces.value = [...produces.value, { name: '', path: '' }]
}

function removeItem(idx: number) {
  produces.value = produces.value.filter((_, i) => i !== idx)
}

/** 在 path 字段中追加一个容错路径 */
function addFallbackPath(item: { name: string; path: string }) {
  if (item.path.trim()) {
    item.path = item.path.trim() + ' | '
  }
  // 触发响应式更新
  produces.value = [...produces.value]
}
</script>

<template>
  <div class="produces-editor">
    <div class="section-header">
      <div class="section-title">
        <el-icon class="title-icon"><InfoFilled /></el-icon>
        <span>产出变量 (produces)</span>
        <el-tooltip placement="top" :show-after="300">
          <template #content>
            <div style="max-width: 360px; line-height: 1.6;">
              定义要从查询结果中提取的变量，每个变量包含：
              <br/><b>name</b> — 输出变量名（建议大写下划线格式，如 HOST、VM_ID），后续信号通过 <code v-pre>{{变量名}}</code> 引用。
              <br/><b>path</b> — acli 返回值中的 JSON 字段路径，支持 <code>|</code> 分隔多路径容错（如 <code>host|hostname|hostid</code> 表示依次尝试）。
            </div>
          </template>
          <el-icon class="help-icon"><InfoFilled /></el-icon>
        </el-tooltip>
      </div>
      <el-button size="small" type="primary" text :icon="Plus" @click="addItem">
        添加变量
      </el-button>
    </div>

    <div v-for="(item, idx) in produces" :key="idx" class="produce-item">
      <el-row :gutter="12" align="middle">
        <el-col :span="8">
          <el-input
            v-model="item.name"
            placeholder="变量名（如 HOST）"
            spellcheck="false"
          >
            <template #prefix>
              <span class="input-prefix">NAME</span>
            </template>
          </el-input>
        </el-col>
        <el-col :span="13">
          <el-input
            v-model="item.path"
            placeholder="JSON Path（支持 | 分隔多路径容错）"
            spellcheck="false"
          >
            <template #prefix>
              <span class="input-prefix">PATH</span>
            </template>
          </el-input>
        </el-col>
        <el-col :span="3" style="text-align: right;">
          <el-button
            v-if="pathCount(item.path) >= 2"
            type="info"
            size="small"
            text
            title="路径数量提示"
          >
            <el-tag size="small" type="info" effect="plain" disable-transitions>
              {{ pathCount(item.path) }} 路径
            </el-tag>
          </el-button>
          <el-button
            type="danger"
            size="small"
            text
            :icon="Delete"
            title="删除此变量"
            @click="removeItem(idx)"
          />
        </el-col>
      </el-row>
      <div v-if="item.path.includes('|')" class="path-hint">
        容错路径：{{ item.path.split('|').map((p: string) => p.trim()).filter(Boolean).join(' → ') }}
      </div>
    </div>

    <el-empty
      v-if="produces.length === 0"
      description="暂无产出变量，点击上方「添加变量」按钮"
      :image-size="60"
    />
  </div>
</template>

<style scoped>
.produces-editor {
  border: 1px solid #e4e7ed;
  border-radius: 6px;
  padding: 16px;
  background: #fafbfc;
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
  color: #409eff;
}

.help-icon {
  color: #909399;
  cursor: help;
  font-size: 14px;
}

.produce-item {
  margin-bottom: 10px;
  padding: 8px 10px;
  background: #fff;
  border: 1px solid #ebeef5;
  border-radius: 4px;
  transition: border-color 0.2s;
}

.produce-item:hover {
  border-color: #c6e2ff;
}

.input-prefix {
  font-size: 11px;
  color: #909399;
  font-weight: 500;
  letter-spacing: 0.5px;
}

.path-hint {
  margin-top: 4px;
  font-size: 12px;
  color: #909399;
  padding-left: 4px;
}
</style>
