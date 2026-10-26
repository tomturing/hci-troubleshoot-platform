/**
 * workspaceLayout.ts 单元测试
 *
 * 锁定三态布局的核心契约（都来自 2026-10-26 现场截图问题）：
 * 1. 无抽屉时中央必须是 900px 居中，不允许被布局系统悄悄改窄；
 * 2. 单抽屉时中央在剩余空间内居中，只要剩余 ≥ 900 就仍取 900；
 * 3. 双抽屉时中央不得低于下限，两侧宽度之和必须与视口严格对齐，不允许溢出或留缝；
 * 4. 窄屏 / 放不下时必须降级为覆盖式，绝不硬塞三栏。
 */
import { describe, expect, it } from 'vitest'
import {
  WORKSPACE_CONSTRAINTS as C,
  leftWidthRange,
  resolveWorkspaceLayout,
  rightWidthRange,
} from '../workspaceLayout'

const viewports = [1920, 1600, 1500, 1440, 1366, 1280, 1024, 900, 768, 375]

describe('resolveWorkspaceLayout', () => {
  describe('展开态（无抽屉）', () => {
    it.each(viewports)('%ipx 下中央保持 900px 且不让位', (viewport) => {
      const layout = resolveWorkspaceLayout({ viewport, leftOpen: false, rightOpen: false })
      expect(layout.mode).toBe('expanded')
      expect(layout.centerMaxWidth).toBe(C.centerMax)
      expect(layout.leftOffset).toBe(0)
      expect(layout.rightOffset).toBe(0)
      // 抽屉宽度是「面板属性」：即使关着也要有合法值，
      // 否则关闭动画的第一帧抽屉会被压成 0 宽（现场表现为「收窄成一个按钮后消失」）
      expect(layout.leftWidth).toBeGreaterThan(0)
      expect(layout.rightWidth).toBeGreaterThan(0)
    })
  })

  describe('关闭态的抽屉宽度（退出动画前置条件）', () => {
    it('双开→全关：两侧宽度与打开时同一量级，不会归零', () => {
      const open = resolveWorkspaceLayout({ viewport: 1920, leftOpen: true, rightOpen: true })
      const closed = resolveWorkspaceLayout({ viewport: 1920, leftOpen: false, rightOpen: false })
      expect(closed.leftWidth).toBeGreaterThanOrEqual(C.leftMin)
      expect(closed.rightWidth).toBeGreaterThanOrEqual(C.rightMin)
      // 关闭态回到各自偏好宽度，仍落在打开态的合法区间内
      expect(closed.leftWidth).toBe(C.leftDefault)
      expect(closed.rightWidth).toBe(C.rightDefault)
      expect(closed.leftWidth + closed.rightWidth).toBeLessThanOrEqual(
        open.leftWidth + open.rightWidth,
      )
    })

    it('只开右侧时左侧宽度仍合法：左抽屉可以用正常动画退出', () => {
      const layout = resolveWorkspaceLayout({ viewport: 1920, leftOpen: false, rightOpen: true })
      expect(layout.leftWidth).toBe(C.leftDefault)
      expect(layout.leftOffset).toBe(0)
    })

    it.each(viewports)('%ipx 任何开合组合下两侧宽度都为正', (viewport) => {
      for (const leftOpen of [true, false]) {
        for (const rightOpen of [true, false]) {
          const layout = resolveWorkspaceLayout({ viewport, leftOpen, rightOpen })
          expect(layout.leftWidth).toBeGreaterThan(0)
          expect(layout.rightWidth).toBeGreaterThan(0)
        }
      }
    })
  })

  describe('侧滑态（单抽屉）', () => {
    it('1920px 开左侧：抽屉 520，中央仍是 900', () => {
      const layout = resolveWorkspaceLayout({ viewport: 1920, leftOpen: true, rightOpen: false })
      expect(layout.mode).toBe('slide')
      expect(layout.leftWidth).toBe(C.leftDefault)
      expect(layout.leftOffset).toBe(C.leftDefault)
      expect(layout.rightOffset).toBe(0)
      // 剩余 1400 > 900，中央不该被压
      expect(layout.centerMaxWidth).toBe(C.centerMax)
    })

    it('1920px 开右侧：终端取 720，中央仍是 900', () => {
      const layout = resolveWorkspaceLayout({ viewport: 1920, leftOpen: false, rightOpen: true })
      expect(layout.mode).toBe('slide')
      expect(layout.rightWidth).toBe(C.rightDefault)
      expect(layout.centerMaxWidth).toBe(C.centerMax)
    })

    it('1440px 开右侧：终端仍取偏好 720，中央压到剩余 720 而不是被无视', () => {
      const layout = resolveWorkspaceLayout({ viewport: 1440, leftOpen: false, rightOpen: true })
      expect(layout.mode).toBe('slide')
      expect(layout.rightWidth).toBe(C.rightDefault)
      expect(layout.centerMaxWidth).toBe(1440 - C.rightDefault)
    })

    it('1100px 开右侧：终端不许吃穿中央的 560，被压到下限 560', () => {
      const layout = resolveWorkspaceLayout({ viewport: 1100, leftOpen: false, rightOpen: true })
      expect(layout.rightWidth).toBe(C.rightMin)
      expect(layout.centerMaxWidth).toBe(1100 - C.rightMin)
    })

    it('拖拽偏好参与求解并被 clamp 到区间内', () => {
      const wide = resolveWorkspaceLayout({
        viewport: 1920,
        leftOpen: false,
        rightOpen: true,
        rightPreferred: 5000,
      })
      expect(wide.rightWidth).toBe(C.rightMax)

      const narrow = resolveWorkspaceLayout({
        viewport: 1920,
        leftOpen: true,
        rightOpen: false,
        leftPreferred: 100,
      })
      expect(narrow.leftWidth).toBe(C.leftMin)
    })

    it('1024px 附近仍可推挤单侧，中区不低于下限', () => {
      const layout = resolveWorkspaceLayout({ viewport: 1024, leftOpen: true, rightOpen: false })
      expect(layout.mode).toBe('slide')
      expect(1024 - layout.leftWidth).toBeGreaterThanOrEqual(C.centerFloor)
    })
  })

  describe('压缩态（双抽屉）', () => {
    it.each([1920, 1600, 1500, 1440])('%ipx 双开：三栏宽度之和等于视口，中区不低于下限', (viewport) => {
      const layout = resolveWorkspaceLayout({ viewport, leftOpen: true, rightOpen: true })
      expect(layout.mode).toBe('compressed')
      expect(layout.leftWidth + layout.rightWidth + layout.centerMaxWidth).toBe(viewport)
      expect(layout.centerMaxWidth).toBeGreaterThanOrEqual(C.centerFloor)
      expect(layout.leftWidth).toBeGreaterThanOrEqual(C.leftMin)
      expect(layout.rightWidth).toBeGreaterThanOrEqual(C.rightMin)
      expect(layout.leftOffset).toBe(layout.leftWidth)
      expect(layout.rightOffset).toBe(layout.rightWidth)
    })

    it('1920px 双开：中区保留 560，右侧终端优先拿到更多宽度', () => {
      const layout = resolveWorkspaceLayout({ viewport: 1920, leftOpen: true, rightOpen: true })
      expect(layout.centerMaxWidth).toBe(C.centerPref)
      expect(layout.rightWidth).toBeGreaterThan(layout.leftWidth)
      // 80 列终端约需 672px，压缩态必须满足
      expect(layout.rightWidth).toBeGreaterThanOrEqual(672)
    })

    it('拖拽后的偏好比例在双开时继续生效', () => {
      const layout = resolveWorkspaceLayout({
        viewport: 1920,
        leftOpen: true,
        rightOpen: true,
        leftPreferred: 700,
        rightPreferred: 620,
      })
      expect(layout.leftWidth).toBeGreaterThan(layout.rightWidth)
      expect(layout.leftWidth + layout.rightWidth + layout.centerMaxWidth).toBe(1920)
    })
  })

  describe('覆盖态（窄屏降级）', () => {
    it.each([1366, 1280, 1024, 900, 768, 375])('%ipx 双开降级为覆盖式，不让位', (viewport) => {
      const layout = resolveWorkspaceLayout({ viewport, leftOpen: true, rightOpen: true })
      expect(layout.mode).toBe('overlay')
      expect(layout.leftOffset).toBe(0)
      expect(layout.rightOffset).toBe(0)
      expect(layout.centerMaxWidth).toBe(C.centerMax)
      expect(layout.leftWidth).toBeGreaterThan(0)
      expect(layout.rightWidth).toBeGreaterThan(0)
    })

    it.each([900, 768, 375])('%ipx 单开也降级为覆盖式', (viewport) => {
      const layout = resolveWorkspaceLayout({ viewport, leftOpen: false, rightOpen: true })
      expect(layout.mode).toBe('overlay')
      expect(layout.rightOffset).toBe(0)
      expect(layout.rightWidth).toBeLessThanOrEqual(560)
    })
  })

  describe('不变量（连续视口扫描）', () => {
    it('1024~2560 区间内：压缩态精确填满，侧滑态不越界，中区永不低于下限', () => {
      for (let viewport = 1024; viewport <= 2560; viewport += 10) {
        for (const [leftOpen, rightOpen] of [
          [true, false],
          [false, true],
          [true, true],
        ] as const) {
          const layout = resolveWorkspaceLayout({ viewport, leftOpen, rightOpen })
          if (layout.mode === 'overlay') continue

          // 打开的一侧：让位宽度必须等于抽屉宽度，否则抽屉会盖住或悬空；
          // 未打开的一侧：不让位（offset 恒为 0），但宽度仍保留给退出动画
          expect(layout.leftOffset).toBe(leftOpen ? layout.leftWidth : 0)
          expect(layout.rightOffset).toBe(rightOpen ? layout.rightWidth : 0)
          // 中央区不得越出剩余空间
          expect(layout.centerMaxWidth).toBeLessThanOrEqual(
            viewport - layout.leftOffset - layout.rightOffset,
          )
          expect(layout.centerMaxWidth).toBeGreaterThanOrEqual(C.centerFloor)

          if (layout.mode === 'compressed') {
            // 中区未触顶时必须精确填满；触顶 900 后剩余像素作为留白，不允许溢出
            const total = layout.leftWidth + layout.centerMaxWidth + layout.rightWidth
            if (layout.centerMaxWidth === C.centerMax) {
              expect(total).toBeLessThanOrEqual(viewport)
            } else {
              expect(total).toBe(viewport)
            }
          }
        }
      }
    })

    it('非法视口（0 / 负数）不产生负宽度', () => {
      for (const viewport of [0, -100]) {
        const layout = resolveWorkspaceLayout({ viewport, leftOpen: true, rightOpen: true })
        expect(layout.leftWidth).toBeGreaterThanOrEqual(0)
        expect(layout.rightWidth).toBeGreaterThanOrEqual(0)
        expect(layout.centerMaxWidth).toBeGreaterThan(0)
      }
    })
  })
})

describe('拖拽区间', () => {
  it('左侧区间与求解器常量同源', () => {
    expect(leftWidthRange(1920)).toEqual({ min: C.leftMin, max: C.leftMax })
    expect(leftWidthRange(1200)).toEqual({ min: C.leftMin, max: 1200 - C.centerPref })
  })

  it('右侧区间在窄视口下被同步收窄，且不会跌破下限', () => {
    expect(rightWidthRange(1920)).toEqual({ min: C.rightMin, max: C.rightMax })
    expect(rightWidthRange(1100)).toEqual({ min: C.rightMin, max: C.rightMin })
  })
})
