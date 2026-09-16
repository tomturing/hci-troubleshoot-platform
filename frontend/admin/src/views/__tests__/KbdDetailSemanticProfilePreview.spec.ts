import { mount, flushPromises } from '@vue/test-utils'
import { nextTick } from 'vue'
import ElementPlus from 'element-plus'
import { afterEach, describe, expect, it, vi } from 'vitest'
import KbdReviewView from '../KbdReviewView.vue'

vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))

afterEach(() => {
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
})

/**
 * 案例 15936 这类 only-human 画像：详情预览必须让专家看到画像的**全部配置项**，
 * 否则「我配了什么」与「详情里显示什么」不一致，审核无从核对。
 */
const guidanceProfile = {
  schema_version: 1,
  diagnosis_capability: 'guidance_only',
  canonical_symptoms: [
    '在 HCI 平台创建虚拟机并通过 ISO 镜像安装 Windows Server 2016 时，安装程序进入“选择要安装的驱动程序”界面后弹出“加载驱动程序”提示，无法继续安装',
  ],
  positive_anchors: ['加载驱动程序', '缺少计算机所需的介质驱动程序', 'Windows Server 2016'],
  exclusion_anchors: [],
  manual_evidence_request: ['请提供所用 ISO 的文件名、SHA1 和文件大小。', '请确认虚拟磁盘控制器类型以及是否已加载对应驱动（如 VirtIO）。'],
  manual_evidence_fields: [
    { id: 'iso_name', label: 'ISO 文件名', required: true, input_type: 'text' },
    { id: 'iso_sha1', label: 'ISO SHA1', required: true, input_type: 'text' },
  ],
  semantic_disambiguation: [
    {
      id: 'install_mode',
      question: '当前是哪种安装方式？',
      choices: [
        { id: 'iso', label: 'ISO 镜像安装', effect: 'support' },
        { id: 'template', label: '模板克隆安装', effect: 'exclude' },
      ],
    },
  ],
  clarifying_questions: ['是否已加载 VirtIO 驱动？'],
  routing_examples: [{ description: 'ISO 安装提示缺少计算机所需的介质驱动程序', expected_match: true }],
  source_evidence: [
    { field_path: 'positive_anchors.1', source_ref: 'problem_description', quote: '缺少计算机所需的介质驱动程序' },
  ],
  applicability: { product_version: ['HCI 6.8.0'] },
}

const contextSignal = { id: 'context', acquire: { tool: 'qkv_case_context', args: {} }, orchestrate: { phase: 'diagnostic' } }

describe('KBD 案例详情预览：语义入口画像完整配置', () => {
  it('仅人工指引画像在详情预览里展示全部配置项，且不渲染空的消费者相关区块', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({ items: [], total: 0 }) }))
    const wrapper = mount(KbdReviewView, { global: { plugins: [ElementPlus] }, attachTo: document.body })
    await flushPromises()

    const vm = wrapper.vm as any
    vm.detailEntry = {
      id: 223,
      support_id: '15936',
      title: '使用ISO镜像安装2016提示缺少计算机所需介质驱动程序',
      status: 'published',
      content_md: '',
      images_json: [],
      metadata: {},
      category_id: null,
      signals_json: { schema_version: 2, signals: [contextSignal], semantic_entry_profile: guidanceProfile },
    }
    vm.detailDialogVisible = true
    await nextTick()
    await flushPromises()

    // 详情预览的读取口径（与 SemanticProfileEditor 的配置项一一对应）
    expect(vm.semanticCapabilityLabel(vm.activeSemanticProfile.diagnosis_capability)).toBe('仅人工指引')
    expect(vm.semanticConsumerSignals).toEqual([])
    expect(vm.semanticEvidenceFields.map((field: any) => field.id)).toEqual(['iso_name', 'iso_sha1'])
    expect(vm.semanticScope).toEqual([{ field: 'product_version', label: '版本', values: ['HCI 6.8.0'] }])
    expect(vm.semanticRecommendation).toBeNull()
    expect(vm.semanticClarifyingQuestions).toEqual(['是否已加载 VirtIO 驱动？'])
    expect(vm.semanticDisambiguation[0].choices).toHaveLength(2)
    expect(vm.semanticRoutingExamples[0].expected_match).toBe(true)
    expect(vm.semanticEvidencePathLabel(vm.semanticSourceEvidence[0].field_path)).toBe('正向锚点 #2')

    const rendered = document.body.textContent || ''
    expect(rendered).toContain('语义入口契约：仅人工指引')
    expect(rendered).toContain('缺少计算机所需的介质驱动程序')
    // 无消费者时的空态要给出明确原因，而不是留白
    expect(rendered).toContain('无可执行消费者：本案例不进入自动执行链路，命中后只请求人工补充证据。')
    expect(rendered).not.toContain('语义命中后的必填输入')
    // 画像配置项全部可见
    expect(rendered).toContain('适用范围')
    expect(rendered).toContain('HCI 6.8.0')
    expect(rendered).toContain('高置信语义推荐')
    expect(rendered).toContain('结构化补证据字段')
    expect(rendered).toContain('ISO SHA1')
    expect(rendered).toContain('请提供所用 ISO 的文件名、SHA1 和文件大小。')
    expect(rendered).toContain('语义澄清问题')
    expect(rendered).toContain('当前是哪种安装方式？')
    expect(rendered).toContain('多篇都像时先问什么')
    expect(rendered).toContain('画像回归正反例')
    expect(rendered).toContain('原文依据关联')

    wrapper.unmount()
  })
})
