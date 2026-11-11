/**
 * 默认场景清单面板契约测试（模拟管理员人工操作）。
 *
 * 守住四件事：
 * 1. 模块由父页面的二级页签指定，本组件不再提供模块下拉；
 * 2. 未改过的模块明确标注「平台默认快照」；
 * 3. 编辑路径后上报 dirty，保存必须先把条目交给服务端试算，再提交服务端保存；
 * 4. 切换模块后回到服务端权威清单，不残留上一个场景的草稿。
 */
import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  listDefaultManifests: vi.fn(),
  validateDefaultManifest: vi.fn(),
  saveDefaultManifest: vi.fn(),
  resetDefaultManifest: vi.fn(),
}))

vi.mock('@/api/logCollectDefaultScenario', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/logCollectDefaultScenario')>()
  return { ...actual, ...api }
})

const DefaultScenarioPanel = (await import('../components/DefaultScenarioPanel.vue')).default

const vmStart = {
  module: 'vm_start',
  label: '虚拟机开机失败',
  customized: false,
  revision: 0,
  item_count: 1,
  sha256: 'a'.repeat(64),
  items: [{ action: 'copy', path: '/var/log/vm.log', category: 'log', note: null }],
}

const network = {
  ...vmStart,
  module: 'network',
  label: '网络异常',
  items: [{ action: 'copy', path: '/var/log/network.log', category: 'log', note: null }],
}

beforeEach(() => {
  vi.clearAllMocks()
  api.listDefaultManifests.mockResolvedValue({
    manifests: [vmStart, network],
    sources: [],
    total_items: 2,
  })
  api.validateDefaultManifest.mockResolvedValue({
    ok: true,
    item_count: 1,
    sha256: 'b'.repeat(64),
    bytes: 120,
    preview_text: '[{"action":"copy","path":"/var/log/vm-start.log"}]',
    issues: [],
  })
  api.saveDefaultManifest.mockResolvedValue({
    module: 'vm_start', revision: 1, item_count: 1, sha256: 'b'.repeat(64), bytes: 120,
  })
  api.resetDefaultManifest.mockResolvedValue(undefined)
})

function mountPanel(module = 'vm_start') {
  return mount(DefaultScenarioPanel, {
    props: { module },
    global: { plugins: [ElementPlus] },
  })
}

describe('默认场景清单面板', () => {
  it('按指定模块加载，标注未自定义，且没有模块下拉与说明文案', async () => {
    const wrapper = mountPanel()
    await flushPromises()

    expect(api.listDefaultManifests).toHaveBeenCalled()
    expect(wrapper.text()).toContain('平台默认快照')
    expect(wrapper.text()).not.toContain('默认场景：')
    expect(wrapper.text()).not.toContain('只保存被改过的模块')
    // input 的 value 不是文本节点，必须从 DOM 元素取值。
    expect(
      wrapper
        .findAll('input')
        .some((item) => (item.element as HTMLInputElement).value === '/var/log/vm.log'),
    ).toBe(true)
    wrapper.unmount()
  })

  it('修改路径后上报 dirty，保存时先试算再提交', async () => {
    const wrapper = mountPanel()
    await flushPromises()

    const pathInput = wrapper
      .findAll('input')
      .find((item) => (item.element as HTMLInputElement).value === '/var/log/vm.log')
    expect(pathInput).toBeTruthy()
    await pathInput!.setValue('/var/log/vm-start.log')
    await flushPromises()

    expect(wrapper.emitted('update:dirty')?.flat()).toContain(true)

    const saveButton = wrapper.findAll('button').find((item) => item.text() === '保存清单')
    expect(saveButton).toBeTruthy()
    await saveButton!.trigger('click')
    await flushPromises()

    expect(api.validateDefaultManifest).toHaveBeenCalledWith([
      expect.objectContaining({ path: '/var/log/vm-start.log' }),
    ])
    expect(api.saveDefaultManifest).toHaveBeenCalledWith('vm_start', [
      expect.objectContaining({ path: '/var/log/vm-start.log' }),
    ])
    // 保存成功后重新拉取列表：界面必须回到服务端权威版本，而不是继续显示本地草稿。
    expect(api.listDefaultManifests).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })

  it('切换模块后加载新场景清单，不残留上一场景的草稿', async () => {
    const wrapper = mountPanel()
    await flushPromises()

    await wrapper.setProps({ module: 'network' })
    await flushPromises()

    const values = wrapper
      .findAll('input')
      .map((item) => (item.element as HTMLInputElement).value)
    expect(values).toContain('/var/log/network.log')
    expect(values).not.toContain('/var/log/vm.log')
    expect(wrapper.emitted('update:dirty')?.flat()).toContain(false)
    wrapper.unmount()
  })
})
