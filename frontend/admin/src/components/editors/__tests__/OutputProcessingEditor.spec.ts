import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import OutputProcessingEditor from '../OutputProcessingEditor.vue'

describe('OutputProcessingEditor', () => {
  it('新增处理时仅使用当前 QKV 已产出的变量作为默认输入', async () => {
    const wrapper = mount(OutputProcessingEditor, {
      props: {
        modelValue: [],
        produces: [{ name: 'DESCRIPTION', path: 'description' }],
      },
      global: {
        stubs: {
          'el-button': { template: '<button @click="$emit(\'click\')"><slot /></button>' },
          'el-alert': true,
          'el-input': true,
          'el-select': true,
          'el-option': true,
        },
      },
    })

    await wrapper.get('button').trigger('click')

    const emitted = wrapper.emitted('update:modelValue')
    expect(emitted).toBeTruthy()
    expect((emitted?.at(-1)?.[0] as Array<Record<string, unknown>>)[0]).toMatchObject({
      mode: 'derive',
      input: '{{DESCRIPTION}}',
      operation: 'feature_extract',
    })
  })

  it('后续处理可引用前序派生变量，但不提供未产生的变量', () => {
    const wrapper = mount(OutputProcessingEditor, {
      props: {
        modelValue: [
          { id: 'one', mode: 'derive', input: '{{DESCRIPTION}}', operation: 'trim', target_variable: 'TEXT' },
          { id: 'two', mode: 'derive', input: '{{TEXT}}', operation: 'upper', target_variable: 'NORMALIZED' },
        ],
        produces: [{ name: 'DESCRIPTION', path: 'description' }],
      },
      global: { stubs: { 'el-button': true, 'el-alert': true, 'el-input': true, 'el-select': true, 'el-option': true } },
    })

    expect((wrapper.vm as any).inputOptionsFor(0)).toEqual(['{{DESCRIPTION}}'])
    expect((wrapper.vm as any).inputOptionsFor(1)).toEqual(['{{DESCRIPTION}}', '{{TEXT}}'])
  })
})
