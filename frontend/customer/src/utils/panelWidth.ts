/**
 * 侧边抽屉的默认宽度。
 *
 * 第一性原理：抽屉是"贴着主内容区"的辅助面，宽度应当由主内容区的留白决定，
 * 而不是各面板各写一个魔法数字。历史工单、SSH 终端、日志收集共用同一算法，
 * 保证左右两侧抽屉视觉对齐。
 *
 * 规则：
 * 1. 主内容区固定上限 900px，两侧各分剩余留白；
 * 2. 留白不足 300px（窄屏）时退回 85% 视口宽；
 * 3. 下限 400px，避免极端窄屏把抽屉压成一条缝。
 */
export function getDefaultPanelWidth(): number {
  if (typeof window === 'undefined') return 510
  const appMaxWidth = 900
  // 两侧平分剩余留白
  const sideBlankSpace = Math.max((window.innerWidth - appMaxWidth) / 2, 0)
  // 屏幕过窄时直接占 85% 视口
  const target = sideBlankSpace >= 300 ? sideBlankSpace : window.innerWidth * 0.85
  return Math.max(400, Math.round(target))
}
