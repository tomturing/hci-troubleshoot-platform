import { mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { nextTick } from 'vue'
import { describe, expect, it, vi } from 'vitest'

import SignalDryRunDialog from '../SignalDryRunDialog.vue'

const stubs = {
  'el-dialog': { template: '<section class="dialog-stub"><slot name="header" /><slot /><slot name="footer" /></section>' },
  'el-icon': { template: '<span><slot /></span>' },
  'el-form': { template: '<form><slot /></form>' },
  'el-form-item': { template: '<label><slot /></label>' },
  'el-radio-group': { template: '<div><slot /></div>' },
  'el-radio-button': { template: '<button :disabled="disabled"><slot /></button>', props: ['disabled'] },
  'el-select': { template: '<select><slot /></select>' },
  'el-option': true,
  'el-input': { template: '<textarea />' },
  'el-alert': { template: '<div><slot />{{ title }}</div>', props: ['title'] },
  'el-tag': { template: '<span><slot /></span>' },
  'el-button': { template: '<button :disabled="disabled"><slot /></button>', props: ['disabled'] },
  'el-tooltip': { template: '<span><slot /></span>' },
}

const signal = {
  id: 'sig_003',
  acquire: { tool: 'qfk_system', args: { instruction: '检查主机系统时间' } },
  match: { type: 'threshold', extract: { ai_extract: { instruction: '计算时间差' } } },
  orchestrate: {},
}

describe('SignalDryRunDialog', () => {
  it('显示已绑定的 KBD、Signal 和 QFK 文本输入，不提供编辑位置选择', () => {
    const wrapper = mount(SignalDryRunDialog, {
      props: { modelValue: true, supportId: '41398', kbdRevision: 7, signal, signalIndex: 2 },
      global: { plugins: [ElementPlus], stubs },
    })

    expect(wrapper.text()).toContain('试运行 · sig_003 检查主机系统时间')
    expect(wrapper.text()).toContain('已绑定 KBD 41398 / rev.7 · qfk_system')
    expect(wrapper.text()).toContain('运行结果')
    expect(wrapper.find('.sample-input').exists()).toBe(true)
    const dryRunButton = wrapper.findAll('button').find((item) => item.text() === '试运行')
    expect(dryRunButton?.exists()).toBe(true)
    expect(wrapper.text()).not.toContain('解析预览')
    expect(wrapper.text()).not.toContain('编辑位置')
  })

  it('没有 AI 处理时禁用当前 AI 处理范围', () => {
    const wrapper = mount(SignalDryRunDialog, {
      props: {
        modelValue: true,
        supportId: '41398',
        signal: { id: 'sig_001', acquire: { tool: 'qfk_system', args: {} }, match: {}, orchestrate: {} },
      },
      global: { plugins: [ElementPlus], stubs },
    })

    const aiButton = wrapper.findAll('button').find((item) => item.text().includes('当前 AI 处理'))
    expect(aiButton?.attributes('disabled')).toBeDefined()
  })

  it('结果区域只展示单一终态并可展开 AI 原始响应', async () => {
    const wrapper = mount(SignalDryRunDialog, {
      props: { modelValue: true, supportId: '41398', kbdRevision: 7, signal, signalIndex: 2 },
      global: { plugins: [ElementPlus], stubs },
    })

    expect(wrapper.text()).not.toContain('已完成')
    expect(wrapper.text()).not.toContain('服务未返回结果')

    const vm = wrapper.vm as unknown as { previewResult: Record<string, unknown> }
    vm.previewResult = {
      trace_id: 'a'.repeat(32), dataset_id: 'sample', config_revision: 'sha256:test', status: 'PASS',
      value: true, evidence: '最大时间差为 8 秒，超过 2 秒阈值。', evidence_lines: [2, 3, 4],
      ai_raw_response: { status: 'success', output: 1, evidence: [{ ref: 'line:2', quote: '...' }], reason: '超过阈值' },
    }
    await nextTick()

    expect(wrapper.find('.result-conclusion strong').text()).toBe('PASS')
    expect(wrapper.find('.result-context dd code').text()).toBe('true')
    expect(wrapper.find('.raw-response pre').text()).toContain('"status": "success"')
  })

  it('支持从已发布 Bundle 资产载入编辑并流转按钮状态机', async () => {
    const wrapper = mount(SignalDryRunDialog, {
      props: { modelValue: true, supportId: '41398', kbdRevision: 7, signal, signalIndex: 2 },
      global: { plugins: [ElementPlus], stubs },
    })

    const vm = wrapper.vm as unknown as {
      source: string
      datasets: Array<{ dataset_id: string; source_type: string; source_ref: string; payload: unknown }>
      selectedDatasetId: string
      isEditingFork: boolean
      previewResult: Record<string, unknown> | null
    }

    // 1. 先切换到 fixture 来源并等待 watch 处理完成
    vm.source = 'fixture'
    await nextTick()

    // 2. 注入模拟数据集与选中项
    vm.datasets = [{ dataset_id: 'ds-1', source_type: 'fixture', source_ref: 'route-stdout', payload: 'Wed Aug 26 11:00:24 CST 2026' }]
    vm.selectedDatasetId = 'ds-1'
    await nextTick()

    // 3. 初始状态：展示「创建新 Bundle 草稿」按钮
    const forkButton = wrapper.findAll('button').find((item) => item.text().includes('创建新 Bundle 草稿'))
    expect(forkButton?.exists()).toBe(true)

    // 4. 点击「创建新 Bundle 草稿」进入编辑模式
    await forkButton?.trigger('click')
    await nextTick()

    expect(vm.isEditingFork).toBe(true)
    expect(wrapper.find('.fork-edit-banner').exists()).toBe(true)

    // 5. 按钮变为置灰的「保存到 Bundle 草稿」
    const saveButtonBeforePass = wrapper.findAll('button').find((item) => item.text().includes('保存到 Bundle 草稿'))
    expect(saveButtonBeforePass?.exists()).toBe(true)
    expect(saveButtonBeforePass?.attributes('disabled')).toBeDefined()

    // 6. 试运行 PASS 后变为可用状态
    vm.previewResult = {
      trace_id: 't-123', dataset_id: 'ds-1', config_revision: 'sha256:test', status: 'PASS',
      value: true,
    }
    await nextTick()

    const saveButtonAfterPass = wrapper.findAll('button').find((item) => item.text().includes('保存到 Bundle 草稿'))
    expect(saveButtonAfterPass?.attributes('disabled')).toBeUndefined()
  })

  it('不根据 AI output 值推导或改写最终结论', async () => {
    const wrapper = mount(SignalDryRunDialog, {
      props: { modelValue: true, supportId: '41398', kbdRevision: 7, signal, signalIndex: 2 },
      global: { plugins: [ElementPlus], stubs },
    })
    const vm = wrapper.vm as unknown as { previewResult: Record<string, unknown> }
    vm.previewResult = {
      trace_id: 'b'.repeat(32), dataset_id: 'sample', config_revision: 'sha256:test', status: 'PASS',
      value: 'degraded', evidence: '命中业务规则', evidence_lines: [2],
    }
    await nextTick()
    expect(wrapper.find('.result-conclusion strong').text()).toBe('PASS')
    expect(wrapper.find('.result-context dd code').text()).toBe('degraded')
  })

  it('保存草稿时过滤并创建匹配当前信号路由的 Draft', async () => {
    const wrapper = mount(SignalDryRunDialog, {
      props: { modelValue: true, supportId: '41446', kbdRevision: 23, signal: { ...signal, id: 'expert_new_001' }, signalIndex: 1 },
      global: { plugins: [ElementPlus], stubs },
    })
    const vm = wrapper.vm as unknown as {
      previewResult: Record<string, unknown>
      lastDryRunRequest: Record<string, unknown>
      saveToBundle: () => Promise<void>
    }
    vm.previewResult = {
      trace_id: 't-999', dataset_id: 'sample', config_revision: 'sha256:test', status: 'PASS',
      preview_token: 'token.sig',
    }
    vm.lastDryRunRequest = { support_id: '41446', kbd_revision: 23, unit_ref: { signal_id: 'expert_new_001' } }

    const fetchMock = vi.fn().mockImplementation((url: string, opts?: { method?: string; body?: string }) => {
      if (url.includes('/bundles?support_id=41446')) {
        // 返回一个包含旧信号 sig_001 的已存在草稿
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({
            bundles: [
              { digest: 'sha256:old_draft', status: 'draft', kbd_revision: 23, route_sources: [{ signal_id: 'sig_001' }] }
            ]
          }),
        })
      }
      if (url === '/api/hci-sim/v1/control-plane/bundles' && opts?.method === 'POST') {
        // 创建新 Draft
        const parsedBody = JSON.parse(opts.body || '{}')
        expect(parsedBody.kbd_revision).toBe(23)
        return Promise.resolve({
          ok: true,
          status: 201,
          json: () => Promise.resolve({
            bundle: { digest: 'sha256:new_draft_rev23', status: 'draft', kbd_revision: 23 }
          }),
        })
      }
      if (url.includes('/dry-run/bundles/sha256%3Anew_draft_rev23') || url.includes('/dry-run/bundles/sha256:new_draft_rev23')) {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({
            bundle: { digest: 'sha256:new_draft_rev23', status: 'draft' }
          }),
        })
      }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) })
    })
    vi.stubGlobal('fetch', fetchMock)

    await vm.saveToBundle()
    expect(fetchMock).toHaveBeenCalledWith('/api/hci-sim/v1/control-plane/bundles', expect.objectContaining({ method: 'POST' }))
    vi.unstubAllGlobals()
  })

  it('【模式一：先发布再测试】存在匹配当前信号路由的 Draft 时直接复用，不重复创建 Draft', async () => {
    const wrapper = mount(SignalDryRunDialog, {
      props: { modelValue: true, supportId: '27123', kbdRevision: 9, signal: { ...signal, id: 'sig_001' }, signalIndex: 0 },
      global: { plugins: [ElementPlus], stubs },
    })
    const vm = wrapper.vm as unknown as {
      previewResult: Record<string, unknown>
      lastDryRunRequest: Record<string, unknown>
      saveToBundle: () => Promise<void>
    }
    vm.previewResult = {
      trace_id: 't-mode1', dataset_id: 'sample', config_revision: 'sha256:test', status: 'PASS',
      preview_token: 'token.mode1',
    }
    vm.lastDryRunRequest = { support_id: '27123', kbd_revision: 9, unit_ref: { signal_id: 'sig_001' } }

    const fetchMock = vi.fn().mockImplementation((url: string, opts?: { method?: string; body?: string }) => {
      if (url.includes('/bundles?support_id=27123')) {
        // 返回一个匹配的已有 Draft
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({
            bundles: [
              { digest: 'sha256:existing_draft_rev9', status: 'draft', kbd_revision: 9, route_sources: [{ signal_id: 'sig_001' }] }
            ]
          }),
        })
      }
      if (url.includes('/dry-run/bundles/sha256%3Aexisting_draft_rev9') || url.includes('/dry-run/bundles/sha256:existing_draft_rev9')) {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({
            bundle: { digest: 'sha256:existing_draft_rev9', status: 'draft' }
          }),
        })
      }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) })
    })
    vi.stubGlobal('fetch', fetchMock)

    await vm.saveToBundle()
    // 验证没有调用 POST /api/hci-sim/v1/control-plane/bundles（即没有创建新 Draft，而是直接复用）
    expect(fetchMock).not.toHaveBeenCalledWith('/api/hci-sim/v1/control-plane/bundles', expect.objectContaining({ method: 'POST' }))
    // 验证直接调用了向已有 Draft 写入验证资产的接口
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('sha256%3Aexisting_draft_rev9'),
      expect.objectContaining({ method: 'POST' })
    )
    vi.unstubAllGlobals()
  })
})


