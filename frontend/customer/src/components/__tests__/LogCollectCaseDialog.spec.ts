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
    // 弹框强依赖 SSH 会话：默认已登录，未登录路径单独用例覆盖
    sshConnectionState: 'connected' as 'disconnected' | 'connecting' | 'connected' | 'error',
    confirmLogCollectCase: vi.fn(),
    closeLogCollectCaseDialog: vi.fn(),
  },
}))

vi.mock('@/stores/chat', () => ({ useChatStore: () => mockStore }))

// 弹框内部做 bridge 前置探测，测试环境不连真实 bridge
vi.mock('@/api/terminal', () => ({
  checkBridgeBeforeOpen: vi.fn().mockResolvedValue('running'),
}))

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
  props: ['disabled'],
  emits: ['click'],
  setup(props: any, { emit, slots }: any) {
    return () =>
      h(
        'button',
        { disabled: props.disabled, onClick: () => emit('click') },
        slots.default?.(),
      )
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
        // 内嵌 SSH 登录区的行为由 SshConnectSection 自身测试覆盖
        SshConnectSection: true,
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
    mockStore.sshConnectionState = 'connected'
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

  it('未登录 SSH 时「确定」禁用，且点击不会发起服务端校验', async () => {
    mockStore.sshConnectionState = 'disconnected'
    const wrapper = await mountDialog()

    const confirm = wrapper.findAll('button').find((b: any) => b.text().includes('确定'))
    expect(confirm!.attributes('disabled')).toBeDefined()

    await wrapper.find('input').setValue('Q2026102300001')
    await clickConfirm(wrapper)
    await wrapper.vm.$nextTick()

    // 浏览器不会向 disabled 按钮派发 click，submit 因此不会被触发
    expect(mockStore.confirmLogCollectCase).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('登录成功后才能提交')
    wrapper.unmount()
  })
})
