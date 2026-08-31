import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import BundleFixtureAssetsView from '../BundleFixtureAssetsView.vue'

const router = { push: vi.fn() }
vi.mock('vue-router', () => ({ useRouter: () => router }))

function response(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

describe('BundleFixtureAssetsView', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    vi.stubGlobal('fetch', vi.fn())
  })

  it('读取资产、基线与调用链并在详情中展示结构化 Bindings', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      response({
        assets: [
          {
            id: '1',
            asset_key: 'qkv_task.instance.delete_vm',
            asset_type: 'instance',
            signal_type: 'qkv_task',
            revision: 1,
            status: 'published',
            content: {
              selection: { keyword: '删除虚拟机', default: false },
              bindings: { TYPE: '删除虚拟机', DESCRIPTION: '创建回收站目录失败', PROCESS: '完成', REQUEST_ID: ',a3a9e0350ab8' },
            },
            category_baseline: { revision: '1.0' },
            catalog_baseline: { revision: '1.0' },
            content_digest: 'sha256:asset',
            created_by: 'seed',
            trace_id: 'migration-000007',
            updated_at: '2026-08-31T00:00:00Z',
          },
        ],
      })
    )
    const wrapper = mount(BundleFixtureAssetsView, { global: { plugins: [ElementPlus] } })
    await flushPromises()

    expect(String(vi.mocked(fetch).mock.calls[0][0])).toContain('/v1/control-plane/fixture-assets')
    expect(wrapper.text()).toContain('qkv_task.instance.delete_vm')
    expect(wrapper.text()).toContain('删除虚拟机')

    await wrapper.find('tbody tr').trigger('click')
    expect(wrapper.text()).toContain('migration-000007')
    expect(wrapper.text()).toContain('创建回收站目录失败')
    expect(wrapper.text()).toContain(',a3a9e0350ab8')
    wrapper.unmount()
  })

  it('新建实例时支持表单编辑向导并可切换至 JSON 模式', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(response({ assets: [] }))
    const wrapper = mount(BundleFixtureAssetsView, { global: { plugins: [ElementPlus] } })
    await flushPromises()

    // 点击新建实例
    const buttons = wrapper.findAll('button')
    const createInstanceBtn = buttons.find((b) => b.text().includes('新建实例'))
    expect(createInstanceBtn).toBeDefined()
    await createInstanceBtn!.trigger('click')
    await flushPromises()

    // 默认处于表单编辑向导
    expect(wrapper.text()).toContain('表单编辑向导')
    expect(wrapper.text()).toContain('业务变量注入 (Bindings)')
    expect(wrapper.text()).toContain('调用链 ID (REQUEST_ID)')

    // 切换至 JSON 显式编辑模式
    const modeGroup = wrapper.findAllComponents({ name: 'ElRadioGroup' })[0]
    await modeGroup.vm.$emit('change', 'json')
    await flushPromises()

    expect(wrapper.text()).toContain('格式化 JSON')
    wrapper.unmount()
  })
})
