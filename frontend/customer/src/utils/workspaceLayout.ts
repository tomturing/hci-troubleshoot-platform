/**
 * 工作区三栏布局求解器（中央会话 / 左侧抽屉 / 右侧终端）。
 *
 * 第一性原理：水平像素是唯一稀缺资源，三块内容的可用宽度应当由
 * 「每一块内容自己的最小可读宽度」正向推导，而不是「中央固定 900px、
 * 剩下的边角料分给抽屉」这种惯性写法——后者正是两侧抽屉同时展开时
 * 日志表格逐字换行、终端列数不足的根因。
 *
 * 这里只做纯计算，渲染层（App.vue / 抽屉组件）消费结果。
 */

export type WorkspaceMode = 'expanded' | 'slide' | 'compressed' | 'overlay'

export interface WorkspaceLayoutInput {
  /** 视口宽度（window.innerWidth） */
  viewport: number
  /** 左侧抽屉是否打开（日志收集 或 历史工单，两者互斥） */
  leftOpen: boolean
  /** 右侧抽屉是否打开（SSH 终端） */
  rightOpen: boolean
  /** 用户拖拽后的左侧偏好宽度，未拖拽时用默认值 */
  leftPreferred?: number
  /** 用户拖拽后的右侧偏好宽度，未拖拽时用默认值 */
  rightPreferred?: number
}

export interface WorkspaceLayout {
  mode: WorkspaceMode
  leftWidth: number
  rightWidth: number
  /** 中央会话区在当前剩余空间内允许的最大宽度（用于居中与紧凑排版） */
  centerMaxWidth: number
  /** 主内容区需要让出的左侧像素（push 布局） */
  leftOffset: number
  /** 主内容区需要让出的右侧像素（push 布局） */
  rightOffset: number
}

/**
 * 硬约束常量。
 *
 * - CENTER_MAX 沿用现有阅读宽度，保证「无抽屉」时不劣化；
 * - CENTER_PREF 是双开时优先保留的会话宽度，560px 仍能容纳代码块与输入框；
 * - CENTER_FLOOR 是气泡 + 输入框的最低可读宽度，低于此值宁可降级为覆盖式；
 * - LEFT_* 由描述表单列（约 420px）与信息密度上限（约 720px）决定；
 * - RIGHT_* 由 xterm 列数决定：约 66 列 ≈ 560px，90 列 ≈ 820px。
 */
export const WORKSPACE_CONSTRAINTS = {
  centerMax: 900,
  centerPref: 560,
  centerFloor: 460,
  leftMin: 420,
  leftMax: 720,
  rightMin: 560,
  rightMax: 820,
  /** 低于该视口宽度时，任何抽屉都走覆盖式 */
  narrowViewport: 1024,
  /** 覆盖式抽屉占视口宽度的比例 */
  overlayRatio: 0.86,
  /** 单侧抽屉的默认偏好宽度 */
  leftDefault: 520,
  rightDefault: 720,
  /** 左右抽屉分配剩余空间时的权重（右侧终端更吃宽度） */
  leftWeight: 0.42,
} as const

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value))
}

function overlayLayout(leftOpen: boolean, rightOpen: boolean, viewport: number): WorkspaceLayout {
  const { overlayRatio, centerMax } = WORKSPACE_CONSTRAINTS
  // 覆盖式抽屉仍要能看清单列表格：下限 320，上限 560，避免窄屏只剩一条缝
  const width = Math.round(clamp(viewport * overlayRatio, 320, 560))
  return {
    mode: 'overlay',
    leftWidth: leftOpen ? width : 0,
    rightWidth: rightOpen ? width : 0,
    centerMaxWidth: centerMax,
    leftOffset: 0,
    rightOffset: 0,
  }
}

/**
 * 求解当前视口与抽屉开合状态下的三栏布局。
 *
 * 三态语义：
 * - expanded：无抽屉，中央 900 居中，两侧留白（大气）；
 * - slide：单抽屉，抽屉推挤占位，中央在剩余空间内居中；
 * - compressed：双抽屉，中央被压缩但有下限，两侧按权重分配；
 * - overlay：窄屏降级，抽屉覆盖在中央之上并带遮罩，不再强行挤压。
 */
export function resolveWorkspaceLayout(input: WorkspaceLayoutInput): WorkspaceLayout {
  const c = WORKSPACE_CONSTRAINTS
  const viewport = Math.max(0, Math.round(input.viewport || 0))
  const { leftOpen, rightOpen } = input

  // 0. 无抽屉：保持现状，中央 900 居中
  if (!leftOpen && !rightOpen) {
    return {
      mode: 'expanded',
      leftWidth: 0,
      rightWidth: 0,
      centerMaxWidth: c.centerMax,
      leftOffset: 0,
      rightOffset: 0,
    }
  }

  // 1. 窄屏：三栏与「推挤单侧」都放不下，统一降级为覆盖式
  if (viewport < c.narrowViewport) {
    return overlayLayout(leftOpen, rightOpen, viewport)
  }

  // 2. 双开：先按 CENTER_PREF 保留会话宽度，剩余按两侧偏好比例分配。
  // 用偏好比例而不是固定权重：用户拖拽过的宽度在单开/双开之间保持连贯，
  // 默认偏好（520 / 720）算出来的比例恰好等于 0.42，与设计权重一致。
  if (leftOpen && rightOpen) {
    const available = viewport - c.centerPref
    const leftPreferred = input.leftPreferred ?? c.leftDefault
    const rightPreferred = input.rightPreferred ?? c.rightDefault
    const share = leftPreferred / Math.max(1, leftPreferred + rightPreferred)
    let left = Math.round(clamp(available * share, c.leftMin, c.leftMax))
    let right = Math.round(clamp(available - left, c.rightMin, c.rightMax))
    // 一侧被封顶时，把富余让给另一侧，避免中央出现无意义的留白
    left = Math.round(clamp(available - right, c.leftMin, c.leftMax))
    let center = viewport - left - right

    // 两侧都低于下限时按比例分配的宽度必然挤到中央，退回各自下限重算
    if (center < c.centerFloor) {
      left = c.leftMin
      right = c.rightMin
      center = viewport - left - right
    }

    // 仍是放不下：这已经是物理约束，不再硬塞，降级为覆盖式
    if (center < c.centerFloor) {
      return overlayLayout(leftOpen, rightOpen, viewport)
    }

    return {
      mode: 'compressed',
      leftWidth: left,
      rightWidth: right,
      centerMaxWidth: Math.min(c.centerMax, center),
      leftOffset: left,
      rightOffset: right,
    }
  }

  // 3. 单开：抽屉尽量取偏好宽度，但不许吃掉中央的 CENTER_PREF
  if (leftOpen) {
    const max = Math.min(c.leftMax, Math.max(c.leftMin, viewport - c.centerPref))
    const width = Math.round(clamp(input.leftPreferred ?? c.leftDefault, c.leftMin, max))
    return {
      mode: 'slide',
      leftWidth: width,
      rightWidth: 0,
      centerMaxWidth: Math.min(c.centerMax, viewport - width),
      leftOffset: width,
      rightOffset: 0,
    }
  }

  const max = Math.min(c.rightMax, Math.max(c.rightMin, viewport - c.centerPref))
  const width = Math.round(clamp(input.rightPreferred ?? c.rightDefault, c.rightMin, max))
  return {
    mode: 'slide',
    leftWidth: 0,
    rightWidth: width,
    centerMaxWidth: Math.min(c.centerMax, viewport - width),
    leftOffset: 0,
    rightOffset: width,
  }
}

/** 当前视口下左侧抽屉允许的拖拽区间，供拖拽逻辑与测试共用 */
export function leftWidthRange(viewport: number): { min: number; max: number } {
  const c = WORKSPACE_CONSTRAINTS
  return { min: c.leftMin, max: Math.min(c.leftMax, Math.max(c.leftMin, viewport - c.centerPref)) }
}

/** 当前视口下右侧抽屉允许的拖拽区间 */
export function rightWidthRange(viewport: number): { min: number; max: number } {
  const c = WORKSPACE_CONSTRAINTS
  return { min: c.rightMin, max: Math.min(c.rightMax, Math.max(c.rightMin, viewport - c.centerPref)) }
}
