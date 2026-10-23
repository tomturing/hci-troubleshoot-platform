/**
 * LogCollectPanel 组件测试
 *
 * 锁定三条回归契约（都来自 2026-10-23 staging 实测的问题）：
 * 1. 面板"带着 visible=true 首次挂载"时必须立即发起 probe——
 *    历史上 watch(visible) 缺 immediate，导致探测永不触发，界面永远停在
 *    「尚未读取集群目录，当前展示平台内置项」；
 * 2. 故障场景必须是勾选框全量平铺，不允许退回下拉框；
 * 3. 探测拿到节点清单后，目标主机必须一次性展示全部节点勾选项（含主控标记）。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { h } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import LogCollectPanel from '@/components/LogCollectPanel.vue'
import { logCollectApi, type LogCollectTask } from '@/api/logCollect'

const { sentMessages, fakeSocket, mockStore } = vi.hoisted(() => {
  const sent: string[] = []
  const socket: any = {
    readyState: 1,
    sent,
    onopen: null as null | (() => void),
    onerror: null as null | (() => void),
    onmessage: null as null | ((event: { data: string }) => void),
    send(payload: string) {
      sent.push(payload)
    },
    close() {
      this.readyState = 3
    },
  }
  return {
    sentMessages: sent,
    fakeSocket: socket,
    mockStore: {
      lastSshHost: '',
      sshConnectionState: 'disconnected' as 'disconnected' | 'connected',
      sshCurrentConfig: null as null | { host: string; port: number; username: string },
      logCollectActiveTask: null,
      openLogCollectCaseDialog: vi.fn(),
      // 抽屉宽度与历史工单共用同一真值
      sidePanelWidth: 510,
    },
  }
})

vi.mock('@/stores/chat', () => ({ useChatStore: () => mockStore }))

vi.mock('@/api/terminal', () => ({
  resolveBridgeUrl: vi.fn().mockResolvedValue('ws://localhost:9999'),
  createBridgeSocket: vi.fn(() => {
    // onopen 在构造之后才被赋值，必须延后一拍触发
    setTimeout(() => fakeSocket.onopen?.(), 0)
    return fakeSocket
  }),
  buildAcliSyncMessage: vi.fn(() => JSON.stringify({ type: 'acli_sync' })),
  buildLogCollectMessage: (caseId: string, action: string, options: any = {}) =>
    JSON.stringify({
      type: 'log_collect',
      case_id: caseId,
      log_action: action,
      node_ip: options.nodeIp,
    }),
}))

vi.mock('@/api/logCollect', () => ({
  logCollectApi: {
    createTask: vi.fn(),
    getTask: vi.fn(),
    cancelTask: vi.fn(),
    listTasks: vi.fn(),
  },
}))

const passthrough = (name: string, tag = 'div') => ({
  name,
  setup(_: any, { slots }: any) {
    return () => h(tag, slots.default?.())
  },
})

// ElAlert 的 title 是属性而非插槽，透传桩会丢掉它；这里显式渲染出来，
// 否则"完成态"一类文案无法在测试里断言。
const ElAlert = {
  props: ['title'],
  setup(props: any, { slots }: any) {
    return () => h('div', { class: 'alert-stub' }, [props.title, slots.default?.()])
  },
}

// 只把结构相关的组件渲染成可断言的 DOM，其余保持透传
const ElCheckboxGroup = {
  props: ['modelValue'],
  emits: ['update:modelValue'],
  setup(_: any, { slots }: any) {
    return () => h('div', { class: 'cbx-group' }, slots.default?.())
  },
}

const ElCheckbox = {
  props: ['value', 'label', 'modelValue'],
  setup(props: any, { slots }: any) {
    return () =>
      h('label', { class: 'cbx', 'data-value': props.value ?? props.label }, slots.default?.())
  },
}

function mountPanel() {
  return mount(
    LogCollectPanel,
    {
      props: { caseId: 'Q2026102300001', nodeIp: '172.28.25.1', modelValue: true },
      global: {
        stubs: {
          // 抽屉本体渲染 header/default/footer 三个插槽，便于断言头部动作
          ElDrawer: {
            name: 'ElDrawer',
            props: ['modelValue', 'direction', 'size', 'modalClass'],
            emits: ['update:modelValue', 'close'],
            setup(props: any, { slots }: any) {
              return () =>
                h('div', { class: 'drawer-stub', 'data-direction': props.direction, 'data-size': props.size }, [
                  h('div', { class: 'drawer-stub-header' }, slots.header?.()),
                  slots.default?.(),
                  slots.footer?.(),
                ])
            },
          },
          ElForm: passthrough('ElForm'),
          ElFormItem: passthrough('ElFormItem'),
          ElAlert,
          ElDescriptions: passthrough('ElDescriptions'),
          ElDescriptionsItem: passthrough('ElDescriptionsItem'),
          ElTag: passthrough('ElTag'),
          ElInput: passthrough('ElInput'),
          ElDatePicker: passthrough('ElDatePicker'),
          ElButton: passthrough('ElButton', 'button'),
          ElProgress: passthrough('ElProgress'),
          ElSteps: passthrough('ElSteps'),
          ElStep: passthrough('ElStep'),
          ElCheckboxGroup,
          ElCheckbox,
        },
      },
    },
  )
}

describe('LogCollectPanel', () => {
  beforeEach(() => {
    sentMessages.length = 0
    fakeSocket.readyState = 1
    mockStore.lastSshHost = ''
    mockStore.sshConnectionState = 'disconnected'
    mockStore.sshCurrentConfig = null
    // 默认"该工单没有任何历史任务"，需要历史的任务用例自行 mock
    vi.mocked(logCollectApi.listTasks).mockReset()
    vi.mocked(logCollectApi.listTasks).mockResolvedValue({ total: 0, items: [] })
    vi.mocked(logCollectApi.getTask).mockReset()
  })

  const taskFixture = (patch: Partial<LogCollectTask> = {}): LogCollectTask => ({
    task_id: 'task-1',
    case_id: 'Q2026102300001',
    origin_node_ip: '172.28.25.1',
    modules: ['vm_suspend'],
    date_from: '2026-10-20',
    date_to: '2026-10-21',
    ips: ['all'],
    status: 'completed',
    progress: 100,
    arch: 'x86_64',
    acli_version: '1.0.0',
    upload_mode: 'bridge_relay',
    archive_path: '/sf/data/local/acli/tasks/Q2026102300001.tar.gz',
    archive_size: 104857600,
    archive_sha256: 'a'.repeat(64),
    cleanup_state: 'none',
    archive_deleted: false,
    relay_status: 'success',
    relay_restarts: 0,
    created_at: '2026-10-22T07:00:00Z',
    updated_at: '2026-10-22T08:30:00Z',
    finished_at: '2026-10-22T08:30:00Z',
    ...patch,
  })

  // 回归：发起节点曾是可编辑输入框，用户改成另一个 IP 后 bridge 按 caseID@nodeIP 查不到会话
  // （session_missing）。它是"当前 SSH 会话连的那台机器"，只能展示，不能改。
  it('发起节点以只读事实展示，取自当前 SSH 会话而非输入', async () => {
    mockStore.sshConnectionState = 'connected'
    mockStore.sshCurrentConfig = { host: '10.10.1.21', port: 22, username: 'root' }

    const wrapper = mountPanel()
    await flushPromises()
    await new Promise((resolve) => setTimeout(resolve, 5))
    await flushPromises()

    const fact = wrapper.find('.lc-node-fact')
    expect(fact.exists()).toBe(true)
    expect(fact.find('.lc-node-fact-value').text()).toBe('10.10.1.21')
    expect(fact.text()).toContain('当前 SSH 会话')

    // 会话主机是唯一真值：即使调用方传了另一个 nodeIp，也不能拿它去探测
    const probe = sentMessages
      .map((raw) => JSON.parse(raw))
      .find((message) => message.log_action === 'probe')
    expect(probe?.node_ip).toBe('10.10.1.21')
  })

  it('带 visible=true 首次挂载即发出 probe（回归：immediate watcher）', async () => {
    mountPanel()
    await flushPromises()
    await new Promise((resolve) => setTimeout(resolve, 5))
    await flushPromises()

    expect(sentMessages.length).toBeGreaterThan(0)
    const probe = sentMessages
      .map((raw) => JSON.parse(raw))
      .find((message) => message.log_action === 'probe')
    expect(probe).toBeTruthy()
    expect(probe.case_id).toBe('Q2026102300001')
    expect(probe.node_ip).toBe('172.28.25.1')
  })

  it('抽屉从左侧弹出，宽度取自共享真值', async () => {
    const wrapper = mountPanel()
    await flushPromises()

    const drawer = wrapper.find('.drawer-stub')
    expect(drawer.attributes('data-direction')).toBe('ltr')
    expect(drawer.attributes('data-size')).toBe('510px')
  })

  it('抽屉头部提供「刷新进度」与「钉住」入口', async () => {
    const wrapper = mountPanel()
    await flushPromises()

    const header = wrapper.find('.drawer-stub-header')
    expect(header.text()).toContain('日志收集')
    expect(header.text()).toContain('刷新进度')
    expect(header.text()).toContain('钉住')
  })

  it('故障场景使用勾选框全量展示，不渲染下拉框', async () => {
    const wrapper = mountPanel()
    await flushPromises()

    const moduleGroup = wrapper.find('.lc-module-group')
    expect(moduleGroup.exists()).toBe(true)
    // 内置目录至少 8 项，且必须一次全部渲染出来
    expect(moduleGroup.findAll('.cbx').length).toBeGreaterThanOrEqual(8)
    expect(wrapper.html()).not.toContain('el-select')
  })

  it('探测返回节点清单后，目标主机以勾选框展示全部节点及主控标记', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    await new Promise((resolve) => setTimeout(resolve, 5))

    fakeSocket.onmessage?.({
      data: JSON.stringify({
        type: 'log_collect_result',
        log_action: 'probe',
        status: 'ok',
        log_data: {
          architecture: 'x86_64',
          acli_installed: true,
          acli_version: '1.0.1',
          available_mb: 20480,
          log_server_reachable: true,
          http_code: 200,
          catalog_source: 'acli_help',
          modules: [
            { value: 'vm_suspend', label: '虚拟机挂起专项' },
            { value: 'host_offline', label: '主机离线专项' },
          ],
          hosts: [
            { ip: '172.28.25.1', name: 'node-1', master: true },
            { ip: '172.28.25.2', name: 'node-2', master: false },
          ],
        },
      }),
    })
    await flushPromises()

    const hostGroup = wrapper.find('.lc-host-group')
    expect(hostGroup.exists()).toBe(true)
    expect(hostGroup.findAll('.cbx').length).toBe(2)
    expect(hostGroup.text()).toContain('172.28.25.2（node-2）')
    expect(hostGroup.text()).toContain('主控')
    // 全选时后端契约仍是 all，界面必须显示"已全选"
    expect(wrapper.text()).toContain('已全选')
  })

  // 回归：任务状态的真值在平台。完成后关掉抽屉再打开，曾经退回空白表单，用户会以为必须重收。
  it('平台已有已完成任务时优先展示完成结果，而不是默认让用户重新收集', async () => {
    vi.mocked(logCollectApi.listTasks).mockResolvedValue({
      total: 2,
      items: [taskFixture()],
    })

    const wrapper = mountPanel()
    await flushPromises()
    await flushPromises()

    const result = wrapper.find('.lc-result')
    expect(result.exists()).toBe(true)
    expect(result.text()).toContain('已完成日志收集')
    expect(result.text()).toContain('P3 经平台中转')
    expect(result.text()).toContain('全部在线节点')
    expect(result.text()).toContain('虚拟机挂起专项')
    expect(result.text()).toContain('/sf/data/local/acli/tasks/Q2026102300001.tar.gz')

    const labels = wrapper.findAll('button').map((button) => button.text())
    expect(labels).toContain('重新收集')
    // 完成态下不应把"再收一次"当成默认动作
    expect(labels).not.toContain('开始收集')
    wrapper.unmount()
  })

  it('结果态点「重新收集」回到表单，并保留重新收集入口', async () => {
    vi.mocked(logCollectApi.listTasks).mockResolvedValue({ total: 1, items: [taskFixture()] })

    const wrapper = mountPanel()
    await flushPromises()
    await flushPromises()

    const button = wrapper.findAll('button').find((item) => item.text() === '重新收集')
    expect(button).toBeTruthy()
    await button!.trigger('click')
    await flushPromises()

    expect(wrapper.find('.lc-result').exists()).toBe(false)
    expect(wrapper.find('.lc-module-group').exists()).toBe(true)
    // 回到表单后会立刻重新探测，按钮文案可能是"正在读取环境…"
    const labels = wrapper.findAll('button').map((item) => item.text())
    expect(labels.some((text) => text.includes('开始收集') || text.includes('正在读取环境'))).toBe(true)
    wrapper.unmount()
  })

  it('有进行中任务时优先接管，即使该工单已存在完成记录', async () => {
    const running = taskFixture({ task_id: 'task-running', status: 'collecting', progress: 42, finished_at: null })
    vi.mocked(logCollectApi.listTasks).mockResolvedValue({
      total: 2,
      items: [running, taskFixture()],
    })
    vi.mocked(logCollectApi.getTask).mockResolvedValue(running)

    const wrapper = mountPanel()
    await flushPromises()
    await flushPromises()

    expect(wrapper.find('.lc-result').exists()).toBe(false)
    expect(wrapper.find('.lc-progress').exists()).toBe(true)
    wrapper.unmount()
  })
})
