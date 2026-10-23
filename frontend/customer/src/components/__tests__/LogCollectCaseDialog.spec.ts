/**
 * LogCollectCaseDialog 组件测试
 *
 * 只覆盖表单校验与提交语义：
 * - 格式非法时本地拦截，不发起服务端校验（避免用无效输入打接口）；
 * - 合法输入调用 store.confirmLogCollectCase，错误信息透传到界面；
 * - store 抛出的归属失败提示必须可见（404 统一文案）。
 * 归属校验的服务端行为在 chat.log-collect-case.spec.ts 与 staging 实测中覆盖。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { h } from 'vue'
import { mount } from '@vue/test-utils'

const { mockStore } = vi.hoisted(() => ({
  mockStore: {
    logCollectCaseDialogVisible: true,
    logCollectCaseId: null as string | null,
    logCollectCaseLoading: false,
    existingCases: [] as any[],
    confirmLogCollectCase: vi.fn(),
    closeLogCollectCaseDialog: vi.fn(),
  },
}))

vi.mock('@/stores/chat', () => ({ useChatStore: () => mockStore }))

const ElDialogStub = {
  setup(_: any, { slots }: any) {
    return () => h('div', [slots.default?.(), slots.footer?.()])
  },
}

const ElInputStub = {
  props: ['modelValue'],
  emits: ['update:modelValue'],
  setup(props: any, { emit }: any) {
    return () =>
      h('input', {
        value: props.modelValue,
        onInput: (e: Event) => emit('update:modelValue', (e.target as HTMLInputElement).value),
      })
  },
}

const ElButtonStub = {
  emits: ['click'],
  setup(_: any, { emit, slots }: any) {
    return () => h('button', { onClick: () => emit('click') }, slots.default?.())
  },
}

async function mountDialog() {
  const Dialog = (await import('@/components/LogCollectCaseDialog.vue')).default
  return mount(Dialog, {
    global: {
      stubs: {
        'el-dialog': ElDialogStub,
        'el-input': ElInputStub,
        'el-button': ElButtonStub,
      },
    },
  })
}

function clickConfirm(wrapper: any) {
  const button = wrapper.findAll('button').find((b: any) => b.text().includes('确定'))
  return button.trigger('click')
}

describe('LogCollectCaseDialog', () => {
  beforeEach(() => {
    mockStore.confirmLogCollectCase.mockReset()
    mockStore.closeLogCollectCaseDialog.mockReset()
    mockStore.logCollectCaseLoading = false
    mockStore.existingCases = []
  })

  it('格式非法时本地拦截，不调用服务端校验', async () => {
    const wrapper = await mountDialog()

    await wrapper.find('input').setValue('not a case id!')
    await clickConfirm(wrapper)

    expect(mockStore.confirmLogCollectCase).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('工单号格式不正确')
    wrapper.unmount()
  })

  it('合法工单号调用 confirm，并把归属失败提示展示出来', async () => {
    mockStore.confirmLogCollectCase.mockRejectedValue(new Error('工单不存在或不属于当前客户端'))
    const wrapper = await mountDialog()

    await wrapper.find('input').setValue('Q2026102300001')
    await clickConfirm(wrapper)
    await wrapper.vm.$nextTick()

    expect(mockStore.confirmLogCollectCase).toHaveBeenCalledWith('Q2026102300001')
    expect(wrapper.text()).toContain('工单不存在或不属于当前客户端')
    wrapper.unmount()
  })

  it('列出当前客户端名下工单，点击即填入输入框', async () => {
    mockStore.existingCases = [
      { case_id: 'Q2026102300001', title: '虚拟机挂起', status: 'closed' },
      { case_id: 'Q2026102300002', title: '存储卡慢', status: 'in_progress' },
    ]
    const wrapper = await mountDialog()

    expect(wrapper.text()).toContain('或从我的工单中选择')
    expect(wrapper.findAll('.lc-case-item')).toHaveLength(2)
    expect(wrapper.text()).toContain('已关闭')

    await wrapper.findAll('.lc-case-item')[1].trigger('click')
    expect((wrapper.find('input').element as HTMLInputElement).value).toBe('Q2026102300002')
    wrapper.unmount()
  })

  it('点击取消关闭对话框', async () => {
    const wrapper = await mountDialog()

    const cancel = wrapper.findAll('button').find((b: any) => b.text().includes('取消'))
    expect(cancel).toBeDefined()
    await cancel!.trigger('click')

    expect(mockStore.closeLogCollectCaseDialog).toHaveBeenCalled()
    wrapper.unmount()
  })
})
