import { mount, shallowMount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { describe, expect, it } from 'vitest'

import MatcherEditor from '../MatcherEditor.vue'
import QfkProcessingEditor from '../QfkProcessingEditor.vue'
import TextExtractEditor from '../TextExtractEditor.vue'
import ValueExtractEditor from '../ValueExtractEditor.vue'

const textExtract = {
  type: 'text',
  rows: { mode: 'keywords', include: ['检测到IP'], exclude: [], include_mode: 'all' },
  cardinality: 'all',
  source: 'stdout',
  value_mode: 'string',
}

function matchProps() {
  return {
    mode: 'keyword' as const,
    match: {
      type: 'keyword',
      pattern: ['设置集群IP失败', 'IP冲突'],
      mode: 'and',
      expected: true,
      extract: textExtract,
    },
    produces: [],
  }
}

function mountEditor(props: InstanceType<typeof QfkProcessingEditor>['$props']) {
  return shallowMount(QfkProcessingEditor, {
    props,
    global: { plugins: [ElementPlus] },
  })
}

function mountMatcher(match: Record<string, any>) {
  return mount(MatcherEditor, {
    props: { modelValue: match },
    global: { plugins: [ElementPlus] },
  })
}

function numericMatch(value: number | string = 80) {
  return {
    type: 'threshold',
    aggregation: 'first_number',
    operator: '>',
    value,
    expected: true,
    extract: { ...textExtract, value_mode: 'number' },
  }
}

describe('QfkProcessingEditor 两步处理交互', () => {
  it('匹配模式按“处理单元 → 第一步取值 → 第二步判断”呈现，并复用统一取值组件', () => {
    const wrapper = mountEditor(matchProps())

    expect(wrapper.findAll('.processing-unit')).toHaveLength(1)
    expect(wrapper.text()).toContain('处理单元')
    expect(wrapper.text()).toContain('第一步：取值')
    expect(wrapper.text()).toContain('第二步：判断')
    expect(wrapper.text()).not.toContain('第二步：产出')

    const extract = wrapper.getComponent(ValueExtractEditor)
    expect(extract.props('modelValue')).toEqual(textExtract)
    expect(extract.props('embedded')).toBe(true)
    expect(extract.props('showTitle')).toBe(false)

    const matcher = wrapper.getComponent(MatcherEditor)
    expect(matcher.props('showExtract')).toBe(false)
    expect(matcher.props('embedded')).toBe(true)
  })

  it('执行结果处理标题展示绑定 Signal 的试运行入口', () => {
    const wrapper = shallowMount(QfkProcessingEditor, {
      props: matchProps(),
      global: {
        plugins: [ElementPlus],
        stubs: { 'el-button': { template: '<button><slot /></button>' } },
      },
    })

    expect(wrapper.find('.processing-header-actions').text()).toContain('试运行')
  })

  it('第一步取值关键字与第二步判定关键字分别更新，互不复制或覆盖', async () => {
    const props = matchProps()
    const wrapper = mountEditor(props)
    const nextExtract = {
      ...textExtract,
      rows: { ...textExtract.rows, include: ['vm-100'], exclude: ['测试数据'] },
    }

    wrapper.getComponent(ValueExtractEditor).vm.$emit('update:modelValue', nextExtract)
    await wrapper.vm.$nextTick()
    const extractUpdate = wrapper.emitted('update:match')?.at(-1)?.[0] as Record<string, any>
    expect(extractUpdate.extract).toEqual(nextExtract)
    expect(extractUpdate.pattern).toEqual(['设置集群IP失败', 'IP冲突'])

    const nextMatcher = { ...props.match, pattern: ['任务失败'] }
    wrapper.getComponent(MatcherEditor).vm.$emit('update:modelValue', nextMatcher)
    await wrapper.vm.$nextTick()
    const predicateUpdate = wrapper.emitted('update:match')?.at(-1)?.[0] as Record<string, any>
    expect(predicateUpdate.pattern).toEqual(['任务失败'])
    expect(predicateUpdate.extract.rows.include).toEqual(['检测到IP'])
  })

  it('第二步提供期望开关，并默认开启', async () => {
    const wrapper = mountMatcher(matchProps().match)
    const expectedSwitch = wrapper.findComponent({ name: 'ElSwitch' })

    expect(expectedSwitch.exists()).toBe(true)
    expect(expectedSwitch.props('modelValue')).toBe(true)

    const matcherVm = wrapper.vm as unknown as { expectedResult: boolean }
    matcherVm.expectedResult = false
    await wrapper.vm.$nextTick()

    const update = wrapper.emitted('update:modelValue')?.at(-1)?.[0] as Record<string, any>
    expect(update.expected).toBe(false)
    expect(update.extract).toEqual(textExtract)
  })

  it('数值阈值支持切换为变量占位符，并可切回固定数值', async () => {
    const wrapper = mountMatcher(numericMatch())
    const matcherVm = wrapper.vm as unknown as { setNumericValueMode: (mode: 'constant' | 'variable') => void }

    matcherVm.setNumericValueMode('variable')
    await wrapper.vm.$nextTick()
    let update = wrapper.emitted('update:modelValue')?.at(-1)?.[0] as Record<string, any>
    expect(update.value).toBe('{{THRESHOLD}}')
    expect(wrapper.text()).toContain('变量')

    await wrapper.setProps({ modelValue: update })
    matcherVm.setNumericValueMode('constant')
    await wrapper.vm.$nextTick()
    update = wrapper.emitted('update:modelValue')?.at(-1)?.[0] as Record<string, any>
    expect(update.value).toBe(0)
  })

  it('统计行数只在允许的数值消费者中展示，并固定为整数行选择配置', async () => {
    const extractWrapper = mount(ValueExtractEditor, {
      props: {
        modelValue: {
          ...textExtract,
          parser: 'whitespace_table',
          columns: [{ key: 'VALUE', selector: { by: 'index', index: 1 } }],
          ai_extract: { instruction: '提取数字' },
        },
        allowRowCount: true,
      },
      global: { plugins: [ElementPlus] },
    })
    const textEditor = extractWrapper.getComponent(TextExtractEditor)

    expect(textEditor.props('allowRowCount')).toBe(true)
    ;(textEditor.vm as unknown as { setCardinality: (value: string) => void }).setCardinality('count')
    await extractWrapper.vm.$nextTick()

    const update = extractWrapper.emitted('update:modelValue')?.at(-1)?.[0] as Record<string, any>
    expect(update).toMatchObject({ cardinality: 'count', value_mode: 'integer', rows: textExtract.rows })
    expect(update).not.toHaveProperty('parser')
    expect(update).not.toHaveProperty('columns')
    expect(update).not.toHaveProperty('ai_extract')

    const unsupportedWrapper = mount(ValueExtractEditor, {
      props: { modelValue: textExtract, allowRowCount: false },
      global: { plugins: [ElementPlus] },
    })
    expect(unsupportedWrapper.getComponent(TextExtractEditor).props('allowRowCount')).toBe(false)
  })

  it('取值和判断关键字输入按回车后保留编辑中的换行', async () => {
    const extractWrapper = mount(ValueExtractEditor, {
      props: { modelValue: textExtract },
      global: { plugins: [ElementPlus] },
    })
    await extractWrapper.findAllComponents({ name: 'ElInput' })[0].vm.$emit('input', '检测到IP\n')
    await extractWrapper.setProps({
      modelValue: { ...textExtract, rows: { ...textExtract.rows, include: ['检测到IP'] } },
    })
    await extractWrapper.vm.$nextTick()
    expect((extractWrapper.get('textarea').element as HTMLTextAreaElement).value).toBe('检测到IP\n')

    const matcherWrapper = mountMatcher({ type: 'keyword', pattern: '', mode: 'or', expected: true })
    await (matcherWrapper.vm as any).updateKeywordPatterns('虚拟机开机失败\n')
    await matcherWrapper.setProps({ modelValue: { type: 'keyword', pattern: '虚拟机开机失败', mode: 'or', expected: true } })
    await matcherWrapper.vm.$nextTick()
    expect((matcherWrapper.findAll('textarea')[1].element as HTMLTextAreaElement).value).toBe('虚拟机开机失败\n')
  })

  it('产出模式为每个变量创建独立处理单元，步骤标题统一为“第二步：产出”', () => {
    const produces = [
      { name: 'DUP_IP', type: 'string', extract: textExtract },
      { name: 'VM_ID', type: 'integer', extract: { type: 'json', path: 'data.vm.id', cardinality: 'exactly_one', source: 'stdout', value_mode: 'integer' } },
    ]
    const wrapper = mountEditor({ mode: 'produces', match: null, produces })

    expect(wrapper.findAll('.processing-unit')).toHaveLength(2)
    expect(wrapper.findAllComponents(ValueExtractEditor)).toHaveLength(2)
    expect(wrapper.findComponent(MatcherEditor).exists()).toBe(false)
    expect(wrapper.text().match(/第一步：取值/g)).toHaveLength(2)
    expect(wrapper.text().match(/第二步：产出/g)).toHaveLength(2)
    expect(wrapper.text()).not.toContain('变量处理单元')
    expect(wrapper.text()).not.toContain('声明式取值')
  })

  it('处理单元仅向阈值和数值变量开放统计行数', () => {
    const threshold = mountEditor({ ...matchProps(), match: numericMatch() })
    expect(threshold.getComponent(ValueExtractEditor).props('allowRowCount')).toBe(true)

    const keyword = mountEditor(matchProps())
    expect(keyword.getComponent(ValueExtractEditor).props('allowRowCount')).toBe(false)

    const produces = mountEditor({
      mode: 'produces',
      match: null,
      produces: [
        { name: 'TEXT', type: 'string', extract: textExtract },
        { name: 'COUNT', type: 'integer', extract: textExtract },
      ],
    })
    expect(produces.findAllComponents(ValueExtractEditor)[0].props('allowRowCount')).toBe(false)
    expect(produces.findAllComponents(ValueExtractEditor)[1].props('allowRowCount')).toBe(true)
  })

  it('更新一个变量的取值时只替换对应处理单元，不污染其他变量', async () => {
    const produces = [
      { name: 'DUP_IP', type: 'string', extract: textExtract },
      { name: 'VM_ID', type: 'integer', extract: { type: 'json', path: 'data.vm.id', cardinality: 'exactly_one', source: 'stdout', value_mode: 'integer' } },
    ]
    const wrapper = mountEditor({ mode: 'produces', match: null, produces })
    const nextExtract = { type: 'json', path: 'data.ip', cardinality: 'first', source: 'stdout', value_mode: 'string' }

    wrapper.findAllComponents(ValueExtractEditor)[0].vm.$emit('update:modelValue', nextExtract)
    await wrapper.vm.$nextTick()

    const emitted = wrapper.emitted('update:produces')?.at(-1)?.[0] as Array<Record<string, any>>
    expect(emitted[0]).toEqual({ ...produces[0], extract: nextExtract })
    expect(emitted[1]).toEqual(produces[1])
  })

  it('新增变量会创建包含默认取值契约的完整处理单元', async () => {
    const wrapper = mountEditor({ mode: 'produces', match: null, produces: [] })

    await wrapper.get('.add-processing-unit').trigger('click')

    const emitted = wrapper.emitted('update:produces')?.at(-1)?.[0] as Array<Record<string, any>>
    expect(emitted).toHaveLength(1)
    expect(emitted[0]).toMatchObject({
      name: '',
      type: 'string',
      extract: {
        type: 'text',
        rows: { mode: 'all' },
        cardinality: 'exactly_one',
        source: 'stdout',
        value_mode: 'string',
      },
    })
  })

  it('编辑产出变量名后保留同一个输入节点并可继续输入', async () => {
    const produces = [{ name: '', type: 'string', extract: textExtract }]
    const wrapper = mount(QfkProcessingEditor, {
      props: { mode: 'produces', match: null, produces },
      global: { plugins: [ElementPlus] },
    })
    const nameInput = wrapper.findAll('.processing-step')[1].find('.el-form-item').find('input')
    expect(nameInput.exists()).toBe(true)
    nameInput.element.focus()

    await nameInput.setValue('N')
    const updatedProduces = wrapper.emitted('update:produces')?.at(-1)?.[0] as Array<Record<string, any>>
    await wrapper.setProps({ produces: updatedProduces })
    await wrapper.vm.$nextTick()

    expect(wrapper.findAll('.processing-step')[1].find('.el-form-item').find('input').element).toBe(nameInput.element)

    await nameInput.setValue('N2')
    const continuedUpdate = wrapper.emitted('update:produces')?.at(-1)?.[0] as Array<Record<string, any>>
    expect(continuedUpdate[0].name).toBe('N2')
  })

  it('删除前置产出单元后保留其余单元的渲染身份', async () => {
    const produces = [
      { name: 'FIRST', type: 'string', extract: textExtract },
      { name: 'SECOND', type: 'string', extract: textExtract },
    ]
    const wrapper = mount(QfkProcessingEditor, {
      props: { mode: 'produces', match: null, produces },
      global: { plugins: [ElementPlus] },
    })
    const secondInput = wrapper.findAll('.processing-step')[3].find('.el-form-item').find('input')
    await wrapper.findAll('.processing-unit')[0].get('.el-button').trigger('click')
    const updatedProduces = wrapper.emitted('update:produces')?.at(-1)?.[0] as Array<Record<string, any>>
    await wrapper.setProps({ produces: updatedProduces })
    await wrapper.vm.$nextTick()

    expect(wrapper.findAll('.processing-step')[1].find('.el-form-item').find('input').element).toBe(secondInput.element)
  }, 15000)

  it('模式切换只发出结构化意图，由父级统一维护互斥契约', () => {
    const wrapper = mountEditor(matchProps())

    wrapper.getComponent({ name: 'ElRadioGroup' }).vm.$emit('change', 'produces')

    expect(wrapper.emitted('update:mode')).toEqual([['produces']])
  })
})
