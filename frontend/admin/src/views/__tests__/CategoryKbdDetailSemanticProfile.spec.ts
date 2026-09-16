import { shallowMount, flushPromises } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { afterEach, describe, expect, it, vi } from 'vitest'
import CategoryManageView from '../CategoryManageView.vue'

afterEach(() => vi.unstubAllGlobals())

function mountView() {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({ items: [], total: 0 }) }))
  return shallowMount(CategoryManageView, { global: { plugins: [ElementPlus] } })
}

const profile = {
  schema_version: 1,
  diagnosis_capability: 'guidance_only',
  canonical_symptoms: ['使用 ISO 安装 2016 提示缺少计算机所需介质驱动程序'],
  positive_anchors: ['缺少计算机所需的介质驱动程序'],
  exclusion_anchors: ['安装完成后无法进入系统'],
  manual_evidence_request: ['请提供 ISO 文件名与 SHA1'],
  manual_evidence_fields: [{ id: 'iso_name', label: 'ISO 文件名', required: true, input_type: 'text' }],
  semantic_recommendation: { enabled: true, minimum_score: 0.75, minimum_margin: 0.15 },
  semantic_disambiguation: [
    { id: 'install_mode', question: '安装方式是？', choices: [{ id: 'iso', label: 'ISO 安装', effect: 'support' }] },
  ],
  clarifying_questions: ['是否已加载 VirtIO 驱动？'],
  routing_examples: [{ description: 'ISO 安装提示缺少介质驱动', expected_match: true }],
  source_evidence: [{ field_path: 'positive_anchors.0', source_ref: 'problem_description', quote: '缺少计算机所需的介质驱动程序' }],
  applicability: { product_version: ['HCI 6.8.0'], product: [] },
}

const consumerSignal = {
  id: 'log_check',
  acquire: { tool: 'qfk_log', args: { keyword: '{{HOST}} 介质', time_window: '{{DATE}}' } },
}
const contextSignal = { id: 'context', acquire: { tool: 'qkv_case_context', args: {} } }

describe('分类管理案例详情预览：语义入口画像', () => {
  it('渲染画像、消费者、必填输入与业务筛选条件', async () => {
    const wrapper = mountView()
    await flushPromises()
    const vm = wrapper.vm as any

    // 无画像时区块不出现，预览保持原有形态
    vm.detailKbdEntry = { id: 1, support_id: '15936', title: '案例', content_md: '', hit_count: 0 }
    expect(vm.detailSemanticProfile).toBeNull()

    vm.detailKbdEntry = {
      id: 1,
      support_id: '15936',
      title: '案例',
      content_md: '',
      hit_count: 0,
      signals_json: {
        schema_version: 2,
        signals: [contextSignal, consumerSignal],
        semantic_entry_profile: profile,
        verification_contract: {
          variables: {
            HOST: { type: 'string', description: '目标主机' },
            DATE: { type: 'string', description: '日志日期' },
          },
        },
      },
    }

    expect(vm.detailSemanticProfile.canonical_symptoms).toEqual(profile.canonical_symptoms)
    expect(vm.semanticCapabilityLabel(vm.detailSemanticProfile.diagnosis_capability)).toBe('仅人工指引')
    // 只统计可执行消费者，入口信号自身不参与
    expect(vm.detailSemanticConsumers.map((signal: any) => signal.id)).toEqual(['log_check'])
    // 必填输入来自消费者命令模板中的变量引用，并带上输入契约说明
    expect(vm.detailSemanticRequiredInputs).toEqual([
      { name: 'DATE', type: 'string', description: '日志日期' },
      { name: 'HOST', type: 'string', description: '目标主机' },
    ])
    // 空数组的业务筛选条件不展示，避免预览出现无意义的空行
    expect(vm.detailSemanticScope).toEqual([{ field: 'product_version', label: '版本', values: ['HCI 6.8.0'] }])
    expect(vm.detailSemanticEvidenceFields).toHaveLength(1)
    // 画像全部配置项都要能在详情预览里看到，未配置的以空态文案呈现
    expect(vm.detailSemanticRecommendation).toEqual({ enabled: true, minimum_score: 0.75, minimum_margin: 0.15 })
    expect(vm.detailSemanticClarifyingQuestions).toEqual(['是否已加载 VirtIO 驱动？'])
    expect(vm.detailSemanticDisambiguation[0].id).toBe('install_mode')
    expect(vm.detailSemanticRoutingExamples[0].expected_match).toBe(true)
    expect(vm.detailSemanticSourceEvidence[0].quote).toBe('缺少计算机所需的介质驱动程序')
    expect(vm.semanticEvidencePathLabel('positive_anchors.0')).toBe('正向锚点 #1')
    expect(vm.semanticEvidencePathLabel('canonical_symptoms')).toBe('标准症状')
    expect(vm.semanticSourceSectionLabel('problem_description')).toBe('问题描述')
    expect(vm.semanticScopeLabel('object_type')).toBe('对象类型')
    expect(vm.semanticInputTypeLabel('textarea')).toBe('多行文本')

    wrapper.unmount()
  })
})
