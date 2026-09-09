import { shallowMount, flushPromises } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { afterEach, describe, expect, it, vi } from 'vitest'
import KbdReviewView from '../KbdReviewView.vue'

vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))

const profile = {
  schema_version: 1, diagnosis_capability: 'executable',
  canonical_symptoms: ['启动失败'], positive_anchors: ['启动失败'],
}
const contextSignal = { id: 'context', acquire: { tool: 'qkv_case_context', args: {} }, orchestrate: {} }

afterEach(() => vi.unstubAllGlobals())

describe('KBD 信号试运行入口分流', () => {
  it('上下文信号打开画像路由区，取消不修改工作稿；普通信号仍使用原弹窗', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ items: [], total: 0 }) })
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = shallowMount(KbdReviewView, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    const vm = wrapper.vm as any
    vm.detailEntry = { id: 42, signals_json: { schema_version: 2, signals: [contextSignal], semantic_entry_profile: profile } }
    fetchMock.mockClear()
    vm.openSignalDryRun(contextSignal, 0)
    expect(vm.semanticProfileDialogVisible).toBe(true)
    expect(vm.semanticProfilePreviewInitiallyOpen).toBe(true)
    expect(vm.signalDryRunVisible).toBe(false)
    vm.semanticProfileDraft.positive_anchors.push('草稿修改')
    vm.semanticProfileDialogVisible = false
    expect(vm.detailEntry.signals_json.semantic_entry_profile.positive_anchors).toEqual(['启动失败'])
    expect(fetchMock).not.toHaveBeenCalled()

    for (const tool of ['qkv_task', 'qkv_alert', 'qkv_dialog', 'qfk_system']) {
      const signal = { id: tool, acquire: { tool, args: {} }, orchestrate: {} }
      vm.openSignalDryRun(signal, 1, 0)
      expect(vm.signalDryRunVisible).toBe(true)
      expect(vm.signalDryRunSignal.acquire.tool).toBe(tool)
      expect(vm.signalDryRunProcessingIndex).toBe(0)
      expect(vm.semanticProfileDialogVisible).toBe(false)
    }
    vm.openSemanticProfileEditor()
    expect(vm.semanticProfilePreviewInitiallyOpen).toBe(false)
    await vm.previewSemanticProfile(profile, { description: '启动失败' })
    expect(fetchMock).toHaveBeenCalledWith('/api/v1/kbd/42/semantic-preview', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ profile, context: { description: '启动失败' }, strong_producer_status: 'no_match' }),
    }))

    // 尚未创建画像时也能进入，要求补齐字段而非伪造变量处理规则。
    vm.detailEntry = { id: 43, signals_json: { schema_version: 2, signals: [contextSignal] } }
    vm.openSignalDryRun(contextSignal, 0)
    expect(vm.semanticProfileDraft.positive_anchors).toEqual([])
    expect(vm.semanticProfilePreviewInitiallyOpen).toBe(true)
    expect(vm.signalDryRunVisible).toBe(false)
    wrapper.unmount()
  })
})
