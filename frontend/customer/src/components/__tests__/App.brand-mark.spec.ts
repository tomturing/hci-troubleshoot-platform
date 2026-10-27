import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { Search } from '@element-plus/icons-vue'

/**
 * 顶栏品牌图标必须和标题同义。
 *
 * 历史问题：标题是「HCI 故障排查助手」，图标却是 `FirstAidKit`（急救箱：箱体 + 十字）。
 * 急救箱在用户眼里是"医疗 / 救助"，与"排查故障"不是一回事——这是第一眼就能看出的错位，
 * 而两个语义在代码里没有任何约束关联，所以谁也不会在改标题时顺手检查图标。
 *
 * 修法是把"图标 = 标题里的动作"写成断言：品牌区用 Search（放大镜，"把问题找出来"），
 * 且与页面其余 Element Plus 线性图标同族，描边风格天然一致。
 *
 * 期间试过 Tools —— 它在 Element Plus 里实际是一个齿轮（设置 / 配置语义），
 * 放品牌区会被读成"设置入口"，同样对不上标题，所以一并否掉。
 */
const APP = resolve(process.cwd(), 'src/App.vue')

describe('顶栏品牌图标与标题语义对齐', () => {
  const source = readFileSync(APP, 'utf-8')

  it('不再使用急救箱图标（急救 = 救助，不是排查）', () => {
    // 只断言"不再引用"，避免命中注释里说明历史的文字
    expect(source).not.toMatch(/<FirstAidKit\s*\/>/)
    expect(source).not.toMatch(/import\s*\{[^}]*\bFirstAidKit\b[^}]*\}/)
  })

  it('不使用齿轮类图标（齿轮 = 设置，会被读成设置入口）', () => {
    expect(source).not.toMatch(/<Tools\s*\/>/)
    expect(source).not.toMatch(/<SetUp\s*\/>/)
  })

  it('使用图标库真实导出的 Search，而不是一个想当然的名字', () => {
    expect(Search).toBeTruthy()
    expect(source).toMatch(/import\s*\{[^}]*\bSearch\b[^}]*\}\s*from\s*'@element-plus\/icons-vue'/)
  })

  it('图标与标题在同一品牌块内（不是搬到了别处）', () => {
    const brand = source.match(/<div class="header-brand">([\s\S]*?)<\/div>/)?.[1] ?? ''
    expect(brand).toContain('<Search />')
    expect(brand).toContain('HCI 故障排查助手')
  })
})
