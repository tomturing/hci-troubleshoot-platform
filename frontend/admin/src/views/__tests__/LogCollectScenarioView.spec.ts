/**
 * 日志收集场景字典维护页（管理台）契约测试。
 *
 * 这个页面唯一的业务价值是「改文案，不改能力」：
 * 1. 列表必须带出停用项与审核状态，否则运营没法把停用的场景重新启用；
 * 2. 短名与释义必填——它们是客户勾选框的两行文字，缺一行就读不懂；
 * 3. 校验不过时**不发请求**，避免把半成品文案写进库。
 */

import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElMessage } from 'element-plus'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  listScenarios: vi.fn(),
  createScenario: vi.fn(),
  updateScenario: vi.fn(),
  toggleScenario: vi.fn(),
}))

// 只替换四个网络调用，保留类型与常量（文案、校验正则都用的真实实现）
vi.mock('@/api/logCollectScenario', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/logCollectScenario')>()
  return { ...actual, ...api }
})

const LogCollectScenarioView = (await import('../LogCollectScenarioView.vue')).default

const rows = [
  {
    value: 'other',
    label: '不收集上述场景日志',
    summary: '只收基础系统日志',
    collects: '基础系统日志',
    typical_symptoms: '尚未定位方向',
    duration_hint: null,
    exclusive: true,
    sort_order: 10,
    enabled: true,
    review_state: 'pending_review' as const,
    updated_by: null,
    updated_at: '2026-10-29T10:00:00Z',
  },
  {
    value: 'vm_suspend',
    label: '虚拟机挂起专项',
    summary: '挂起失败后的专项日志',
    collects: 'VMS 进程日志',
    typical_symptoms: '虚拟机卡在挂起中',
    duration_hint: '单天约 4 分钟',
    exclusive: false,
    sort_order: 20,
    enabled: false,
    review_state: 'approved' as const,
    updated_by: 'ops',
    updated_at: '2026-10-28T10:00:00Z',
  },
]

async function mountView() {
  const wrapper = mount(LogCollectScenarioView, { global: { plugins: [ElementPlus] } })
  await flushPromises()
  return wrapper
}

function buttonByText(wrapper: ReturnType<typeof mount>, text: string) {
  const button = wrapper.findAll('button').find((item) => item.text().includes(text))
  if (!button) throw new Error(`未找到按钮: ${text}`)
  return button
}

/** 表格里的短名/释义是输入框，取 value 而不是 textContent。 */
function inputValues(wrapper: ReturnType<typeof mount>): string[] {
  return wrapper.findAll('input').map((input) => (input.element as HTMLInputElement).value)
}

describe('LogCollectScenarioView', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    api.listScenarios.mockReset()
    api.updateScenario.mockReset()
    api.toggleScenario.mockReset()
    api.createScenario.mockReset()
    api.listScenarios.mockResolvedValue(rows)
    api.updateScenario.mockImplementation(async (value: string, patch: any) => ({
      ...rows.find((row) => row.value === value),
      ...patch,
    }))
    api.toggleScenario.mockResolvedValue({ ...rows[0], enabled: false })
    api.createScenario.mockResolvedValue({ ...rows[0], value: 'network' })
    // ElMessage 会往 body 挂节点，断言 wrapper.text() 拿不到；这里改为拦截调用
    vi.spyOn(ElMessage, 'warning').mockImplementation((() => undefined) as any)
    vi.spyOn(ElMessage, 'success').mockImplementation((() => undefined) as any)
    vi.spyOn(ElMessage, 'error').mockImplementation((() => undefined) as any)
  })

  it('加载后展示全部字典项（含停用与待审核）', async () => {
    const wrapper = await mountView()

    // 短名与释义直接在表格输入框里编辑，所以断言的是 input value
    const values = inputValues(wrapper)
    expect(values).toContain('不收集上述场景日志')
    expect(values).toContain('只收基础系统日志')
    expect(values).toContain('虚拟机挂起专项')

    const text = wrapper.text()
    expect(text).toContain('other')
    expect(text).toContain('待审核')
    expect(text).toContain('已审核')
    // 概览行：运营一眼能看到「有多少条还没审核」
    expect(text).toContain('共 2 条')
    expect(text).toContain('启用 1 条')
    expect(text).toContain('待审核 1 条')
  })

  it('切换启用开关只提交 enabled 一个字段（不覆盖别人的文案）', async () => {
    const wrapper = await mountView()

    // 列顺序：互斥开关在前，启用开关在后
    await wrapper.findAll('.el-switch')[1].trigger('click')
    await flushPromises()

    expect(api.toggleScenario).toHaveBeenCalledWith('other', false)
    // 不能退化成全量更新，否则会把别人正在编辑的文案一起覆盖掉
    expect(api.updateScenario).not.toHaveBeenCalled()
  })

  it('编辑弹窗保存成功：只提交改动字段', async () => {
    const wrapper = await mountView()

    await buttonByText(wrapper, '编辑说明').trigger('click')
    await flushPromises()

    // 弹窗输入顺序：0 枚举值（禁用）1 短名 2 释义 3（占位）4 排序
    const dialogInputs = wrapper.findAll('.el-dialog .el-input__inner')
    expect((dialogInputs[0].element as HTMLInputElement).disabled).toBe(true)
    await dialogInputs[2].setValue('只收系统基础日志（已澄清）')

    await buttonByText(wrapper, '保存').trigger('click')
    await flushPromises()

    expect(api.updateScenario).toHaveBeenCalledTimes(1)
    const [value, patch] = api.updateScenario.mock.calls[0]
    expect(value).toBe('other')
    expect(patch.summary).toBe('只收系统基础日志（已澄清）')
    expect(patch.label).toBeUndefined()
  })

  it('释义为空时拦住保存，不写库', async () => {
    const wrapper = await mountView()

    await buttonByText(wrapper, '新增场景').trigger('click')
    await flushPromises()

    const dialogInputs = wrapper.findAll('.el-dialog .el-input__inner')
    await dialogInputs[0].setValue('network')
    await dialogInputs[1].setValue('网络专项')
    // 释义留空

    await buttonByText(wrapper, '保存').trigger('click')
    await flushPromises()

    expect(api.createScenario).not.toHaveBeenCalled()
    expect(ElMessage.warning).toHaveBeenCalledWith(expect.stringContaining('一句话释义必填'))
  })

  it('枚举值不符合 acli 形状时不发请求', async () => {
    const wrapper = await mountView()

    await buttonByText(wrapper, '新增场景').trigger('click')
    await flushPromises()

    const dialogInputs = wrapper.findAll('.el-dialog .el-input__inner')
    await dialogInputs[0].setValue('Network-1')
    await dialogInputs[1].setValue('网络专项')
    await dialogInputs[2].setValue('网络类故障日志')

    await buttonByText(wrapper, '保存').trigger('click')
    await flushPromises()

    expect(api.createScenario).not.toHaveBeenCalled()
    expect(ElMessage.warning).toHaveBeenCalledWith(expect.stringContaining('枚举值需为小写字母开头'))
  })
})
