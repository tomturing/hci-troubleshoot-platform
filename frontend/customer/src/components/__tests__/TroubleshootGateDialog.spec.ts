/**
 * TroubleshootGateDialog 组件测试
 *
 * 只验证「每个阶段必须给出哪些出路」，不验证像素与布局：
 * - bridge 未运行 → 下载并运行 bridge + 重新检测（+ 探测地址自证）
 * - bridge 就绪但没 SSH → 内嵌 SSH 登录区（复用 SshConnectSection）
 * - 两个阶段都必须有「无 SSH 排障（离线诊断）」与「暂不排障」
 *
 * 交互闭环（门禁结论如何回到调用方）在 chat.troubleshoot-gate.spec.ts 覆盖，
 * 这里刻意不重复断 store 行为，避免一处改动要修两套断言。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import { mount } from '@vue/test-utils'

vi.mock('@hci/shared', () => ({
  createApiClient: () => ({
    get: vi.fn().mockResolvedValue({ data: [] }),
    post: vi.fn().mockResolvedValue({ data: {} }),
    patch: vi.fn().mockResolvedValue({ data: {} }),
    delete: vi.fn().mockResolvedValue({ data: {} }),
  }),
  createCaseApi: () => ({
    listByClient: vi.fn().mockResolvedValue({ data: [] }),
    getById: vi.fn(),
    close: vi.fn().mockResolvedValue({}),
    create: vi.fn().mockResolvedValue({ data: {} }),
  }),
  createConversationApi: () => ({
    create: vi.fn().mockResolvedValue({ data: { conversation_id: 'conv-test' } }),
    getMessages: vi.fn().mockResolvedValue({ data: [] }),
  }),
  createAssistantApi: () => ({
    list: vi.fn().mockResolvedValue({ data: { assistants: [], default: 'htp-agent' } }),
  }),
  createEnvironmentApi: () => ({
    getEnvironmentByCase: vi.fn().mockResolvedValue({ data: null }),
    reportEnvironment: vi.fn().mockResolvedValue({}),
    getContext: vi.fn().mockResolvedValue({ data: { env_info: {}, alert_logs: [], task_logs: [] } }),
    listByCase: vi.fn().mockResolvedValue({ data: [] }),
    create: vi.fn().mockResolvedValue({ data: {} }),
    upsert: vi.fn().mockResolvedValue({ data: {} }),
  }),
}))

vi.mock('@/utils/clientId', () => ({ getClientId: () => 'test-client-id' }))
vi.mock('@/api/terminal', () => ({
  checkBridgeRunning: vi.fn().mockResolvedValue(false),
  checkBridgeBeforeOpen: vi.fn().mockResolvedValue('not-running'),
  createBridgeSocket: vi.fn(),
  getBridgeUrl: vi.fn().mockReturnValue('ws://localhost:9999'),
  buildConnectMessage: vi.fn(),
  buildInputMessage: vi.fn(),
  buildDisconnectMessage: vi.fn(),
  buildAgentExecMessage: vi.fn(),
  buildAgentExecProcessMessage: vi.fn(),
  buildVmConsoleOpMessage: vi.fn(),
  stripAnsi: (s: string) => s,
  parseJsonOutput: vi.fn(),
}))

/** el-dialog 会 teleport 内容；用同构 stub 保留默认插槽与 footer 插槽 */
const stubs = {
  'el-dialog': {
    props: ['modelValue', 'title'],
    template: '<div class="dialog-stub"><slot /><slot name="footer" /></div>',
  },
  'el-alert': { template: '<div class="alert-stub"><slot name="title" /><slot /></div>' },
  'el-button': { template: '<button><slot /></button>' },
  'el-icon': { template: '<i><slot /></i>' },
}

async function mountDialog() {
  const pinia = createPinia()
  setActivePinia(pinia)
  const { useChatStore } = await import('@/stores/chat')
  const store = useChatStore()
  store.troubleshootGateVisible = true
  store.troubleshootGateCaseId = 'Q2026102901522'
  const Dialog = (await import('@/components/TroubleshootGateDialog.vue')).default
  const wrapper = mount(Dialog, { global: { plugins: [pinia], stubs } })
  await nextTick()
  return { wrapper, store }
}

describe('TroubleshootGateDialog', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('bridge 未运行：给出下载 bridge 与重新检测，并暴露探测地址', async () => {
    const { wrapper, store } = await mountDialog()
    store.troubleshootGateBridgeStatus = 'not-running'
    store.troubleshootGateBridgeUrl = 'ws://localhost:9999'
    store.troubleshootGateStage = 'bridge-down'
    await nextTick()

    const text = wrapper.text()
    expect(text).toContain('未检测到 terminal_bridge')
    expect(text).toContain('下载 terminal_bridge')
    expect(text).toContain('已启动，重新检测')
    expect(text).toContain('ws://localhost:9999')
    // bridge 都没有时绝不能出现 SSH 表单（否则用户会以为可以直接登录）
    expect(wrapper.find('.ssh-connect-section').exists()).toBe(false)
    wrapper.unmount()
  })

  it('bridge 就绪但无 SSH：内嵌统一登录区，且不再显示下载引导', async () => {
    const { wrapper, store } = await mountDialog()
    store.troubleshootGateBridgeStatus = 'running'
    store.troubleshootGateStage = 'ssh-required'
    await nextTick()

    const text = wrapper.text()
    expect(text).toContain('尚未连接 HCI 的 SSH')
    // 复用统一登录区（唯一 SSH 模板），不是另造一套表单
    expect(wrapper.find('.ssh-connect-section').exists()).toBe(true)
    // 认证方式选择器是统一表单的标志性控件（el-form-item 的 label 在 stub 下不渲染）
    expect(wrapper.find('.ssh-connect-section').text()).toContain('密码认证')
    expect(text).not.toContain('未检测到 terminal_bridge')
    wrapper.unmount()
  })

  it('检查中给出明确反馈（不能点了没反应）', async () => {
    const { wrapper, store } = await mountDialog()
    store.troubleshootGateStage = 'checking'
    await nextTick()

    expect(wrapper.text()).toContain('正在检查 terminal_bridge')
    wrapper.unmount()
  })

  it('两个阶段都保留「无 SSH 排障」与「暂不排障」两条出路', async () => {
    const { wrapper, store } = await mountDialog()

    for (const stage of ['bridge-down', 'ssh-required'] as const) {
      store.troubleshootGateBridgeStatus = stage === 'bridge-down' ? 'not-running' : 'running'
      store.troubleshootGateStage = stage
      await nextTick()
      const text = wrapper.text()
      expect(text).toContain('无 SSH 排障（离线诊断）')
      expect(text).toContain('暂不排障')
    }
    wrapper.unmount()
  })
})
