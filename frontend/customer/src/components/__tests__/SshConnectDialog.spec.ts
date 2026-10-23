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
    sshFlowDialogMode: 'terminal-only' as 'create-case' | 'terminal-only',
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
  setup(_: any, { slots }: any) {
    return () => h('div', { class: 'el-dialog-stub' }, slots.default?.())
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
  props: ['bridgeStatus', 'mode', 'caseId', 'autoClose'],
  emits: ['cancelled', 'connected', 'state-change'],
  setup(_: any, { emit }: any) {
    return () => h('button', { class: 'section-cancel', onClick: () => emit('cancelled') }, '取消')
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
})
