import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import BundleFactoryView from '../BundleFactoryView.vue'

function response(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

const manifest = {
  schema_version: '2.0', bundle: { digest: 'sha256:bundle', status: 'published' },
  kbd: { support_id: '27123', revision: 25, checksum: 'sha256:kbd' },
  contracts: { tool_revision: 'tool-r25', policy_revision: 'policy-r1' }, variables: { SYNTHETIC: 'true' },
  routes: [{ id: 'r1', signal_id: 'sig-1', variant: 'positive-minimal', route_key: { tool: 'acli', argv: ['acli', 'system', 'ps'], node: 'SIM-HCI-NODE-01', container: 'host' }, result: { exit_code: 0, stdout: 'old\n', stderr: '' }, fault: { type: 'none' } }],
}
const draft = {
  digest: 'sha256:bundle', status: 'draft', input_fingerprint: 'sha256:input', support_id: '27123', kbd_revision: 25,
  kbd_checksum: 'sha256:kbd', signals_digest: 'sha256:signals', tool_contract_revision: 'tool-r25', policy_revision: 'policy-r1',
  compiler_revision: 'bundle-factory-v1', draft_revision: 0, creator: 'compiler', created_at: '2026-08-18T00:00:00Z', updated_at: '2026-08-18T00:00:00Z', approvals: [], manifest,
}

describe('BundleFactoryView', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    vi.stubGlobal('fetch', vi.fn())
  })

  it('展示冻结事实和仿真 Route，不把 argv 暴露为普通编辑字段', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(response({ bundles: [draft] })).mockResolvedValueOnce(response({ bundle: draft }))
    const wrapper = mount(BundleFactoryView, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    await wrapper.find('tbody tr').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('KBD 27123')
    expect(wrapper.text()).toContain('tool-r25')
    expect(wrapper.text()).toContain('acli system ps')
    expect(wrapper.text()).toContain('编辑 Draft')
  })

  it('专家修改输出时生成新 Draft 并携带修改原因', async () => {
    const revised = { ...draft, digest: 'sha256:revised', draft_revision: 1, parent_bundle_digest: draft.digest }
    vi.mocked(fetch)
      .mockResolvedValueOnce(response({ bundles: [draft] }))
      .mockResolvedValueOnce(response({ bundle: draft }))
      .mockResolvedValueOnce(response({ bundle: revised }, 201))
      .mockResolvedValueOnce(response({ bundles: [revised] }))
      .mockResolvedValueOnce(response({ bundle: revised }))
    const wrapper = mount(BundleFactoryView, { attachTo: document.body, global: { plugins: [ElementPlus] } })
    await flushPromises()
    await wrapper.find('tbody tr').trigger('click')
    await flushPromises()
    await wrapper.findAll('button').find((button) => button.text() === '编辑 Draft')!.trigger('click')
    await flushPromises()
    const reason = document.querySelector('input[placeholder="说明证据或仿真设定的修正依据"]') as HTMLInputElement
    reason.value = '修正专家复核后的进程输出'
    reason.dispatchEvent(new Event('input', { bubbles: true }))
    const stdout = document.querySelector('.editor-body tbody textarea') as HTMLTextAreaElement
    stdout.value = 'corrected\n'
    stdout.dispatchEvent(new Event('input', { bubbles: true }))
    const save = Array.from(document.querySelectorAll('button')).find((button) => button.textContent?.trim() === '生成新 Draft') as HTMLButtonElement
    save.click()
    await flushPromises()

    const reviseCall = vi.mocked(fetch).mock.calls.find(([url]) => String(url).includes('/revise'))
    expect(reviseCall).toBeTruthy()
    const payload = JSON.parse(String((reviseCall![1] as RequestInit).body))
    expect(payload.reason).toBe('修正专家复核后的进程输出')
    expect(payload.manifest.routes[0].result.stdout).toBe('corrected\n')
    wrapper.unmount()
  })
})
