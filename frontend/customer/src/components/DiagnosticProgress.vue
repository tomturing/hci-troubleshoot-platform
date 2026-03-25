<script setup lang="ts">
import { computed } from 'vue'

/**
 * 诊断阶段进度条组件
 * 展示 S0~S6 的诊断阶段进度，当前阶段高亮
 */
const props = defineProps<{
  /** 当前阶段 S0~S6 */
  stage: string
}>()

/** 阶段标签映射 */
const stageLabels = ['问题收集', '信息确认', '初步诊断', '故障假设', '验证假设', '方案确认', '执行修复']

/** 获取阶段索引 (0-6) */
const stageIndex = computed(() => {
  const match = props.stage.match(/^S(\d)$/)
  return match ? parseInt(match[1], 10) : 0
})

/** 当前阶段标签 */
const currentLabel = computed(() => stageLabels[stageIndex.value] || '未知阶段')

/** 是否为移动端视图 (≤375px) */
const isMobile = computed(() => typeof window !== 'undefined' && window.innerWidth <= 375)
</script>

<template>
  <!-- 移动端：单行文字展示 -->
  <div v-if="isMobile" class="diagnostic-progress-mobile">
    <span class="stage-indicator">{{ stageIndex + 1 }}/7</span>
    <span class="stage-label">{{ currentLabel }}</span>
  </div>

  <!-- 桌面端：水平步骤条 -->
  <div v-else class="diagnostic-progress">
    <div class="progress-track">
      <!-- 已完成阶段 (绿色) -->
      <div
        v-for="i in 7"
        :key="i"
        class="progress-node"
        :class="{
          'done': i - 1 < stageIndex,
          'active': i - 1 === stageIndex,
        }"
      >
        <div class="node-circle">
          <span v-if="i - 1 < stageIndex" class="check-icon">✓</span>
          <span v-else class="node-number">{{ i }}</span>
        </div>
        <div class="node-label">{{ stageLabels[i - 1] }}</div>
      </div>
      <!-- 连接线 -->
      <div class="progress-line-wrapper">
        <div
          class="progress-line"
          :style="{ width: `${(stageIndex / 6) * 100}%` }"
        ></div>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 移动端样式 */
.diagnostic-progress-mobile {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 16px;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  border-radius: 8px;
  color: #fff;
  font-size: 14px;
  margin-bottom: 8px;
}

.stage-indicator {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  background: rgba(255, 255, 255, 0.3);
  border-radius: 50%;
  font-weight: 600;
  font-size: 12px;
}

.stage-label {
  font-weight: 500;
}

/* 桌面端样式 */
.diagnostic-progress {
  padding: 12px 16px;
  background: #fff;
  border-radius: 8px;
  margin-bottom: 8px;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.08);
}

.progress-track {
  display: flex;
  justify-content: space-between;
  position: relative;
}

.progress-line-wrapper {
  position: absolute;
  top: 14px;
  left: 20px;
  right: 20px;
  height: 2px;
  background: #e4e7ed;
  z-index: 0;
}

.progress-line {
  height: 100%;
  background: linear-gradient(90deg, #67c23a 0%, #409eff 100%);
  transition: width 0.3s ease;
}

.progress-node {
  display: flex;
  flex-direction: column;
  align-items: center;
  position: relative;
  z-index: 1;
}

.node-circle {
  width: 28px;
  height: 28px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #fff;
  border: 2px solid #e4e7ed;
  transition: all 0.3s ease;
}

.node-number {
  font-size: 12px;
  font-weight: 600;
  color: #909399;
}

/* 已完成阶段 */
.progress-node.done .node-circle {
  background: #67c23a;
  border-color: #67c23a;
}

.progress-node.done .node-number {
  color: #fff;
}

.check-icon {
  color: #fff;
  font-size: 14px;
  font-weight: bold;
}

.node-label {
  margin-top: 6px;
  font-size: 11px;
  color: #909399;
  white-space: nowrap;
  transition: color 0.3s ease;
}

/* 当前活跃阶段 */
.progress-node.active .node-circle {
  background: #409eff;
  border-color: #409eff;
  box-shadow: 0 0 0 4px rgba(64, 158, 255, 0.2);
}

.progress-node.active .node-number {
  color: #fff;
}

.progress-node.active .node-label {
  color: #409eff;
  font-weight: 600;
}
</style>