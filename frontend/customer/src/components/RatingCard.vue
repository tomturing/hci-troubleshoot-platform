<script setup lang="ts">
/**
 * 评分卡组件
 * 工单关闭后弹出，非阻塞式评分交互
 */

import { ref, watch, onUnmounted } from 'vue'

/** 组件 Props */
interface Props {
  /** 是否显示评分卡 */
  visible: boolean
  /** 会话 ID */
  conversationId: string | null
}

/** 组件事件 */
interface Emits {
  /** 提交评分事件 */
  (e: 'submit', score: number): void
  /** 跳过评分事件 */
  (e: 'skip'): void
  /** 关闭事件（包括超时自动关闭） */
  (e: 'close'): void
}

const props = defineProps<Props>()
const emit = defineEmits<Emits>()

/** 当前选中的评分（1-5） */
const selectedScore = ref<number>(0)

/** 是否已提交，防止重复提交 */
const isSubmitted = ref<boolean>(false)

/** 倒计时剩余秒数 */
const countdownSeconds = ref<number>(3)

/** 倒计时定时器 ID */
let countdownTimer: ReturnType<typeof setInterval> | null = null

/**
 * 监听显示状态变化
 * 显示时启动 3 秒倒计时，隐藏时重置状态
 */
watch(
  () => props.visible,
  (newVisible) => {
    if (newVisible) {
      // 重置状态
      selectedScore.value = 0
      isSubmitted.value = false
      countdownSeconds.value = 3
      startCountdown()
    } else {
      // 隐藏时清理定时器
      clearCountdown()
    }
  },
  { immediate: true },
)

/** 启动倒计时 */
function startCountdown() {
  clearCountdown()
  countdownTimer = setInterval(() => {
    countdownSeconds.value--
    if (countdownSeconds.value <= 0) {
      // 倒计时结束，自动关闭
      handleTimeout()
    }
  }, 1000)
}

/** 清理倒计时定时器 */
function clearCountdown() {
  if (countdownTimer) {
    clearInterval(countdownTimer)
    countdownTimer = null
  }
}

/** 倒计时超时处理 */
function handleTimeout() {
  if (!isSubmitted.value) {
    clearCountdown()
    emit('close')
  }
}

/**
 * 选择评分
 * @param score 评分值 1-5
 */
function selectScore(score: number) {
  if (isSubmitted.value) return
  selectedScore.value = score
}

/** 提交评分 */
function handleSubmit() {
  if (isSubmitted.value || selectedScore.value === 0) return

  isSubmitted.value = true
  clearCountdown()
  emit('submit', selectedScore.value)
}

/** 跳过评分 */
function handleSkip() {
  if (isSubmitted.value) return

  isSubmitted.value = true
  clearCountdown()
  emit('skip')
}

/** 组件卸载时清理定时器 */
onUnmounted(() => {
  clearCountdown()
})
</script>

<template>
  <Transition name="rating-card">
    <div v-if="visible" class="rating-card" @click.stop>
      <!-- 标题 -->
      <div class="rating-title">本次排障对您有帮助吗？</div>

      <!-- 星级评分 -->
      <div class="rating-stars">
        <span
          v-for="star in 5"
          :key="star"
          class="star"
          :class="{
            active: star <= selectedScore,
            hover: !isSubmitted,
          }"
          @click="selectScore(star)"
        >
          <svg viewBox="0 0 24 24" fill="currentColor">
            <path
              d="M12 17.27L18.18 21l-1.64-7.03L22 9.24l-7.19-.61L12 2 9.19 8.63 2 9.24l5.46 4.73L5.82 21z"
            />
          </svg>
        </span>
      </div>

      <!-- 评分提示文字 -->
      <div class="rating-hint">
        <span v-if="selectedScore === 0">请点击星星评分</span>
        <span v-else-if="selectedScore === 1">非常不满意</span>
        <span v-else-if="selectedScore === 2">不满意</span>
        <span v-else-if="selectedScore === 3">一般</span>
        <span v-else-if="selectedScore === 4">满意</span>
        <span v-else>非常满意</span>
      </div>

      <!-- 操作按钮 -->
      <div class="rating-actions">
        <button class="btn-skip" @click="handleSkip" :disabled="isSubmitted">
          跳过
        </button>
        <button
          class="btn-submit"
          :disabled="selectedScore === 0 || isSubmitted"
          @click="handleSubmit"
        >
          提交
        </button>
      </div>

      <!-- 倒计时进度条 -->
      <div class="countdown-bar">
        <div
          class="countdown-progress"
          :style="{ width: `${(countdownSeconds / 3) * 100}%` }"
        />
      </div>
    </div>
  </Transition>
</template>

<style scoped>
/** 评分卡容器 - 非阻塞式 toast 样式 */
.rating-card {
  position: fixed;
  bottom: 120px;
  left: 50%;
  transform: translateX(-50%);
  background: #ffffff;
  border-radius: 12px;
  padding: 20px 24px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.12), 0 2px 8px rgba(0, 0, 0, 0.08);
  z-index: 2000;
  min-width: 280px;
  max-width: 360px;
  text-align: center;
  border: 1px solid #ebeef5;
  pointer-events: auto;
}

/** 标题 */
.rating-title {
  font-size: 15px;
  font-weight: 500;
  color: #303133;
  margin-bottom: 16px;
}

/** 星级容器 */
.rating-stars {
  display: flex;
  justify-content: center;
  gap: 8px;
  margin-bottom: 8px;
}

/** 单个星星 */
.star {
  width: 32px;
  height: 32px;
  cursor: pointer;
  color: #dcdfe6;
  transition: all 0.2s ease;
}

.star svg {
  width: 100%;
  height: 100%;
}

/** 鼠标悬停效果 */
.star.hover:hover {
  transform: scale(1.15);
}

/** 选中状态 */
.star.active {
  color: #f7ba2a;
}

/** 评分提示文字 */
.rating-hint {
  font-size: 13px;
  color: #909399;
  margin-bottom: 16px;
  height: 18px;
}

/** 操作按钮区域 */
.rating-actions {
  display: flex;
  justify-content: center;
  gap: 12px;
}

/** 跳过按钮 */
.btn-skip {
  padding: 8px 20px;
  border: 1px solid #dcdfe6;
  background: #ffffff;
  color: #606266;
  border-radius: 6px;
  font-size: 14px;
  cursor: pointer;
  transition: all 0.2s ease;
}

.btn-skip:hover:not(:disabled) {
  border-color: #c6c8cc;
  color: #303133;
  background: #f5f7fa;
}

.btn-skip:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

/** 提交按钮 */
.btn-submit {
  padding: 8px 20px;
  border: none;
  background: #409eff;
  color: #ffffff;
  border-radius: 6px;
  font-size: 14px;
  cursor: pointer;
  transition: all 0.2s ease;
}

.btn-submit:hover:not(:disabled) {
  background: #66b1ff;
}

.btn-submit:disabled {
  background: #a0cfff;
  cursor: not-allowed;
}

/** 倒计时进度条 */
.countdown-bar {
  position: absolute;
  bottom: 0;
  left: 0;
  right: 0;
  height: 3px;
  background: #ebeef5;
  border-radius: 0 0 12px 12px;
  overflow: hidden;
}

.countdown-progress {
  height: 100%;
  background: linear-gradient(90deg, #409eff, #66b1ff);
  transition: width 1s linear;
}

/** 进入/离开动画 */
.rating-card-enter-active {
  transition: all 0.3s ease;
}

.rating-card-leave-active {
  transition: all 0.2s ease;
}

.rating-card-enter-from {
  opacity: 0;
  transform: translateX(-50%) translateY(20px);
}

.rating-card-leave-to {
  opacity: 0;
  transform: translateX(-50%) translateY(-10px);
}
</style>
