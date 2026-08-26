import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import OutputProcessingEditor from '../OutputProcessingEditor.vue'

const stubs = {
  'el-button': { template: '<button @click="$emit(\'click\')"><slot /></button>' },
  'el-alert': { template: '<div class="alert">{{ title }}</div>', props: ['title'] },
  'el-empty': true,
  'el-icon': { template: '<span><slot /></span>' },
  'el-input': { template: '<input :value="modelValue" @input="$emit(\'input\', $event.target.value)" />', props: ['modelValue'] },
  'el-select': { template: '<select :value="modelValue" @change="$emit(\'change\', $event.target.value)"><slot /></select>', props: ['modelValue'] },
  'el-option': { template: '<option :value="value">{{ label }}</option>', props: ['label', 'value'] },
  'el-form': { template: '<form><slot /></form>' },
  'el-form-item': { template: '<label><slot /></label>' },
  'el-radio-group': true,
  'el-radio-button': true,
  'el-switch': true,
  'el-tooltip': true,
  MatcherEditor: { template: '<div class="matcher-editor" />', props: ['modelValue'] },
}

function mountEditor(modelValue: Record<string, any>[] = [], produces = [{ name: 'DESCRIPTION', path: 'description' }]) {
  return mount(OutputProcessingEditor, { props: { modelValue, produces }, global: { stubs } })
}

describe('OutputProcessingEditor', () => {
  it('使用产出变量处理标题并删除旧说明', () => {
    const wrapper = mountEditor()
    expect(wrapper.text()).toContain('产出变量处理')
    expect(wrapper.text()).toContain('可选：对 QKV 产出变量进一步处理，包括派生变量和断言判断。')
    expect(wrapper.text()).not.toContain('输出后处理')
    expect(wrapper.text()).not.toContain('沿用 QFK 的处理单元')
    expect(wrapper.text()).not.toContain('QKV 已从 JSON 路径取得具体值')
  })

  it('变量处理标题提供绑定 Signal 的试运行入口', () => {
    const wrapper = mountEditor()

    expect(wrapper.find('.processing-header-actions').text()).toContain('试运行')
  })

  it('使用“分隔”术语并展示真实边界符号示例', async () => {
    const wrapper = mountEditor([{ mode: 'derive', input: '{{DESCRIPTION}}', name: 'VM_NAME', type: 'string', extract: { type: 'split', separator: '：' } }])
    expect(wrapper.text()).toContain('分隔')
    expect(wrapper.text()).not.toContain('分割')
    expect(wrapper.findAll('input').find((input) => input.attributes('placeholder')?.startsWith('例如：'))?.attributes('placeholder')).toBe('例如：（）、<>、【】、：')
  })

  it('新增派生变量符合三列布局契约', async () => {
    const wrapper = mountEditor()
    await wrapper.findAll('button').find((button) => button.text() === '添加处理')!.trigger('click')
    const emitted = wrapper.emitted('update:modelValue')
    expect((emitted?.at(-1)?.[0] as Array<Record<string, unknown>>)[0]).toMatchObject({
      mode: 'derive', input: '{{DESCRIPTION}}', name: '', type: 'string',
      extract: { type: 'feature', feature: 'vm_name', cardinality: 'exactly_one' },
    })
    await wrapper.setProps({ modelValue: (emitted?.at(-1)?.[0] as Array<Record<string, unknown>>) })
    expect(wrapper.text()).toContain('输入变量')
    expect(wrapper.text()).toContain('输出变量')
    expect(wrapper.text()).toContain('提取方式')
  })

  it('AI 提取入口保存 QFK 兼容的 instruction', async () => {
    const wrapper = mountEditor([{ mode: 'derive', input: '{{DESCRIPTION}}', name: 'VM_NAME', type: 'string', extract: { type: 'feature', feature: 'vm_name' } }])
    ;(wrapper.vm as any).setExtractType(0, 'ai')
    const latest = wrapper.emitted('update:modelValue')?.at(-1)?.[0] as Array<Record<string, any>>
    expect(latest[0].extract).toMatchObject({ type: 'feature', ai_extract: { instruction: '' } })
  })

  it('派生变量可以引用前序变量，且不提供后序变量', () => {
    const wrapper = mountEditor([
      { mode: 'derive', input: '{{DESCRIPTION}}', name: 'TEXT', type: 'string', extract: { type: 'feature', feature: 'vm_name' } },
      { mode: 'derive', input: '{{TEXT}}', name: 'NORMALIZED', type: 'string', extract: { type: 'feature', feature: 'vm_name' } },
    ])
    expect((wrapper.vm as any).inputOptionsFor(0)).toEqual(['{{DESCRIPTION}}'])
    expect((wrapper.vm as any).inputOptionsFor(1)).toEqual(['{{DESCRIPTION}}', '{{TEXT}}'])
  })

  it('断言直接挂载 QFK Matcher，不引入 compare 字段', () => {
    const wrapper = mountEditor([{ mode: 'assert', input: '{{PERCENT_CURRENT}}', match: { type: 'threshold', expected: true, operator: '>', value: 90 } }])
    expect(wrapper.find('.matcher-editor').exists()).toBe(true)
    expect(wrapper.text()).toContain('判断类型及要求')
    expect((wrapper.props('modelValue') as any)[0]).not.toHaveProperty('compare')
  })
})
