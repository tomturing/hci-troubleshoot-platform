import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import SimulationTestView from '../SimulationTestView.vue'

function response(body: unknown, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response
}

describe('SimulationTestView P0 状态机', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.restoreAllMocks()
    vi.stubGlobal('fetch', vi.fn())
    class TestWebSocket {
      static OPEN = 1
      static CONNECTING = 0
      readyState = 1
      onopen: (() => void) | null = null
      onmessage: ((event: { data: string }) => void) | null = null
      onerror: (() => void) | null = null
      onclose: (() => void) | null = null
      send() {}
      close() { this.onclose?.() }
    }
    vi.stubGlobal('WebSocket', TestWebSocket)
  })

  it('环境构建成功后，开始测试会打开工单步骤而不是静默展开日志', async () => {
    const fetchMock = vi.mocked(fetch)
    fetchMock
      .mockResolvedValueOnce(response({ support_id: '27123', requested_revision: 24, runtime_revision: 1, bundle_digest: 'sha256:test', bundle_status: 'published', authority_scope: 'dev_golden', buildable: true, capability_gap: ['kbd_revision_mismatch'] }))
      .mockResolvedValueOnce(response({ test_run_id: 'run-27123', environment_context: { test_run_id: 'run-27123', support_id: '27123', kbd_revision: 1, case_id: '' }, connection: { host: 'hci-sim', port: 2222, username: 'sim', password: 'htp2.test', execution_mode: 'sim-ssh', test_run_id: 'run-27123' } }))

    const wrapper = mount(SimulationTestView, { global: { plugins: [ElementPlus] } })
    await wrapper.get('input').setValue('27123')
    const buttons = wrapper.findAll('button')
    await buttons.find((button) => button.text() === '环境构建')?.trigger('click')
    await flushPromises()
    await buttons.find((button) => button.text() === '开始测试')?.trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('创建仿真测试工单')
    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes('/test-runs'))).toBe(false)
  })

  it('能力预检阻断时不发送环境构建请求，并展示结构化差距', async () => {
    const fetchMock = vi.mocked(fetch)
    fetchMock.mockResolvedValueOnce(response({ support_id: '23821', requested_revision: 2, runtime_revision: 1, bundle_digest: '', bundle_status: 'missing', authority_scope: 'runtime_fixture', buildable: false, capability_gap: ['no_published_immutable_bundle'] }))
    const wrapper = mount(SimulationTestView, { global: { plugins: [ElementPlus] } })
    await wrapper.get('input').setValue('23821')
    await wrapper.findAll('button').find((button) => button.text() === '环境构建')?.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('no_published_immutable_bundle')
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })
})
