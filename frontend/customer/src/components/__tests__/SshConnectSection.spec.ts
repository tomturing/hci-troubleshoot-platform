/**
 * SshConnectSection 组件测试（抽取后的唯一 SSH 模板）
 *
 * 锁定方案 §9 的四条契约：
 * 1. bridge 未运行 → 显示引导，不渲染表单；
 * 2. bridge 已运行 → 渲染表单；
 * 3. 连接成功 → emit connected(host)，写入 lastSshHost 与本地非敏感配置；
 * 4. 连接失败 → 错误留在表单，不 emit connected；
 * 5. inline 模式下已有会话 → 直接折叠为「已连接」，不再要求登录。
 *
 * 表单字段由 SshFormSection 负责，这里用 stub 直接给出一次完整快照。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { h, onMounted } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'

const { mockStore } = vi.hoisted(() => ({
  mockStore: {
    sshConnectionState: 'disconnected' as 'disconnected' | 'connecting' | 'connected' | 'error',
    sshErrorMessage: '',
    sshFlowDialogCaseId: null as string | null,
    sshFlowDialogVisible: true,
    connectSSH: vi.fn(),
    setLastSshHost: vi.fn(),
    openTerminalSidebar: vi.fn(),
  },
}))

vi.mock('@/stores/chat', () => ({ useChatStore: () => mockStore }))

vi.mock('@/api/terminal', () => ({
  checkBridgeBeforeOpen: vi.fn().mockResolvedValue('running'),
  checkBridgeRunning: vi.fn().mockResolvedValue(true),
}))

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

const ElIconStub = {
  setup(_: any, { slots }: any) {
    return () => h('span', slots.default?.())
  },
}

/** 表单一挂载就回填一份合法快照，模拟用户已填写 */
const SshFormSectionStub = {
  props: ['sshForm', 'authType'],
  emits: ['update:sshForm', 'update:authType'],
  setup(_: any, { emit }: any) {
    onMounted(() => {
      emit('update:sshForm', {
        host: '10.10.1.21',
        port: '22',
        username: 'root',
        password: 'secret',
        privateKey: '',
        passphrase: '',
      })
      emit('update:authType', 'password')
    })
    return () => h('div', { class: 'ssh-form-stub' })
  },
}

async function mountSection(props: Record<string, unknown> = {}) {
  const Section = (await import('@/components/SshConnectSection.vue')).default
  return mount(Section, {
    props: { bridgeStatus: 'running', mode: 'inline', ...props },
    global: {
      stubs: {
        'el-button': ElButtonStub,
        'el-alert': ElAlertStub,
        'el-icon': ElIconStub,
        SshFormSection: SshFormSectionStub,
      },
    },
  })
}

describe('SshConnectSection', () => {
  beforeEach(() => {
    localStorage.clear()
    mockStore.sshConnectionState = 'disconnected'
    mockStore.sshErrorMessage = ''
    mockStore.sshFlowDialogCaseId = null
    mockStore.connectSSH.mockReset()
    mockStore.setLastSshHost.mockReset()
    mockStore.openTerminalSidebar.mockReset()
  })

  it('bridge 未运行 → 显示引导，不渲染表单', async () => {
    const wrapper = await mountSection({ bridgeStatus: 'not-running' })

    expect(wrapper.text()).toContain('SSH Bridge 未运行')
    expect(wrapper.find('.ssh-form-stub').exists()).toBe(false)
    wrapper.unmount()
  })

  it('bridge 已运行 → 渲染表单与「连接」按钮', async () => {
    const wrapper = await mountSection()

    expect(wrapper.find('.ssh-form-stub').exists()).toBe(true)
    expect(wrapper.findAll('button').some((b) => b.text().includes('连接'))).toBe(true)
    wrapper.unmount()
  })

  it('连接成功 → emit connected(host) 并记录非敏感配置', async () => {
    mockStore.connectSSH.mockResolvedValue(undefined)
    const wrapper = await mountSection()
    await flushPromises()

    const connectBtn = wrapper.findAll('button').find((b) => b.text().includes('连接'))!
    await connectBtn.trigger('click')
    await flushPromises()

    expect(mockStore.connectSSH).toHaveBeenCalledWith(
      expect.objectContaining({ host: '10.10.1.21', username: 'root', authType: 'password' }),
    )
    expect(mockStore.setLastSshHost).toHaveBeenCalledWith('10.10.1.21')
    expect(wrapper.emitted('connected')?.[0]).toEqual(['10.10.1.21'])

    const saved = JSON.parse(localStorage.getItem('hci_last_ssh_config') || '{}')
    expect(saved.host).toBe('10.10.1.21')
    // 密码绝不落盘
    expect(saved.password).toBeUndefined()
    wrapper.unmount()
  })

  it('连接失败 → 错误留在表单，不 emit connected', async () => {
    mockStore.connectSSH.mockRejectedValue(new Error('密码错误'))
    const wrapper = await mountSection()
    await flushPromises()

    const connectBtn = wrapper.findAll('button').find((b) => b.text().includes('连接'))!
    await connectBtn.trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('SSH 连接失败')
    expect(wrapper.text()).toContain('密码错误')
    expect(wrapper.emitted('connected')).toBeUndefined()
    expect(mockStore.setLastSshHost).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('inline 模式且已连接 → 直接折叠为已连接状态', async () => {
    mockStore.sshConnectionState = 'connected'
    localStorage.setItem('hci_last_ssh_config', JSON.stringify({ host: '10.10.1.22', port: '22' }))

    const wrapper = await mountSection()

    expect(wrapper.text()).toContain('已连接 10.10.1.22')
    expect(wrapper.find('.ssh-form-stub').exists()).toBe(false)
    wrapper.unmount()
  })
})