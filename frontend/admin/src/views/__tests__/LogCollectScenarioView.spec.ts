/**
 * 默认场景（两级页签）契约测试。
 *
 * 守住四件事：
 * 1. 固定枚举用二级页签平铺，页面不再出现搜索、筛选、新增场景入口；
 * 2. 只渲染当前选中场景的概要，短名/释义必填；
 * 3. 保存概要只提交四个字段，编辑说明只提交差异字段；
 * 4. 有未保存改动时切换场景必须二次确认，确认取消则留在原场景。
 */
import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElMessage, ElMessageBox } from 'element-plus'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  listScenarios: vi.fn(),
  updateScenario: vi.fn(),
}))

vi.mock('@/api/logCollectScenario', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/logCollectScenario')>()
  return { ...actual, ...api }
})

// 三个子组件在本用例里只验证宿主页面的选择与拦截，各自网络调用由独立测试覆盖。
vi.mock('../components/DefaultScenarioPanel.vue', () => ({
  default: { props: ['module'], emits: ['update:dirty'], template: '<div class="manifest-stub" />' },
}))
vi.mock('../components/LogUploadPanel.vue', () => ({ default: { template: '<div />' } }))
vi.mock('../components/CustomScenarioPanel.vue', () => ({ default: { template: '<div />' } }))

const LogCollectScenarioView = (await import('../LogCollectScenarioView.vue')).default

const rows = [
  {
    value: 'other',
    label: '默认收集专项',
    summary: '只收基础系统日志',
    collects: '基础系统日志',
    typical_symptoms: '尚未定位方向',
    duration_hint: null,
    exclusive: true,
    sort_order: 10,
    enabled: true,
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

describe('默认场景（两级页签）', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    api.listScenarios.mockReset()
    api.updateScenario.mockReset()
    api.listScenarios.mockResolvedValue(rows)
    api.updateScenario.mockImplementation(async (value: string, patch: any) => ({
      ...rows.find((row) => row.value === value),
      ...patch,
    }))
    vi.spyOn(ElMessage, 'warning').mockImplementation((() => undefined) as any)
    vi.spyOn(ElMessage, 'success').mockImplementation((() => undefined) as any)
    vi.spyOn(ElMessage, 'error').mockImplementation((() => undefined) as any)
  })

  it('固定场景横向平铺，无搜索、筛选与新增入口', async () => {
    const wrapper = await mountView()
    const text = wrapper.text()

    expect(text).toContain('默认收集专项')
    expect(text).toContain('虚拟机挂起专项')
    expect(text).not.toContain('搜索场景')
    expect(text).not.toContain('只看已停用')
    expect(text).not.toContain('新增场景')
    expect(text).not.toContain('待审核')
    expect(text).not.toContain('最后修改')

    // 当前场景的概要直接出现在最前面
    const values = wrapper
      .findAll('input')
      .map((input) => (input.element as HTMLInputElement).value)
    expect(values).toContain('默认收集专项')
    expect(values).toContain('只收基础系统日志')
    expect(text).toContain('other')
    wrapper.unmount()
  })

  it('保存概要只提交短名、释义、排序与启用四个字段', async () => {
    const wrapper = await mountView()
    const inputs = wrapper.findAll('input')
    const summaryInput = inputs.find(
      (item) => (item.element as HTMLInputElement).value === '只收基础系统日志',
    )
    expect(summaryInput).toBeTruthy()
    await summaryInput!.setValue('只收系统基础日志（已澄清）')
    await flushPromises()

    await buttonByText(wrapper, '保存概要').trigger('click')
    await flushPromises()

    expect(api.updateScenario).toHaveBeenCalledTimes(1)
    const [value, patch] = api.updateScenario.mock.calls[0]
    expect(value).toBe('other')
    expect(patch).toMatchObject({ label: '默认收集专项', summary: '只收系统基础日志（已澄清）' })
    expect(Object.keys(patch).sort()).toEqual(['enabled', 'label', 'sort_order', 'summary'])
    wrapper.unmount()
  })

  it('编辑说明只提交差异字段', async () => {
    const wrapper = await mountView()
    await buttonByText(wrapper, '编辑说明').trigger('click')
    await flushPromises()

    const dialogTextareas = wrapper.findAll('.el-dialog textarea')
    const collects = dialogTextareas.find(
      (item) => (item.element as HTMLTextAreaElement).value === '基础系统日志',
    )
    expect(collects).toBeTruthy()
    await collects!.setValue('基础系统日志（修订）')
    await flushPromises()

    // 页面上还有「保存概要」「保存清单」，必须只点弹窗里的「保存」
    const dialogSave = wrapper
      .findAll('.el-dialog button')
      .find((item) => item.text() === '保存')
    expect(dialogSave).toBeTruthy()
    await dialogSave!.trigger('click')
    await flushPromises()

    expect(api.updateScenario).toHaveBeenCalledTimes(1)
    const [value, patch] = api.updateScenario.mock.calls[0]
    expect(value).toBe('other')
    expect(patch).toEqual({ collects: '基础系统日志（修订）' })
    wrapper.unmount()
  })

  it('有未保存改动时切换场景需确认，取消后留在原场景', async () => {
    const confirm = vi.spyOn(ElMessageBox, 'confirm').mockRejectedValue(new Error('取消'))
    const wrapper = await mountView()

    const inputs = wrapper.findAll('input')
    const labelInput = inputs.find(
      (item) => (item.element as HTMLInputElement).value === '默认收集专项',
    )
    await labelInput!.setValue('默认收集专项（草稿）')
    await flushPromises()

    const secondTab = wrapper
      .findAll('.scenario-subtabs .el-tabs__item')
      .find((item) => item.text().includes('虚拟机挂起专项'))
    expect(secondTab).toBeTruthy()
    await secondTab!.trigger('click')
    await flushPromises()

    expect(confirm).toHaveBeenCalled()
    expect(wrapper.text()).toContain('other')
    expect(wrapper.find('.readonly').text()).toBe('other')
    wrapper.unmount()
  })
})
