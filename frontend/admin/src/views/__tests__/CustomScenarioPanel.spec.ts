/**
 * 自定义场景面板契约测试。
 *
 * 这个面板真正要守的不是"表格能渲染"，而是四条边界：
 * 1. 非法路径（空格/相对路径）在进入清单前被拒绝 —— 它们会被插件直接拼进 `cp -r`，
 *    属于命令注入面；
 * 2. **通道决定条目合法性**：spc 允许通配符与 filters，-c 不允许，放进去会被
 *    插件静默跳过（任务成功但少收），必须在编辑器里就拦住；
 * 3. **勾选必须保真**：从清单勾来的 filters/date_placeholders/docker 要原样提交，
 *    否则写出的 spc 清单会丢过滤条件（与 057 迁移踩过的坑同类）；
 * 4. 保存前必须调用服务端试算，前端不自行拼装下发文本。
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
  getSpcCatalog: vi.fn(),
}))

vi.mock('@/api/logCollectCustomScenario', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/logCollectCustomScenario')>()
  return { ...actual, ...api }
})

const CustomScenarioPanel = (await import('../components/CustomScenarioPanel.vue')).default

const spcRow = {
  scenario_id: '0d0f5a48-1111-4222-8333-944445555666',
  revision: 3,
  code: 'cs_storage_full',
  label: '存储链路完整取证',
  summary: '定位卡慢盘',
  applies_to: null,
  items: [{ action: 'copy' as const, path: '/sf/log/cluster/', category: 'log' as const, note: null }],
  item_count: 1,
  config_sha256: 'a'.repeat(64),
  config_bytes: 120,
  source: 'spc' as const,
  enabled: true,
  sort_order: 10,
  review_state: 'approved' as const,
  created_by: 'ops',
  updated_by: 'ops',
  created_at: '2026-11-09T10:00:00Z',
  updated_at: '2026-11-09T10:00:00Z',
}

const configRow = { ...spcRow, scenario_id: 'sc-2', code: 'cs_paths', label: '路径清单', source: 'visual' as const }

const catalogItem = {
  module: 'core',
  group: 'core 文件',
  action: 'copy' as const,
  path: '/sf/log/core/{date}/*.log',
  filters: [{ placeholder: '{date}' }],
  date_placeholders: [{ placeholder: '{date}', format: '%Y-%m-%d' }],
  docker: null,
  interpreter: null,
  has_wildcard: true,
  needs_date_expand: true,
  usable_by_config: false,
}

const catalog = {
  modules: [
    {
      module: 'core',
      label: 'core（core 收集）',
      m_whitelisted: true,
      item_count: 2,
      config_usable_count: 1,
      groups: [
        {
          note: 'core 文件',
          items: [
            { ...catalogItem, path: '/sf/log/core/core.log', filters: [], date_placeholders: [], has_wildcard: false, needs_date_expand: false, usable_by_config: true },
            catalogItem,
          ],
        },
      ],
    },
  ],
  sources: [{ file: 'core.json', sha256: 'b'.repeat(64), items: 2 }],
  total_items: 2,
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

function radioByValue(wrapper: ReturnType<typeof mount>, value: string) {
  const radio = wrapper.findAll('input[type="radio"]').find((item) => (item.element as HTMLInputElement).value === value)
  if (!radio) throw new Error(`未找到单选项: ${value}`)
  return radio
}

/** 等自动试算的 400ms 防抖 + 请求落地。 */
async function settlePreview() {
  await new Promise((resolve) => setTimeout(resolve, 480))
  await flushPromises()
}

describe('CustomScenarioPanel', () => {
  beforeEach(() => {
    api.listCustomScenarios.mockReset()
    api.createCustomScenario.mockReset()
    api.validateCustomScenario.mockReset()
    api.getSpcCatalog.mockReset()
    api.listCustomScenarios.mockResolvedValue([spcRow, configRow])
    api.getSpcCatalog.mockResolvedValue(catalog)
    api.validateCustomScenario.mockResolvedValue({
      ok: true,
      issues: [],
      item_count: 1,
      config_bytes: 80,
      preview_kind: 'spc',
      preview_text: '[{"action":"copy","path":"/sf/log/cluster/"}]',
    })
  })

  it('列表按通道区分展示，不只显示路径数', async () => {
    const wrapper = await mountPanel()
    const text = wrapper.text()
    expect(text).toContain('存储链路完整取证')
    expect(text).toContain('日志清单（spc）')
    expect(text).toContain('-c 路径')
    expect(text).toContain('v3')
    wrapper.unmount()
  })

  it('spc 通道保留通配符，-c 通道拒绝通配符', async () => {
    const wrapper = await mountPanel()
    await buttonByText(wrapper, '新增自定义场景').trigger('click')
    await flushPromises()

    // 默认 spc：通配符是合法条目（插件自己 glob）
    const textarea = wrapper.find('textarea')
    expect(textarea.exists()).toBe(true)
    await textarea.setValue('/sf/log/*.tar.gz')
    await buttonByText(wrapper, '批量添加').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('收集条目（1 条）')

    // 切到 -c：同一路径必须被拒绝
    await radioByValue(wrapper, 'visual').setValue()
    await flushPromises()
    const textareaAfter = wrapper.find('textarea')
    await textareaAfter.setValue('/sf/log/bad path')
    await buttonByText(wrapper, '批量添加').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('已忽略 1 条')
    wrapper.unmount()
  })

  it('从清单勾选的条目保真提交 filters 与 date_placeholders', async () => {
    api.createCustomScenario.mockResolvedValue(spcRow)
    const wrapper = await mountPanel()
    await buttonByText(wrapper, '新增自定义场景').trigger('click')
    await flushPromises()

    await buttonByText(wrapper, '从插件清单勾选').trigger('click')
    await flushPromises()

    const checkboxes = wrapper.findAll('.cs-picker input[type="checkbox"]')
    expect(checkboxes.length).toBe(2)
    // 勾选带 filters / 通配符的那条
    await checkboxes[1].setValue(true)
    await flushPromises()

    const inputs = wrapper.findAll('input[type="text"], input:not([type])')
    const codeInput = inputs.find((item) => (item.element as HTMLInputElement).placeholder?.includes('cs_storage_full'))
    if (!codeInput) throw new Error('未找到场景标识输入框')
    await codeInput.setValue('cs_core_check')

    const labelInput = wrapper.findAll('input').find((item) =>
      (item.element as HTMLInputElement).placeholder?.includes('存储链路完整取证'))
    if (!labelInput) throw new Error('未找到场景名称输入框')
    await labelInput.setValue('core 取证')

    await buttonByText(wrapper, '保存').trigger('click')
    await flushPromises()

    expect(api.createCustomScenario).toHaveBeenCalledTimes(1)
    const payload = api.createCustomScenario.mock.calls[0][0]
    expect(payload.source).toBe('spc')
    expect(payload.items).toHaveLength(1)
    expect(payload.items[0].path).toBe('/sf/log/core/{date}/*.log')
    expect(payload.items[0].filters).toEqual([{ placeholder: '{date}' }])
    expect(payload.items[0].date_placeholders).toEqual([{ placeholder: '{date}', format: '%Y-%m-%d' }])
    expect(payload.items[0].note).toBe('core 文件')
    wrapper.unmount()
  })

  it('-c 通道拒绝勾选 -c 不可用的清单条目', async () => {
    const wrapper = await mountPanel()
    await buttonByText(wrapper, '新增自定义场景').trigger('click')
    await flushPromises()

    await radioByValue(wrapper, 'visual').setValue()
    await flushPromises()

    await buttonByText(wrapper, '从插件清单勾选').trigger('click')
    await flushPromises()

    const checkboxes = wrapper.findAll('.cs-picker input[type="checkbox"]')
    await checkboxes[1].setValue(true)
    await flushPromises()

    expect(wrapper.text()).toContain('收集条目（0 条）')
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

  it('预览文本来自服务端试算而不是前端拼装', async () => {
    const wrapper = await mountPanel()
    await buttonByText(wrapper, '新增自定义场景').trigger('click')
    await flushPromises()

    const codeInput = wrapper.findAll('input').find((item) =>
      (item.element as HTMLInputElement).placeholder?.includes('cs_storage_full'))
    if (!codeInput) throw new Error('未找到场景标识输入框')
    await codeInput.setValue('cs_preview')

    const textarea = wrapper.find('textarea')
    await textarea.setValue('/sf/log/cluster/')
    await buttonByText(wrapper, '批量添加').trigger('click')
    await flushPromises()

    await settlePreview()

    expect(api.validateCustomScenario).toHaveBeenCalled()
    expect(wrapper.text()).toContain('[{"action":"copy","path":"/sf/log/cluster/"}]')
    wrapper.unmount()
  })

  it('第一屏默认平铺清单：库里没有场景也能看到可勾选项', async () => {
    api.listCustomScenarios.mockResolvedValue([])
    const wrapper = await mountPanel()
    expect(api.getSpcCatalog).toHaveBeenCalled()
    expect(wrapper.text()).toContain('插件清单速选')
    expect(wrapper.text()).toContain('/sf/log/core/{date}/*.log')
    expect(wrapper.findAll('.cs-catalog-card .el-table__body tr')).toHaveLength(2)
    wrapper.unmount()
  })

  it('清单支持按路径搜索与模块筛选', async () => {
    const wrapper = await mountPanel()
    await wrapper.find('input[placeholder="按路径 / 分组 / 模块搜索"]').setValue('core.log')
    await flushPromises()
    expect(wrapper.findAll('.cs-catalog-card .el-table__body tr')).toHaveLength(1)
    wrapper.unmount()
  })

  it('主区勾选后 JSON 视图取服务端序列化结果', async () => {
    const wrapper = await mountPanel()
    const boxes = wrapper.findAll('.cs-catalog-card .el-table__body input[type="checkbox"]')
    await boxes[0].setValue(true)
    await flushPromises()
    expect(wrapper.text()).toContain('已选 1 条')

    await buttonByText(wrapper, '查看 JSON（只读）').trigger('click')
    await flushPromises()
    expect(api.validateCustomScenario).toHaveBeenCalledWith(
      expect.objectContaining({ source: 'spc', code: 'draft_preview' }),
    )
    expect(wrapper.text()).toContain('[{"action":"copy","path":"/sf/log/cluster/"}]')
    wrapper.unmount()
  })

  it('带入新增场景时保留 filters 与 date_placeholders', async () => {
    api.createCustomScenario.mockResolvedValue(spcRow)
    const wrapper = await mountPanel()
    const boxes = wrapper.findAll('.cs-catalog-card .el-table__body input[type="checkbox"]')
    // 第二条是带 filters / 通配符的条目
    await boxes[1].setValue(true)
    await flushPromises()
    await buttonByText(wrapper, '带入新增场景').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('收集条目（1 条）')
    await wrapper.findAll('input').find((item) =>
      (item.element as HTMLInputElement).placeholder?.includes('cs_storage_full'))!.setValue('cs_flat_e2e')
    await wrapper.findAll('input').find((item) =>
      (item.element as HTMLInputElement).placeholder?.includes('存储链路完整取证'))!.setValue('平铺清单验收')
    await buttonByText(wrapper, '保存').trigger('click')
    await flushPromises()

    expect(api.createCustomScenario).toHaveBeenCalledTimes(1)
    const payload = api.createCustomScenario.mock.calls[0][0]
    expect(payload.items[0].path).toBe('/sf/log/core/{date}/*.log')
    expect(payload.items[0].filters).toEqual([{ placeholder: '{date}' }])
    expect(payload.items[0].date_placeholders).toEqual([{ placeholder: '{date}', format: '%Y-%m-%d' }])
    wrapper.unmount()
  })

  it('组件卸载后排队中的试算不再发请求（防定时器泄漏）', async () => {
    const wrapper = await mountPanel()
    await buttonByText(wrapper, '新增自定义场景').trigger('click')
    await flushPromises()
    const codeInput = wrapper.findAll('input').find((item) =>
      (item.element as HTMLInputElement).placeholder?.includes('cs_storage_full'))
    expect(codeInput).toBeTruthy()
    await codeInput!.setValue('cs_leak_guard')
    wrapper.unmount()

    api.validateCustomScenario.mockClear()
    await settlePreview()
    expect(api.validateCustomScenario).not.toHaveBeenCalled()
  })
})
