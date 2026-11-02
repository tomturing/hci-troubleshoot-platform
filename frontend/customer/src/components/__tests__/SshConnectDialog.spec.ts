/**
 * SshConnectDialog 组件测试
 *
 * 锁定回归：抽取 SshConnectSection 后，弹框内「取消」按钮 emit 的 cancelled 无人监听，
 * 点击后弹框不关闭（用户只能点右上角 X）。本用例直接锁定「点取消必须关框」这一契约。
 *
 * 收尾语义（断开未建立的会话 / create-case 的离线诊断导航）统一由 el-dialog 的 @close
 * 单点执行，因此按钮点击本身不得直接调用 disconnectSSH，否则会与 @close 重复执行。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { h } from 'vue'
import { mount } from '@vue/test-utils'

const { mockStore } = vi.hoisted(() => ({
  mockStore: {
    sshConnectionState: 'disconnected' as 'disconnected' | 'connecting' | 'connected' | 'error',
    sshFlowDialogVisible: true,
    sshFlowDialogMode: 'terminal-only' as 'create-case' | 'terminal-only' | 'log-collect',
    sshFlowDialogCaseId: null as string | null,
    disconnectSSH: vi.fn().mockResolvedValue(undefined),
    closeSshFlowDialog: vi.fn().mockResolvedValue(undefined),
  },
}))

vi.mock('@/stores/chat', () => ({ useChatStore: () => mockStore }))

/** 只渲染默认插槽，避免依赖 element-plus 的真实弹框与过渡动画 */
const ElDialogStub = {
  props: ['modelValue', 'title'],
  emits: ['update:modelValue', 'close'],
  setup(props: any, { slots }: any) {
    // 标题落成 data 属性：不同入口打开的是同一个弹框，标题是唯一能区分入口的可见证据
    return () =>
      h('div', { class: 'el-dialog-stub', 'data-title': props.title }, slots.default?.())
  },
}

const ElButtonStub = {
  emits: ['click'],
  setup(_: any, { emit, slots }: any) {
    return () => h('button', { onClick: () => emit('click') }, slots.default?.())
  },
}

const ElAlertStub = {
  setup(_: any, { slots }: any) {
    return () => h('div', { class: 'alert' }, [slots.title?.(), slots.default?.()])
  },
}

/** 模拟 section 的取消按钮：点击即 emit('cancelled') */
const SectionStub = {
  props: ['bridgeStatus', 'mode', 'caseId', 'autoClose', 'openTerminal'],
  emits: ['cancelled', 'connected', 'state-change'],
  setup(props: any, { emit }: any) {
    return () =>
      h(
        'button',
        {
          class: 'section-cancel',
          'data-open-terminal': String(props.openTerminal),
          'data-case-id': String(props.caseId ?? ''),
          onClick: () => emit('cancelled'),
        },
        '取消',
      )
  },
}

async function mountDialog(bridgeStatus: 'running' | 'not-running' = 'not-running') {
  const Dialog = (await import('@/components/SshConnectDialog.vue')).default
  return mount(Dialog, {
    props: { bridgeStatus },
    global: {
      stubs: {
        'el-dialog': ElDialogStub,
        'el-button': ElButtonStub,
        'el-alert': ElAlertStub,
        SshConnectSection: SectionStub,
      },
    },
  })
}

describe('SshConnectDialog', () => {
  beforeEach(() => {
    mockStore.sshConnectionState = 'disconnected'
    mockStore.sshFlowDialogVisible = true
    mockStore.sshFlowDialogMode = 'terminal-only'
    mockStore.disconnectSSH.mockClear()
    mockStore.closeSshFlowDialog.mockClear()
  })

  it('点击「取消」必须关闭弹框（回归：事件未接线时点了毫无反应）', async () => {
    const wrapper = await mountDialog()

    await wrapper.find('.section-cancel').trigger('click')

    expect(mockStore.sshFlowDialogVisible).toBe(false)
    wrapper.unmount()
  })

  it('按钮只负责关框，收尾逻辑交给 el-dialog @close 单点执行', async () => {
    const wrapper = await mountDialog()

    await wrapper.find('.section-cancel').trigger('click')

    // 避免与 @close 重复断开/重复导航
    expect(mockStore.disconnectSSH).not.toHaveBeenCalled()
    expect(mockStore.closeSshFlowDialog).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('已连接时通过 @close 收尾不得误断终端', async () => {
    mockStore.sshConnectionState = 'connected'
    const wrapper = await mountDialog('running')

    await wrapper.findComponent(ElDialogStub).vm.$emit('close')

    expect(mockStore.disconnectSSH).not.toHaveBeenCalled()
    expect(mockStore.closeSshFlowDialog).toHaveBeenCalled()
    wrapper.unmount()
  })

  /**
   * 日志收集面板的「连接 SSH」与右侧终端用的是同一个弹框，但两者的下一步不同：
   * 终端要看见终端，日志收集要回到表单点「开始收集」。因此模式必须同时决定
   * 标题（用户得知道自己从哪来）与「成功后是否弹右侧终端」。
   */
  it('log-collect 模式：标题指向日志收集，且成功后不打开右侧终端', async () => {
    mockStore.sshFlowDialogMode = 'log-collect'
    mockStore.sshFlowDialogCaseId = 'Q2026102300001'
    const wrapper = await mountDialog('running')

    const dialog = wrapper.findComponent(ElDialogStub)
    expect(dialog.attributes('data-title')).toBe('连接 SSH（日志收集需要会话）')
    const section = wrapper.find('.section-cancel')
    expect(section.attributes('data-open-terminal')).toBe('false')
    // 会话键是 caseID@nodeIP：弹框必须把面板绑定的工单号交给 section
    expect(section.attributes('data-case-id')).toBe('Q2026102300001')
    wrapper.unmount()
  })

  it('terminal-only 模式：标题与"成功后打开终端"保持原样', async () => {
    const wrapper = await mountDialog('running')

    expect(wrapper.findComponent(ElDialogStub).attributes('data-title')).toBe('连接 SSH 终端')
    expect(wrapper.find('.section-cancel').attributes('data-open-terminal')).toBe('true')
    wrapper.unmount()
  })
})
