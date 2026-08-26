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
  beforeEach(() => { vi.restoreAllMocks(); vi.stubGlobal('fetch', vi.fn()) })

  it('读取资产、基线与调用链', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(response({ assets: [{ id: '1', asset_key: 'qkv_task.template', asset_type: 'template', signal_type: 'qkv_task', revision: 1, status: 'published', content: { stdout_template: 'x' }, category_baseline: { revision: '1.0' }, catalog_baseline: { revision: '1.0' }, content_digest: 'sha256:asset', created_by: 'seed', trace_id: 'migration-000006', updated_at: '2026-08-27T00:00:00Z' }] }))
    const wrapper = mount(BundleFixtureAssetsView, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    expect(String(vi.mocked(fetch).mock.calls[0][0])).toContain('/v1/control-plane/fixture-assets')
    expect(wrapper.text()).toContain('qkv_task.template')
    await wrapper.find('tbody tr').trigger('click')
    expect(wrapper.text()).toContain('migration-000006')
    wrapper.unmount()
  })
})
