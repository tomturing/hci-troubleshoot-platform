/**
 * LogCollectCaseDialog 组件测试
 *
 * 覆盖表单校验与「确定」的单入口语义：
 * - 格式非法时本地拦截，不发起服务端校验（避免用无效输入打接口）；
 * - 未登录时「确定」先驱动内嵌登录区登录，登录成功才提交工单；
 * - 登录失败 / bridge 未运行时一律不提交工单；
 * - 合法输入调用 store.confirmLogCollectCase，错误信息透传到界面；
 * - 内嵌登录区被配置为"无连接按钮、不打开终端抽屉"（登录与提交合并为一步）。
 * 归属校验的服务端行为在 chat.log-collect-case.spec.ts 与 staging 实测中覆盖。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { h } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'

const { mockStore, mockConnect, mockCheckBridge } = vi.hoisted(() => ({
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
  // 内嵌登录区由「确定」驱动：stub 暴露 connect()，返回值代表登录是否成功
  mockConnect: vi.fn(),
  mockCheckBridge: vi.fn(),
}))

vi.mock('@/stores/chat', () => ({ useChatStore: () => mockStore }))

// 弹框内部做 bridge 前置探测，测试环境不连真实 bridge
vi.mock('@/api/terminal', () => ({
  checkBridgeBeforeOpen: mockCheckBridge,
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

/**
 * 内嵌 SSH 登录区 stub：只暴露 connect() 与 props。
 * 真实的表单/状态机行为由 SshConnectSection.spec.ts 覆盖，这里只验证
 * "宿主怎么调用它、传了什么开关"。
 */
const SshConnectSectionStub = {
  name: 'SshConnectSection',
  props: ['bridgeStatus', 'caseId', 'mode', 'showConnectButton', 'openTerminal'],
  setup(_: any, { expose }: any) {
    expose({ connect: mockConnect })
    return () => h('div', { class: 'ssh-section-stub' })
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
        SshConnectSection: SshConnectSectionStub,
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
    mockConnect.mockReset()
    mockConnect.mockResolvedValue(true)
    mockCheckBridge.mockReset()
    mockCheckBridge.mockResolvedValue('running')
  })

  it('格式非法时本地拦截，不调用服务端校验，也不发起 SSH 登录', async () => {
    const wrapper = await mountDialog()

    await wrapper.find('input').setValue('not a case id!')
    await clickConfirm(wrapper)

    expect(mockStore.confirmLogCollectCase).not.toHaveBeenCalled()
    expect(mockConnect).not.toHaveBeenCalled()
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

  it('未登录时点「确定」先登录 SSH，成功后才提交工单', async () => {
    mockStore.sshConnectionState = 'disconnected'
    const order: string[] = []
    mockConnect.mockImplementation(async () => {
      order.push('connect')
      // 模拟 connectSSH 成功后 store 置位
      mockStore.sshConnectionState = 'connected'
      return true
    })
    mockStore.confirmLogCollectCase.mockImplementation(async () => {
      order.push('confirm')
    })

    const wrapper = await mountDialog()
    await wrapper.find('input').setValue('Q2026102300001')
    await clickConfirm(wrapper)
    await flushPromises()

    expect(mockConnect).toHaveBeenCalledTimes(1)
    expect(mockStore.confirmLogCollectCase).toHaveBeenCalledWith('Q2026102300001')
    expect(order).toEqual(['connect', 'confirm'])
    wrapper.unmount()
  })

  it('未登录时「确定」不再禁用（登录并入点击动作）', async () => {
    mockStore.sshConnectionState = 'disconnected'
    const wrapper = await mountDialog()

    const confirm = wrapper.findAll('button').find((b: any) => b.text().includes('确定'))
    expect(confirm!.attributes('disabled')).toBeUndefined()
    wrapper.unmount()
  })

  it('SSH 登录失败时不提交工单', async () => {
    mockStore.sshConnectionState = 'disconnected'
    mockConnect.mockResolvedValue(false)

    const wrapper = await mountDialog()
    await wrapper.find('input').setValue('Q2026102300001')
    await clickConfirm(wrapper)
    await flushPromises()

    expect(mockConnect).toHaveBeenCalledTimes(1)
    expect(mockStore.confirmLogCollectCase).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('bridge 未运行时先探测，仍不通则不登录也不提交', async () => {
    mockStore.sshConnectionState = 'disconnected'
    mockCheckBridge.mockResolvedValue('not-running')

    const wrapper = await mountDialog()
    await wrapper.find('input').setValue('Q2026102300001')
    await clickConfirm(wrapper)
    await flushPromises()

    expect(mockCheckBridge).toHaveBeenCalled()
    expect(mockConnect).not.toHaveBeenCalled()
    expect(mockStore.confirmLogCollectCase).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('SSH Bridge 未运行')
    wrapper.unmount()
  })

  it('内嵌登录区不带独立连接按钮，且登录成功不打开终端抽屉', async () => {
    const wrapper = await mountDialog()
    // stub 的 props 就是宿主实际下发的开关：两者都必须关闭，否则会多弹一个终端抽屉
    const section = wrapper.findComponent({ name: 'SshConnectSection' })
    expect(section.props('showConnectButton')).toBe(false)
    expect(section.props('openTerminal')).toBe(false)
    expect(section.props('mode')).toBe('inline')
    wrapper.unmount()
  })
})
