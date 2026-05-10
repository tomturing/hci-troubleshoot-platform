<template>
  <!-- 交互选项列表（共用于 MessageBubble 气泡和 InteractiveRequestCard 弹框）
       提交后：所有选项保持可见，已选项蓝色高亮，其余置灰不可再点。 -->
  <div v-if="options?.length" class="ir-options">
    <span class="ir-label">可选回复</span>
    <div class="ir-option-list">
      <el-button
        v-for="opt in options"
        :key="opt.optionId"
        size="default"
        :type="selectedOptionId === opt.optionId ? 'primary' : 'default'"
        :disabled="isSubmitted || submitting"
        :class="{ 'ir-opt--dimmed': isSubmitted && selectedOptionId !== opt.optionId }"
        @click="handleClick(opt.optionId, opt.name)"
      >
        <span class="ir-opt-id">{{ opt.optionId }}.</span> {{ opt.name }}
      </el-button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{
  /** 选项列表 */
  options: Array<{ optionId: string; name: string }>
  /** 已选中的选项 ID；null 表示尚未提交 */
  selectedOptionId: string | null
  /** 正在提交中（显示 loading 状态） */
  submitting?: boolean
}>()

const emit = defineEmits<{
  /** 用户点击选项，父组件负责提交和设置 selectedOptionId */
  select: [optionId: string, name: string]
}>()

const isSubmitted = computed(() => props.selectedOptionId !== null)

function handleClick(optionId: string, name: string) {
  if (isSubmitted.value || props.submitting) return
  emit('select', optionId, name)
}
</script>

<style scoped>
.ir-options {
  margin-top: 10px;
}

.ir-label {
  display: inline-block;
  font-size: 11px;
  font-weight: 600;
  color: #909399;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 4px;
}

.ir-option-list {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 6px;
}

.ir-opt-id {
  color: #909399;
  margin-right: 2px;
}

/* 已提交后非选中项置灰，与 choice-btn--interacted 保持一致 */
.ir-opt--dimmed {
  opacity: 0.45;
}
</style>
