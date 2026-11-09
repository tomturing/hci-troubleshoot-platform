/**
 * 自定义场景面板契约测试。
 *
 * 这个面板真正要守的不是"表格能渲染"，而是两条安全边界：
 * 1. 非法路径（空格/通配符/相对路径）必须在进入列表前被拒绝 —— 它们会被
 *    插件直接拼进 `cp -r`，属于命令注入面；
 * 2. 保存前必须调用服务端试算，前端不能自己拍脑袋认为"这份配置没问题"。
 */

import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  listCustomScenarios: vi.fn(),
  createCustomScenario: vi.fn(),
  updateCustomScenario: vi.fn(),
  deleteCustomScenario: vi.fn(),
  validateCustomScenario: vi.fn(),
}))

vi.mock('@/api/logCollectCustomScenario', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/logCollectCustomScenario')>()
  return { ...actual, ...api }
})

const CustomScenarioPanel = (await import('../components/CustomScenarioPanel.vue')).default

const row = {
  scenario_id: '0d0f5a48-1111-4222-8333-944445555666',
  revision: 3,
  code: 'cs_storage_full',
  label: '存储链路完整取证',
  summary: '定位卡慢盘',
  applies_to: null,
  items: [{ path: '/sf/log/cluster/', category: 'log' as const, note: null }],
  item_count: 1,
  config_sha256: 'a'.repeat(64),
  config_bytes: 120,
  source: 'visual' as const,
  enabled: true,
  sort_order: 10,
  review_state: 'approved' as const,
  created_by: 'ops',
  updated_by: 'ops',
  created_at: '2026-11-09T10:00:00Z',
  updated_at: '2026-11-09T10:00:00Z',
}

async function mountPanel() {
  const wrapper = mount(CustomScenarioPanel, { global: { plugins: [ElementPlus] } })
  await flushPromises()
  return wrapper
}

function buttonByText(wrapper: ReturnType<typeof mount>, text: string) {
  const button = wrapper.findAll('button').find((item) => item.text().includes(text))
  if (!button) throw new Error(`未找到按钮: ${text}`)
  return button
}

describe('CustomScenarioPanel', () => {
  beforeEach(() => {
    api.listCustomScenarios.mockReset()
    api.createCustomScenario.mockReset()
    api.validateCustomScenario.mockReset()
    api.listCustomScenarios.mockResolvedValue([row])
  })

  it('列表展示名称、路径数与版本', async () => {
    const wrapper = await mountPanel()
    const text = wrapper.text()
    expect(text).toContain('存储链路完整取证')
    expect(text).toContain('cs_storage_full')
    expect(text).toContain('v3')
    wrapper.unmount()
  })

  it('批量添加时拒绝含空格与通配符的路径', async () => {
    const wrapper = await mountPanel()
    await buttonByText(wrapper, '新增自定义场景').trigger('click')
    await flushPromises()

    const textarea = wrapper.find('textarea')
    expect(textarea.exists()).toBe(true)
    await textarea.setValue('/sf/good/\n/sf/bad path\n/sf/bad*')

    await buttonByText(wrapper, '批量添加').trigger('click')
    await flushPromises()

    // 只有合法路径进入列表；两条非法项合并成一次提示。
    // 断言下发预览而不是整页文本：错误提示里本来就会出现被拒绝的原文。
    expect(wrapper.text()).toContain('已忽略 2 条')
    expect(wrapper.text()).toContain('{"paths":"/sf/good/"}')
    expect(wrapper.text()).toContain('收集路径（1 条）')
    wrapper.unmount()
  })

  it('名称为空时不发起保存请求', async () => {
    const wrapper = await mountPanel()
    await buttonByText(wrapper, '新增自定义场景').trigger('click')
    await flushPromises()

    await buttonByText(wrapper, '保存').trigger('click')
    await flushPromises()

    expect(api.validateCustomScenario).not.toHaveBeenCalled()
    expect(api.createCustomScenario).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})
