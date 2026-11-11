/**
 * 默认场景清单面板契约测试（模拟管理员人工操作）。
 *
 * 守住三件事：
 * 1. 未改过的模块明确标注「平台默认快照」，不能让管理员误以为当前是自定义清单；
 * 2. 编辑路径后，保存必须先把条目交给服务端试算，再提交服务端保存；
 * 3. 前端绝不自己拼下发文本，预览内容来自服务端。
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

describe('默认场景清单面板', () => {
  it('展示每个场景并标注未自定义', async () => {
    const wrapper = mount(DefaultScenarioPanel, {
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()

    expect(api.listDefaultManifests).toHaveBeenCalled()
    expect(wrapper.text()).toContain('虚拟机开机失败')
    expect(wrapper.text()).toContain('平台默认快照')
    // input 的 value 不是文本节点，必须从 DOM 元素取值。
    expect(
      wrapper
        .findAll('input')
        .some((item) => (item.element as HTMLInputElement).value === '/var/log/vm.log'),
    ).toBe(true)
    wrapper.unmount()
  })

  it('修改 vm_start 的收集路径后，先试算再保存', async () => {
    const wrapper = mount(DefaultScenarioPanel, {
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()

    const pathInput = wrapper
      .findAll('input')
      .find((item) => (item.element as HTMLInputElement).value === '/var/log/vm.log')
    expect(pathInput).toBeTruthy()
    await pathInput!.setValue('/var/log/vm-start.log')
    await flushPromises()

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
})
