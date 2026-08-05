import { shallowMount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { describe, expect, it } from 'vitest'

import MatcherEditor from '../MatcherEditor.vue'
import QfkProcessingEditor from '../QfkProcessingEditor.vue'
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

  it('产出模式为每个变量创建独立处理单元，步骤标题统一为“第二步：产出”', () => {
    const produces = [
      { name: 'DUP_IP', type: 'string', extract: textExtract },
      { name: 'VM_ID', type: 'integer', extract: { type: 'json', path: 'data.vm.id', cardinality: 'exactly_one', source: 'stdout', value_mode: 'integer' } },
    ]
    const wrapper = mountEditor({ mode: 'produces', match: null, produces })

    expect(wrapper.findAll('.processing-unit')).toHaveLength(2)
    expect(wrapper.findAllComponents(ValueExtractEditor)).toHaveLength(2)
    expect(wrapper.text().match(/第一步：取值/g)).toHaveLength(2)
    expect(wrapper.text().match(/第二步：产出/g)).toHaveLength(2)
    expect(wrapper.text()).not.toContain('变量处理单元')
    expect(wrapper.text()).not.toContain('声明式取值')
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

  it('模式切换只发出结构化意图，由父级统一维护互斥契约', () => {
    const wrapper = mountEditor(matchProps())

    wrapper.getComponent({ name: 'ElRadioGroup' }).vm.$emit('change', 'produces')

    expect(wrapper.emitted('update:mode')).toEqual([['produces']])
  })
})
