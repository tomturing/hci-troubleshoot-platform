import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

/**
 * 顶栏四个入口按钮（工单信息 / 关闭工单 / 日志收集 / SSH终端）必须共用 .header-btn。
 *
 * 历史坑：四个按钮曾各自写死底色（半透明白 / 纯白 0.9 / 无样式回落到 el-button 默认白 /
 * 半透明绿），叠在 #409eff→#337ecc 渐变顶栏上肉眼是四种颜色；
 * 而且"日志收集"压根没有样式类定义，纯靠 el-button 默认值，最难被发现。
 */
const APP = resolve(process.cwd(), 'src/App.vue')

describe('顶栏按钮底色一致性', () => {
  const source = readFileSync(APP, 'utf-8')
  const template = source.slice(0, source.indexOf('<style'))

  it('四个入口按钮都挂 header-btn 统一基底类', () => {
    const btnClasses = [...template.matchAll(/class="([^"]*)"/g)].map((m) => m[1])
    const headerBtns = btnClasses.filter((c) => c.split(/\s+/).includes('header-btn'))
    expect(headerBtns).toHaveLength(4)
  })

  it('不再为单个按钮单独指定底色（半透明白 / 绿色 / 白色实底都不允许）', () => {
    expect(source).not.toMatch(/\.(case-info-btn|close-btn|log-collect-btn|terminal-btn)\s*\{[^}]*background/)
    expect(source).not.toMatch(/rgba\(103,\s*194,\s*58/)
  })

  it('统一规则使用不透明底色（半透明叠在渐变上会呈现不同颜色）', () => {
    const rule = source.match(/\.header-btn\s*\{([\s\S]*?)\}/)?.[1] ?? ''
    expect(rule).toMatch(/background:\s*#ffffff\s*!important/)
  })
})
