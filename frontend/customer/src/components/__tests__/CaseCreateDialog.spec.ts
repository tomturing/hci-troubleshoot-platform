/**
 * CaseCreateDialog 组件级测试
 *
 * 验证 Bug 2 修复：弹框每次打开时 caseForm.assistantType 应重新同步为
 * 当前 chatStore.selectedAssistant，而非使用组件挂载时 immediate watch 锁定的初始值。
 *
 * Bug 复现路径（修复前）：
 *   1. 组件挂载 → selectedAssistant='qwen'
 *      → selectedAssistant watch {immediate:true} 触发
 *      → caseForm.assistantType = 'qwen'（已锁定）
 *   2. 用户在 top-bar 切换助手到 'ops-agent'
 *      → selectedAssistant watch 再次触发
 *      → 守卫 `if (val && !caseForm.assistantType)` → 'qwen' 为真 → 拦截更新
 *      → caseForm.assistantType 仍然是 'qwen'
 *   3. 用户发消息 → 弹框打开 (showCaseTemplate=true)
 *      → showCaseTemplate watch 触发
 *      → 修复前：caseForm.assistantType 仍是 'qwen'（工单被绑到错误助手）
 *      → 修复后：caseForm.assistantType = chatStore.selectedAssistant = 'ops-agent'
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { shallowMount } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { nextTick } from 'vue'
import type { AssistantInfo } from '@hci/shared'

const offlineDiagnosisMocks = vi.hoisted(() => ({
  navigateToOfflineDiagnosis: vi.fn(),
}))

vi.mock('@/utils/offlineDiagnosis', () => ({
  navigateToOfflineDiagnosis: offlineDiagnosisMocks.navigateToOfflineDiagnosis,
}))

// ── 模块 mock（需要在 @/stores/chat 和 CaseCreateDialog.vue 加载前生效）────
vi.mock('@hci/shared', () => ({
  createApiClient: () => ({
    get: vi.fn().mockResolvedValue({ data: [] }),
    post: vi.fn().mockResolvedValue({ data: {} }),
    patch: vi.fn().mockResolvedValue({ data: {} }),
    delete: vi.fn().mockResolvedValue({ data: {} }),
  }),
  createCaseApi: () => ({
    listByClient: vi.fn().mockResolvedValue({ data: [] }),
    getById: vi.fn().mockResolvedValue({ data: {} }),
    close: vi.fn().mockResolvedValue({}),
    create: vi.fn().mockResolvedValue({
      data: {
        case_id: 'case-dialog-1',
        client_id: 'test-client-id',
        status: 'in_progress',
        title: 'Test',
        description: null,
        assistant_type: 'ops-agent',
        created_at: '',
        updated_at: '',
        closed_at: null,
        trace_id: null,
      },
    }),
  }),
  createConversationApi: () => ({
    create: vi.fn().mockResolvedValue({ data: { conversation_id: 'conv-1' } }),
    getMessages: vi.fn().mockResolvedValue({ data: [] }),
  }),
  createAssistantApi: () => ({
    list: vi.fn().mockResolvedValue({
      data: { assistants: [], show_selector: false, default_assistant: null, selector_mode: 'auto' },
    }),
  }),
  createEnvironmentApi: () => ({
    getContext: vi.fn().mockResolvedValue({ data: { env_info: {}, alert_logs: [], task_logs: [] } }),
    listByCase: vi.fn().mockResolvedValue({ data: { items: [] } }),
    create: vi.fn().mockResolvedValue({ data: {} }),
    upsert: vi.fn().mockResolvedValue({ data: {} }),
  }),
}))

vi.mock('@/utils/clientId', () => ({ getClientId: () => 'test-client-id' }))
vi.mock('@/api/evaluate', () => ({ createEvaluateApi: () => ({}) }))
vi.mock('@/api/terminal', () => ({
  checkBridgeRunning: vi.fn().mockResolvedValue({ running: false }),
  checkBridgeBeforeOpen: vi.fn().mockResolvedValue({ running: false }),
  // createBridgeSocket 返回一个 WebSocket 形状的 stub，避免 closeTempSocket 出错
  createBridgeSocket: vi.fn(() => ({
    onopen: null,
    onmessage: null,
    onerror: null,
    onclose: null,
    send: vi.fn(),
    close: vi.fn(),
  })),
  buildConnectMessage: vi.fn().mockReturnValue('{}'),
  buildInputMessage: vi.fn().mockReturnValue('{}'),
  buildDisconnectMessage: vi.fn().mockReturnValue('{}'),
  buildBridgeMarker: vi.fn().mockReturnValue('__marker__'),
  buildBridgeCommandPayload: vi.fn().mockReturnValue('cmd\n'),
  parseBridgeCommandResult: vi.fn().mockReturnValue(null),
  stripAnsi: (s: string) => s,
  parseJsonOutput: vi.fn(),
}))

// element-plus 仅用作函数调用（ElMessage），shallowMount 会自动 stub 所有 el-* 组件
vi.mock('element-plus', () => ({
  ElMessage: { warning: vi.fn(), error: vi.fn(), success: vi.fn() },
}))

// ── 工厂函数 ─────────────────────────────────────────────────────────────
function makeAssistant(type: string, available: boolean, is_default = false): AssistantInfo {
  return { type, display_name: type, description: '', capabilities: [], available, is_default }
}

// ── 测试 ─────────────────────────────────────────────────────────────────
describe('CaseCreateDialog — Bug 2 组件级测试', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('弹框打开时 caseForm.assistantType 应同步为最新 selectedAssistant（而非初始挂载时锁定的旧值）', async () => {
    // 使用同一个 pinia 实例，确保 store 与组件共享同一状态
    const pinia = createPinia()
    setActivePinia(pinia)

    const { useChatStore } = await import('@/stores/chat')
    const store = useChatStore()

    // ── 步骤 1：手动设置 store 已完成初始化的状态（模拟 fetchAssistants 完成，默认 qwen）
    store.assistants = [
      makeAssistant('qwen', true, true),
      makeAssistant('ops-agent', true),
    ]
    store.selectedAssistant = 'qwen'
    store.showCaseTemplate = false
    store.caseTemplate = { title: '节点故障', description: '节点无法访问' }
    store.showAssistantSelector = true

    // ── 挂载组件（shallowMount 自动 stub 所有 el-* 子组件）
    const CaseCreateDialog = (await import('@/components/CaseCreateDialog.vue')).default
    const wrapper = shallowMount(CaseCreateDialog, {
      props: { bridgeStatus: 'running' },
      global: { plugins: [pinia] },
    })

    await nextTick()

    // ── 步骤 2：确认 immediate watch 触发，caseForm.assistantType 已被锁定为 'qwen'
    const setupState = (wrapper.getCurrentComponent() as any).setupState
    expect(setupState.caseForm.assistantType).toBe('qwen')

    // ── 步骤 3：用户在 top-bar 切换助手到 ops-agent
    store.selectedAssistant = 'ops-agent'
    await nextTick()

    // 守卫逻辑：if (val && !caseForm.assistantType)
    //   → caseForm.assistantType = 'qwen'（真值）→ ! 'qwen' = false → 拦截
    // 这是 Bug 2 的根因：切换后 caseForm 仍然是 'qwen'
    expect(setupState.caseForm.assistantType).toBe('qwen')

    // ── 步骤 4：用户输入消息后弹框打开（showCaseTemplate = true）
    store.showCaseTemplate = true
    await nextTick()

    // ── 关键断言：showCaseTemplate watch 触发，执行 caseForm.assistantType = chatStore.selectedAssistant
    // 修复前：'qwen'（工单会被错误地绑到 qwen 助手）
    // 修复后：'ops-agent'（正确同步为当前 top-bar 选择的助手）
    expect(setupState.caseForm.assistantType).toBe('ops-agent')

    wrapper.unmount()
  })

  it('首次打开弹框时（caseForm 尚未被锁定），assistantType 也正确同步', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)

    const { useChatStore } = await import('@/stores/chat')
    const store = useChatStore()

    // 初始状态：selectedAssistant 尚未赋值（空字符串）
    // 模拟组件挂载时 fetchAssistants 还未完成
    store.assistants = []
    store.selectedAssistant = ''
    store.showCaseTemplate = false
    store.caseTemplate = { title: '测试工单', description: '测试' }

    const CaseCreateDialog = (await import('@/components/CaseCreateDialog.vue')).default
    const wrapper = shallowMount(CaseCreateDialog, {
      props: { bridgeStatus: 'running' },
      global: { plugins: [pinia] },
    })
    await nextTick()

    const setupState = (wrapper.getCurrentComponent() as any).setupState
    // 空 selectedAssistant → immediate watch 不触发（val 为 falsy）→ caseForm 保持 ''
    expect(setupState.caseForm.assistantType).toBe('')

    // fetchAssistants 完成后设置了助手列表和 selectedAssistant
    store.assistants = [makeAssistant('qwen', true, true), makeAssistant('ops-agent', true)]
    store.selectedAssistant = 'ops-agent'
    await nextTick()
    // caseForm 此时 '' → 守卫通过（!'' = true）→ caseForm.assistantType = 'ops-agent'
    expect(setupState.caseForm.assistantType).toBe('ops-agent')

    // 弹框打开
    store.showCaseTemplate = true
    await nextTick()
    // showCaseTemplate watch 再次同步（值未变）→ 仍是 ops-agent
    expect(setupState.caseForm.assistantType).toBe('ops-agent')

    wrapper.unmount()
  })

  it('无 SSH 创建工单后不发送消息给 Agent，并默认打开离线诊断', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)

    const { useChatStore } = await import('@/stores/chat')
    const store = useChatStore()
    store.assistants = [makeAssistant('ops-agent', true, true)]
    store.selectedAssistant = 'ops-agent'
    store.showCaseTemplate = true
    store.caseTemplate = { title: '离线节点故障', description: '客户现场无法提供 SSH' }
    store.pendingUserMessage = '客户现场无法提供 SSH'

    const onlineFlowSpy = vi.spyOn(store, 'completeCaseCreationFlow')
    const offlineFlowSpy = vi.spyOn(store, 'completeOfflineCaseCreation')
    const CaseCreateDialog = (await import('@/components/CaseCreateDialog.vue')).default
    const wrapper = shallowMount(CaseCreateDialog, {
      props: { bridgeStatus: 'not-running' },
      global: { plugins: [pinia] },
    })
    await nextTick()

    const setupState = (wrapper.getCurrentComponent() as any).setupState
    setupState.caseForm.title = '离线节点故障'
    setupState.caseForm.description = '客户现场无法提供 SSH'
    setupState.caseForm.assistantType = 'ops-agent'

    await setupState.handleNoSSHCreate()
    await nextTick()

    expect(onlineFlowSpy).not.toHaveBeenCalled()
    expect(offlineFlowSpy).toHaveBeenCalledOnce()
    expect(store.currentCase?.case_id).toBe('case-dialog-1')
    expect(store.conversationId).toBeNull()
    expect(store.pendingUserMessage).toBe('')
    expect(store.showCaseTemplate).toBe(false)
    expect(offlineDiagnosisMocks.navigateToOfflineDiagnosis).toHaveBeenCalledWith('case-dialog-1')
    expect(store.messages.some(message => message.role === 'user')).toBe(false)

    wrapper.unmount()
  })
})
