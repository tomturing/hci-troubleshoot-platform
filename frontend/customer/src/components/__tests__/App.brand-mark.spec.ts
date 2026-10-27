import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

/**
 * 顶栏品牌图标必须和标题同义。
 *
 * 历史问题：标题是「HCI 故障排查助手」，图标却换过三版——
 * `FirstAidKit`（急救箱：医疗 / 救助）、`Tools`（在 Element Plus 里实为齿轮：设置 / 配置）、
 * `Search`（放大镜：搜索）。三者描述的都是"人做的事"，而这个产品的主体是「助手」本身，
 * 于是用户直接给出结论：换成一个机器人。
 *
 * Element Plus 没有机器人图标，所以这里内联一支 SVG，并把关键约束写成断言：
 * 1. 品牌区里必须是 SVG 机器人，不能再退回任何图标库图标；
 * 2. 必须 `fill="none"` + `stroke="currentColor"`——一旦套进 `<el-icon>`，
 *    它的 `.el-icon svg { fill: currentColor }` 会把描边图标填成一团实心色块；
 * 3. 形状要有"头 + 两眼 + 嘴"这三笔，否则只是个圆角矩形，不像机器人。
 */
const APP = resolve(process.cwd(), 'src/App.vue')

describe('顶栏品牌图标与标题语义对齐', () => {
  const source = readFileSync(APP, 'utf-8')
  const brand = source.match(/<div class="header-brand">([\s\S]*?)<\/div>/)?.[1] ?? ''

  it('不再使用急救箱 / 齿轮 / 放大镜这些"人做的事"的图标', () => {
    // 只断言"不再引用"，避免命中注释里说明历史的文字
    for (const name of ['FirstAidKit', 'Tools', 'SetUp', 'Search', 'ZoomIn']) {
      expect(source).not.toMatch(new RegExp(`<${name}\\s*/>`))
      expect(source).not.toMatch(new RegExp(`import\\s*\\{[^}]*\\b${name}\\b[^}]*\\}`))
    }
  })

  it('品牌区使用内联机器人 SVG，而不是图标库组件', () => {
    expect(brand).toContain('brand-mark__bot')
    expect(brand).toMatch(/<svg[\s\S]*?viewBox="0 0 24 24"/)
    // el-icon 会把 SVG 填满，机器人图标必须是描边画法
    expect(brand).toMatch(/fill="none"[\s\S]*?stroke="currentColor"/)
  })

  it('机器人形状三件套齐全：头部 + 双眼 + 嘴线', () => {
    expect(brand).toMatch(/<rect[^>]*rx=/)
    expect(brand.match(/M9\.6 10\.6v2\.2M14\.4 10\.6v2\.2/)).toBeTruthy()
    expect(brand).toMatch(/M10\.2 15\.4h3\.6/)
  })

  it('图标与标题仍在同一品牌块内（不是搬到了别处）', () => {
    expect(brand).toContain('HCI 故障排查助手')
  })
})