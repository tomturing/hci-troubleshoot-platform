import { mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { describe, expect, it } from 'vitest'

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
    expect(wrapper.text()).toContain('完整 stdout / stderr')
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

  it('有服务结果时不再显示空状态感叹号和未返回提示', async () => {
    const wrapper = mount(SignalDryRunDialog, {
      props: { modelValue: true, supportId: '41398', kbdRevision: 7, signal, signalIndex: 2 },
      global: { plugins: [ElementPlus], stubs },
    })
    // 通过组件实例注入结果，专门验证结果/空状态的互斥渲染契约。
    const vm = wrapper.vm as unknown as { previewRequested: boolean; previewResult: Record<string, unknown> }
    vm.previewRequested = true
    vm.previewResult = { status: 'PASS', value: true, evidence: '已完成处理' }
    await wrapper.vm.$nextTick()
    expect(wrapper.text()).toContain('PASS')
    expect(wrapper.text()).toContain('已完成处理')
    expect(wrapper.text()).not.toContain('服务未返回结果')
    expect(wrapper.find('.preview-empty').exists()).toBe(false)
  })

  it('支持在表单展示与 JSON 展示之间切换', async () => {
    const wrapper = mount(SignalDryRunDialog, {
      props: { modelValue: true, supportId: '41398', kbdRevision: 7, signal, signalIndex: 2 },
      global: { plugins: [ElementPlus], stubs },
    })
    const vm = wrapper.vm as unknown as {
      previewRequested: boolean
      previewResult: Record<string, unknown>
      resultViewMode: 'form' | 'json'
    }
    vm.previewRequested = true
    vm.previewResult = { status: 'PASS', value: { diff: 12, ok: true }, evidence: '说明' }
    await wrapper.vm.$nextTick()

    expect(wrapper.find('.result-form').exists()).toBe(true)
    expect(wrapper.find('.result-value').exists()).toBe(false)
    expect(wrapper.text()).toContain('diff')

    vm.resultViewMode = 'json'
    await wrapper.vm.$nextTick()
    expect(wrapper.find('.result-form').exists()).toBe(false)
    expect(wrapper.find('.result-value').exists()).toBe(true)
    expect(wrapper.find('.result-value').text()).toContain('"diff": 12')
  })
})
