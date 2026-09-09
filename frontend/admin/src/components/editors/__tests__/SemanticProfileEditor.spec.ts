import { mount, flushPromises } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { describe, expect, it, vi } from 'vitest'
import SemanticProfileEditor from '../SemanticProfileEditor.vue'
import { createOfflineDiagnosisApi } from '@hci/shared'

const initial = {
  schema_version: 1 as const, diagnosis_capability: 'executable' as const,
  canonical_symptoms: ['镜像格式不支持'], positive_anchors: ['镜像格式不支持'], exclusion_anchors: [],
}

describe('语义画像编辑与预览', () => {
  it('从上下文信号进入时展开路由区，不自动请求或保存', async () => {
    const preview = vi.fn()
    const wrapper = mount(SemanticProfileEditor, {
      props: { initial, preview, previewInitiallyOpen: true }, global: { plugins: [ElementPlus] },
    })
    const section = wrapper.findAll('.el-collapse-item').find(item => item.text().includes('路由试运行：'))!
    expect(section.classes()).toContain('is-active')
    expect(wrapper.text()).toContain('本次假设任务、告警、弹框均已确认未命中')
    expect(preview).not.toHaveBeenCalled()
    expect(wrapper.emitted('save')).toBeUndefined()
    wrapper.unmount()
  })

  it('画像未填写时仍可打开路由区，但不能提交试运行', () => {
    const wrapper = mount(SemanticProfileEditor, {
      props: { initial: { ...initial, canonical_symptoms: [], positive_anchors: [] }, preview: vi.fn(), previewInitiallyOpen: true },
      global: { plugins: [ElementPlus] },
    })
    expect(wrapper.text()).toContain('请先补齐上方标准症状、正向锚点')
    const button = wrapper.findAll('button').find(item => item.text().includes('试运行当前草稿'))!
    expect(button.attributes('disabled')).toBeDefined()
    wrapper.unmount()
  })

  it('多行编辑不重建输入框、不吞掉换行，保存不污染原始对象', async () => {
    const wrapper = mount(SemanticProfileEditor, { props: { initial, preview: vi.fn() }, global: { plugins: [ElementPlus] } })
    const input = wrapper.find('textarea')
    const originalNode = input.element
    await input.setValue('镜像格式不支持\n')
    expect(wrapper.find('textarea').element).toBe(originalNode)
    expect((input.element as HTMLTextAreaElement).value).toBe('镜像格式不支持\n')
    await input.setValue('镜像格式不支持\n创建时格式不受支持')
    await wrapper.findAll('button').find(button => button.text().includes('保存画像'))!.trigger('click')
    expect((wrapper.emitted('save')![0][0] as any).canonical_symptoms).toHaveLength(2)
    expect(initial.canonical_symptoms).toEqual(['镜像格式不支持'])
    wrapper.unmount()
  })

  it('预览失败保留草稿，成功展示过滤理由和补证据问题', async () => {
    const preview = vi.fn().mockRejectedValueOnce(new Error('画像修订已变化')).mockResolvedValue({
      decision: 'inconclusive', reason: 'ambiguous_candidates', candidates: [], degraded: true,
      filtered_candidates: [{ support_id: 'KB1', reason: 'scope_mismatch:product_version' }],
      next_action: { question: '请补充平台版本' },
    })
    const wrapper = mount(SemanticProfileEditor, { props: { initial, preview }, global: { plugins: [ElementPlus] } })
    await wrapper.find('textarea[placeholder="输入正例或近似反例的客户描述"]').setValue('镜像格式不支持')
    const button = wrapper.findAll('button').find(item => item.text().includes('试运行当前草稿'))!
    await button.trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('画像修订已变化')
    expect((wrapper.find('textarea').element as HTMLTextAreaElement).value).toBe('镜像格式不支持')
    await button.trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('请补充平台版本')
    expect(wrapper.text()).toContain('scope_mismatch:product_version')
    expect(wrapper.emitted('save')).toBeUndefined()
    wrapper.unmount()
  })

  it('离线故障上下文随采集计划提交，旧调用仍兼容', () => {
    const client = { post: vi.fn() }
    const api = createOfflineDiagnosisApi(client as any, {})
    const context = { semantic_context: { description: '创建失败', error_text: '镜像格式不支持' } }
    api.createPlan('session', '6.12', 'request', context)
    expect(client.post.mock.calls[0][1]).toEqual({ product_version: '6.12', context })
    api.createPlan('session', '6.12')
    expect(client.post.mock.calls[1][1]).toEqual({ product_version: '6.12', context: {} })
  })
})
