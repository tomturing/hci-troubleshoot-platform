/**
 * LogCollectEntryCard 组件测试
 *
 * 只验证渲染条件与文案：交互行为（打开抽屉 / 按工单 dismiss）已在
 * chat.log-collect-guide.spec.ts 覆盖，避免对 Element Plus stub 的点击语义
 * 产生脆弱依赖。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import { shallowMount } from '@vue/test-utils'

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
  getBridgeUrl: vi.fn().mockReturnValue('ws://127.0.0.1:17680'),
  buildConnectMessage: vi.fn(),
  buildInputMessage: vi.fn(),
  buildDisconnectMessage: vi.fn(),
  buildAgentExecMessage: vi.fn(),
  buildAgentExecProcessMessage: vi.fn(),
  buildVmConsoleOpMessage: vi.fn(),
  parseAgentExecResult: vi.fn(),
  stripAnsi: (s: string) => s,
  parseJsonOutput: vi.fn(),
}))

describe('LogCollectEntryCard', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('无活跃工单时不渲染引导', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const LogCollectEntryCard = (await import('@/components/LogCollectEntryCard.vue')).default

    const wrapper = shallowMount(LogCollectEntryCard, { global: { plugins: [pinia], stubs: { "el-button": true } } })

    expect(wrapper.find('.log-collect-guide').exists()).toBe(false)
    wrapper.unmount()
  })

  it('建单后渲染引导，包含前置条件说明', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const { useChatStore } = await import('@/stores/chat')
    const store = useChatStore()
    store.currentCase = { case_id: 'Q2026102200001', status: 'in_progress' } as any

    const LogCollectEntryCard = (await import('@/components/LogCollectEntryCard.vue')).default
    const wrapper = shallowMount(LogCollectEntryCard, { global: { plugins: [pinia], stubs: { "el-button": true } } })

    const guide = wrapper.find('.log-collect-guide')
    expect(guide.exists()).toBe(true)
    expect(guide.text()).toContain('需要采集 HCI 侧日志')
    expect(guide.text()).toContain('SSH 终端连接 HCI')
    wrapper.unmount()
  })

  it('选择暂不需要后引导消失', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const { useChatStore } = await import('@/stores/chat')
    const store = useChatStore()
    store.currentCase = { case_id: 'Q2026102200001', status: 'in_progress' } as any

    const LogCollectEntryCard = (await import('@/components/LogCollectEntryCard.vue')).default
    const wrapper = shallowMount(LogCollectEntryCard, { global: { plugins: [pinia], stubs: { "el-button": true } } })

    store.dismissLogCollectGuide()
    await nextTick()

    expect(wrapper.find('.log-collect-guide').exists()).toBe(false)
    wrapper.unmount()
  })
})
