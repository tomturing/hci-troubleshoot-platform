<script setup lang="ts">
/**
 * 侧栏面板外壳。
 *
 * 为什么不用 el-drawer：抽屉是「模态浮层」语义（遮罩、焦点锁定、ESC、translate 滑出），
 * 用它做常驻分栏会不断和这些语义打架——推挤只能靠主区补 padding、关闭瞬间
 * size 归零导致宽度被压扁、窄屏还得再套一层遮罩降级。
 * 这里就是一个普通 flex 子项：宽度由外部求解结果决定，折叠后保留 48px 图标条，
 * 和 VS Code 的侧栏/活动栏是同一套模型。
 */
import { computed } from 'vue'
import { ArrowLeft, ArrowRight } from '@element-plus/icons-vue'

const props = withDefaults(
  defineProps<{
    side: 'left' | 'right'
    open: boolean
    /** 展开态宽度（由 workspaceLayout 求解给出） */
    width: number
    /** 窄屏降级：绝对定位覆盖在中央区之上 */
    overlay?: boolean
    /** 是否正在拖拽（拖拽时关掉宽度过渡，保证跟手） */
    resizing?: boolean
  }>(),
  { overlay: false, resizing: false },
)

defineEmits<{
  (e: 'collapse'): void
  (e: 'resizeStart', ev: MouseEvent): void
}>()

/** 折叠态的固定宽度：够放一个 34px 图标按钮 */
const RAIL_WIDTH = 48

const style = computed(() => ({
  width: `${props.open ? props.width : RAIL_WIDTH}px`,
}))

/** 折叠方向图标：左侧面板往左收，右侧往右收 */
const collapseIcon = computed(() => (props.side === 'left' ? ArrowLeft : ArrowRight))
</script>

<template>
  <aside
    class="side-panel"
    :class="[
      `side-panel--${side}`,
      { 'is-collapsed': !open, 'is-overlay': overlay && open, 'is-resizing': resizing },
    ]"
    :style="style"
  >
    <!-- 折叠态：常驻图标条。折叠不等于消失，入口必须一直在（VS Code 活动栏同款） -->
    <div v-show="!open" class="panel-rail">
      <slot name="rail" />
    </div>

    <!--
      内容用 v-show 而不是 v-if：终端/日志面板内部持有 xterm 与 SSH 会话，
      折叠一次就销毁会导致重连与状态丢失。折叠时靠 overflow:hidden 收成 48px，
      内容同步淡出，避免"文字被切一半"。
    -->
    <div v-show="open" class="panel-main">
      <div class="panel-head">
        <div class="panel-head-title">
          <slot name="title" />
        </div>
        <div class="panel-head-actions">
          <slot name="actions" />
          <el-tooltip content="收起（保留图标入口）" placement="bottom" :show-after="400">
            <button class="panel-collapse-btn" type="button" @click="$emit('collapse')">
              <el-icon><component :is="collapseIcon" /></el-icon>
            </button>
          </el-tooltip>
        </div>
      </div>

      <div class="panel-body">
        <slot />
      </div>

      <div
        v-if="!overlay"
        class="panel-resizer"
        title="拖拽调整宽度"
        @mousedown="$emit('resizeStart', $event)"
      />
    </div>
  </aside>
</template>

<style scoped>
.side-panel {
  position: relative;
  flex: 0 0 auto;
  display: flex;
  flex-direction: column;
  height: 100%;
  min-width: 0;
  background: #fff;
  overflow: hidden;
  /* 折叠/展开是宽度过渡（不是位移），与内容换密度同一个节奏 */
  transition: width 0.3s cubic-bezier(0.4, 0, 0.2, 1);
}

.side-panel--left {
  border-right: 1px solid #e4e7ed;
}

.side-panel--right {
  border-left: 1px solid #e4e7ed;
}

/* 拖拽时逐帧跟手，过渡会让面板滞后于手柄 */
.side-panel.is-resizing {
  transition: none;
}

.side-panel.is-overlay {
  position: absolute;
  top: 0;
  bottom: 0;
  z-index: 30;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.18);
}

.side-panel--left.is-overlay {
  left: 0;
}

.side-panel--right.is-overlay {
  right: 0;
}

.panel-main {
  height: 100%;
  display: flex;
  flex-direction: column;
  min-width: 0;
  transition: opacity 0.15s ease;
}

/* 折叠动画期间内容快速淡出，宽度收成 48px 后只剩图标条 */
.side-panel.is-collapsed .panel-main {
  opacity: 0;
  pointer-events: none;
}

.panel-head {
  flex: 0 0 48px;
  height: 48px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 0 6px 0 14px;
  border-bottom: 1px solid #e4e7ed;
  background: #fff;
}

.panel-head-title {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  font-size: 14px;
  font-weight: 600;
  color: #303133;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.panel-head-actions {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-shrink: 0;
}

.panel-collapse-btn {
  width: 28px;
  height: 28px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: #606266;
  cursor: pointer;
  transition: background-color 0.15s;
}

.panel-collapse-btn:hover {
  background: #f0f2f5;
}

.panel-body {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

/* 折叠态图标条：宽度固定 48px，图标纵向排列 */
.panel-rail {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  padding: 8px 0;
  background: #fafbfc;
}

.panel-rail :deep(.rail-btn) {
  width: 34px;
  height: 34px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: none;
  border-radius: 8px;
  background: transparent;
  color: #606266;
  font-size: 17px;
  cursor: pointer;
  transition: background-color 0.15s, color 0.15s;
}

.panel-rail :deep(.rail-btn:hover) {
  background: #eef1f6;
  color: #409eff;
}

.panel-rail :deep(.rail-btn.is-active) {
  background: #ecf5ff;
  color: #409eff;
}

/*
 * 可拖拽边界。
 *
 * 命中区必须画在容器「内侧」：aside 是 overflow:hidden，原来用 right:-3px
 * 把 6px 手柄的一半挂到容器外，结果被裁得只剩 3px，而且正好压在窗口边缘——
 * 鼠标既难对准，又容易被系统当成窗口缩放，实际表现为「边框拖不动」。
 * 现在 9px 全部落在内侧，常态不画线，悬停/拖拽时才出现 2px 高亮。
 */
.panel-resizer {
  position: absolute;
  top: 0;
  bottom: 0;
  width: 9px;
  cursor: col-resize;
  z-index: 3;
  background: transparent;
}

.side-panel--left .panel-resizer {
  right: 0;
}

.side-panel--right .panel-resizer {
  left: 0;
}

.panel-resizer::after {
  content: '';
  position: absolute;
  top: 0;
  bottom: 0;
  width: 2px;
  background: transparent;
  transition: background-color 0.15s ease;
}

.side-panel--left .panel-resizer::after {
  right: 0;
}

.side-panel--right .panel-resizer::after {
  left: 0;
}

.panel-resizer:hover::after,
.side-panel.is-resizing .panel-resizer::after {
  background: #409eff;
}
</style>
