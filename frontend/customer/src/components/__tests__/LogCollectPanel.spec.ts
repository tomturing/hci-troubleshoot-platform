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

import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { h, inject, provide } from 'vue'
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
      // 抽屉宽度由工作区布局求解器给出，面板只读不再自持
      leftPanelWidth: 520,
      workspaceLayout: {
        mode: 'slide' as const,
        leftWidth: 520,
        rightWidth: 0,
        centerMaxWidth: 900,
        leftOffset: 520,
        rightOffset: 0,
      },
      setLeftPanelPreferred: vi.fn(),
      setPanelResizing: vi.fn(),
    },
  }
})

vi.mock('@/stores/chat', () => ({ useChatStore: () => mockStore }))

vi.mock('@/api/terminal', async () => {
  // 真实实现仅在“纯函数”上复用：消息构造与完整性解析都是纯数据变换，桩掉它们
  // 等于把“点重试有没有真的把 retry_stage 发给 bridge”从测试里抹掉。
  const actual = await vi.importActual<typeof import('@/api/terminal')>('@/api/terminal')
  return {
    ...actual,
    resolveBridgeUrl: vi.fn().mockResolvedValue('ws://localhost:9999'),
    createBridgeSocket: vi.fn(() => {
      // 真实 WebSocket 每次 new 都是一条**新**连接，不可能返回一个已 CLOSED 的实例。
      // 这里复用单例，就必须在每次「建连」时把 readyState 复位：前一个用例 unmount
      // 时 close() 会把它置成 3，残留的 3 会让后续挂载永远拿到一条已关闭的通道，
      // 于是 sendAndWait 依通道不可用立即失败、探测根本发不出去，描述表也就无从渲染。
      fakeSocket.readyState = 1
      // onopen 在构造之后才被赋值，必须延后一拍触发
      setTimeout(() => fakeSocket.onopen?.(), 0)
      return fakeSocket
    }),
    buildLogCollectMessage: (caseId: string, action: string, options: any = {}) =>
      JSON.stringify({
        type: 'log_collect',
        case_id: caseId,
        log_action: action,
        node_ip: options.nodeIp,
      }),
    // 交付动作走独立消息类型：载荷字段原样透传，用例才能断言 delivery_mode 是否送到 bridge
    buildDeliveryMessage: (action: string, caseId: string, options: any = {}) =>
      JSON.stringify({ type: 'delivery', case_id: caseId, log_action: action, ...options }),
    // submit 走独立入口（snake_case 载荷平铺）。mock 必须与真实实现同构，
    // 否则又会像上一轮那样把"字段被丢空"掩盖成绿灯。
    buildDeliverySubmitMessage: (payload: any, caseId: string, nodeIp?: string) =>
      JSON.stringify({ type: 'delivery_submit', case_id: caseId, node_ip: nodeIp, ...payload }),
  }
})

vi.mock('@/api/logCollect', () => ({
  logCollectApi: {
    createTask: vi.fn(),
    getTask: vi.fn(),
    cancelTask: vi.fn(),
    listTasks: vi.fn(),
    // 场景字典：默认返回空数组（= 平台未维护），由各用例按需覆盖
    listScenarios: vi.fn(),
    patchTask: vi.fn(),
    deliverTask: vi.fn(),
    getRelay: vi.fn(),
    getUploadStatus: vi.fn(),
  },
}))

const passthrough = (name: string, tag = 'div') => ({
  name,
  setup(_: any, { slots }: any) {
    return () => h(tag, slots.default?.())
  },
})

// label 是属性而非插槽，纯透传桩会把它丢掉，导致"字段顺序 / 字段文案"无法断言
const ElDescriptionsItem = {
  name: 'ElDescriptionsItem',
  props: ['label'],
  setup(props: any, { slots }: any) {
    return () =>
      h('div', { class: 'desc-item' }, [
        h('span', { class: 'desc-label' }, props.label),
        h('span', { class: 'desc-val' }, slots.default?.()),
      ])
  },
}

// ElAlert 的 title 是属性而非插槽，透传桩会丢掉它；这里显式渲染出来，
// 否则"完成态"一类文案无法在测试里断言。
// type 同样要落到 class：提示等级（常态 info / 需处理 warning）正是本文件要锁住的契约。
const ElAlert = {
  props: ['title', 'type'],
  setup(props: any, { slots }: any) {
    return () =>
      h('div', { class: ['alert-stub', `alert-${props.type}`] }, [props.title, slots.default?.()])
  },
}

// 只把结构相关的组件渲染成可断言的 DOM，其余保持透传
//
// 勾选框桩要真的能“点”：互斥规则（exclusive）的全部意义在于点了之后另一项被禁用，
// 若桩子只渲染个 label，就只能断言初始态，等于什么都没锁住。
// 因此这里用 provide/inject 复刻 Element Plus 的分组语义（选中集合 + 点击切换）。
const CBX_GROUP_KEY = Symbol('cbx-group')

const ElCheckboxGroup = {
  props: ['modelValue'],
  emits: ['update:modelValue'],
  setup(props: any, { slots, emit }: any) {
    provide(CBX_GROUP_KEY, {
      isChecked: (value: unknown) => (props.modelValue ?? []).includes(value),
      toggle: (value: unknown) => {
        const next = [...(props.modelValue ?? [])]
        const index = next.indexOf(value)
        if (index >= 0) next.splice(index, 1)
        else next.push(value)
        emit('update:modelValue', next)
      },
    })
    return () => h('div', { class: 'cbx-group' }, slots.default?.())
  },
}

const ElCheckbox = {
  props: ['value', 'label', 'modelValue', 'disabled'],
  setup(props: any, { slots }: any) {
    const group: any = inject(CBX_GROUP_KEY, null)
    return () =>
      h(
        'label',
        {
          class: ['cbx', { 'is-disabled': props.disabled === true }],
          'data-value': props.value ?? props.label,
          'data-checked': group?.isChecked(props.value) ? '1' : '0',
          'data-disabled': props.disabled ? '1' : '0',
          onClick: () => {
            if (props.disabled) return
            group?.toggle(props.value)
          },
        },
        slots.default?.(),
      )
  },
}

// 交付路径单选同理：P1/P2 是用户对交付链的取舍，而「选了 P1 就该升级告警」是硬契约，
// 桩子不能真点时这条规则就只能靠人工看屏。
const RADIO_GROUP_KEY = Symbol('radio-group')

const ElRadioGroup = {
  props: ['modelValue'],
  emits: ['update:modelValue', 'change'],
  setup(props: any, { slots, emit }: any) {
    provide(RADIO_GROUP_KEY, {
      isChecked: (value: unknown) => props.modelValue === value,
      select: (value: unknown) => {
        if (props.modelValue === value) return
        emit('update:modelValue', value)
        emit('change', value)
      },
    })
    return () => h('div', { class: 'radio-group' }, slots.default?.())
  },
}

const ElRadioButton = {
  props: ['value', 'disabled'],
  setup(props: any, { slots }: any) {
    const group: any = inject(RADIO_GROUP_KEY, null)
    return () =>
      h(
        'label',
        {
          class: ['radio-btn', { 'is-active': group?.isChecked(props.value) }],
          'data-value': props.value,
          'data-disabled': props.disabled ? '1' : '0',
          onClick: () => {
            if (props.disabled) return
            group?.select(props.value)
          },
        },
        slots.default?.(),
      )
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
          // 描述表要断言列数：窄抽屉必须退成单列，字段名才不会逐字竖排
          ElDescriptions: {
            name: 'ElDescriptions',
            props: ['column'],
            setup(props: any, { attrs, slots }: any) {
              return () =>
                h(
                  'div',
                  {
                    ...attrs,
                    class: ['desc-stub', attrs.class],
                    'data-column': String(props.column),
                  },
                  slots.default?.(),
                )
            },
          },
          ElDescriptionsItem,
          ElTag: passthrough('ElTag'),
          ElInput: passthrough('ElInput'),
          ElDatePicker: passthrough('ElDatePicker'),
          ElButton: passthrough('ElButton', 'button'),
          ElProgress: passthrough('ElProgress'),
          ElSteps: passthrough('ElSteps'),
          ElStep: passthrough('ElStep'),
          ElCheckboxGroup,
          ElCheckbox,
          ElRadioGroup,
          ElRadioButton,
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
    // 抽屉宽度用例会改写，逐例复位，避免用例间互相污染
    mockStore.leftPanelWidth = 520
    // 默认“该工单没有任何历史任务”，需要历史的任务用例自行 mock
    vi.mocked(logCollectApi.listTasks).mockReset()
    vi.mocked(logCollectApi.listTasks).mockResolvedValue({ total: 0, items: [] })
    // 默认“平台字典为空”：未维护时界面应回落 bridge 短名 + 给出降级提示
    vi.mocked(logCollectApi.listScenarios).mockReset()
    vi.mocked(logCollectApi.listScenarios).mockResolvedValue([])
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

  /**
   * 现场回归（2026-10-28，8×/天 LC-ENV-006）：
   * 桥侧 SSH 会话已销毁、但前端 sshConnectionState 仍显示 connected（面板这条 WS 未必是
   * 会话属主，可能没收到 ssh_disconnected）。用户点开始收集会反复误发请求拿 session_missing。
   * 契约：前端收到桥侧权威判定 LC-ENV-006 时，必须立即把 sshConnectionState 置否，
   * 让按钮禁用、提示出现，避免重复误发。
   */
  it('收到桥侧 LC-ENV-006 时立即把 sshConnectionState 置为 disconnected（自愈陈旧状态）', async () => {
    // 模拟 UI 仍显示已连接、但桥侧会话其实已销毁的陈旧状态
    mockStore.sshConnectionState = 'connected'
    mockStore.sshCurrentConfig = { host: '172.28.25.1', port: 22, username: 'root' }

    mountPanel()
    await flushPromises()
    await new Promise((resolve) => setTimeout(resolve, 5))
    await flushPromises()

    // 桥权威判定：会话不存在
    fakeSocket.onmessage?.({
      data: JSON.stringify({
        type: 'log_collect_result',
        log_action: 'create',
        status: 'error',
        error_code: 'LC-ENV-006',
        message: 'SSH 会话不存在',
      }),
    })
    await flushPromises()

    expect(mockStore.sshConnectionState).toBe('disconnected')
  })

  // 面板化之后外壳（标题/收起/拖拽）由 PanelShell 负责，
  // 本组件只渲染内容——再出现 el-drawer 就说明有人把抽屉外壳加回来了。
  it('不再自带抽屉外壳，只渲染面板内容', async () => {
    const wrapper = mountPanel()
    await flushPromises()

    expect(wrapper.find('.drawer-stub').exists()).toBe(false)
    expect(wrapper.find('.lc-panel').exists()).toBe(true)
    // 底部操作区走全局统一规范（.panel-composer），不再自造 .lc-footer
    expect(wrapper.find('.lc-footer').exists()).toBe(false)
    expect(wrapper.find('.panel-composer').exists()).toBe(true)
  })

  it('工具条提供「刷新进度」「重新探测」两个常驻入口，「关闭」由外壳的收起按钮承担', async () => {
    const wrapper = mountPanel()
    await flushPromises()

    const bar = wrapper.find('.lc-case-bar')
    expect(bar.text()).toContain('归档工单')
    expect(bar.text()).toContain('刷新进度')
    // 探测结论是快照：入口必须常驻在工具条，而不是埋在探测表下方（现场找不到，只能 F5）
    expect(bar.text()).toContain('重新探测')
    // 收起动作由 PanelShell 提供，组件内不该再出现"关闭"按钮导致两个入口
    expect(bar.text()).not.toContain('关闭')
    expect(bar.text()).not.toContain('钉住')
  })

  // ── 探测重试与事实复查（2026-10-30 现场：装完 acli 必须 F5 面板才更新）──

  const probePayload = (patch: Record<string, unknown> = {}) => ({
    architecture: 'x86_64',
    acli_installed: true,
    acli_version: '1.0.1',
    acli_build_time: '2026-09-20 17:22:30',
    acli_binary: '/sf/bin/acli',
    available_mb: 540088,
    log_server_reachable: true,
    http_code: 200,
    modules: [{ value: 'vm_suspend', label: '虚拟机挂起专项' }],
    hosts: [{ ip: '10.122.216.222', name: 'host', master: true }],
    catalog_source: 'acli_help',
    ...patch,
  })

  const replyProbe = (payload: Record<string, unknown>) => {
    fakeSocket.onmessage?.({
      data: JSON.stringify({ type: 'log_collect_result', log_action: 'probe', ...payload }),
    })
  }

  const probeCount = () =>
    sentMessages
      .map((raw) => JSON.parse(raw))
      .filter((message) => message.log_action === 'probe').length

  const connectSsh = () => {
    mockStore.sshConnectionState = 'connected'
    mockStore.sshCurrentConfig = { host: '172.28.25.1', port: 22, username: 'root' }
  }

  it('瞬态失败自动重试：第二次探测成功后更新界面，成功后停止重试', async () => {
    connectSsh()
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const wrapper = mountPanel()
    try {
      await vi.advanceTimersByTimeAsync(5)
      await flushPromises()
      expect(probeCount()).toBe(1)

      // 第一次：桥侧超时（瞬态失败）
      replyProbe({ status: 'error', message: 'probe 超时（90s）' })
      await flushPromises()
      // 重试必须可见：否则用户分不清「没反应」与「正在重试」（这正是现场误判）
      expect(wrapper.text()).toContain('自动重试（第 1/3 次）')

      // 2s 退避后第二次探测成功
      await vi.advanceTimersByTimeAsync(3000)
      await flushPromises()
      expect(probeCount()).toBe(2)
      replyProbe({ status: 'ok', log_data: probePayload() })
      await flushPromises()
      await vi.advanceTimersByTimeAsync(10)
      await flushPromises()

      expect(wrapper.text()).toContain('1.0.1')
      expect(wrapper.text()).toContain('1 个')
      // 成功即收敛：不得再发第三次
      await vi.advanceTimersByTimeAsync(20000)
      expect(probeCount()).toBe(2)
    } finally {
      wrapper.unmount()
      vi.useRealTimers()
    }
  })

  // 反向用例：重试不能把「重连 SSH 才能解决」的失败也重放一遍，那是纯噪音
  it('会话缺失（LC-ENV-006）属永久失败，不进入自动重试', async () => {
    connectSsh()
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const wrapper = mountPanel()
    try {
      await vi.advanceTimersByTimeAsync(5)
      await flushPromises()
      expect(probeCount()).toBe(1)

      replyProbe({ status: 'error', error_code: 'LC-ENV-006', message: 'SSH 会话不存在' })
      await flushPromises()
      await vi.advanceTimersByTimeAsync(20000)
      expect(probeCount()).toBe(1)
    } finally {
      wrapper.unmount()
      vi.useRealTimers()
    }
  })

  // 现场核心场景：探测发生在「acli 还没装」之前，装好后界面必须自动点亮，不需要 F5
  it('acli 在探测之后才装好时，事实复查自动更新界面并收敛', async () => {
    connectSsh()
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const wrapper = mountPanel()
    try {
      await vi.advanceTimersByTimeAsync(5)
      await flushPromises()

      // 首次探测：acli 未安装、节点清单为空（现场 09:50 的真实状态）
      replyProbe({
        status: 'ok',
        log_data: probePayload({
          acli_installed: false,
          acli_version: '',
          acli_binary: '',
          hosts: [],
          catalog_source: 'platform_default',
          catalog_note: 'acli 未安装，使用平台内置场景目录',
        }),
      })
      await flushPromises()
      expect(wrapper.text()).toContain('acli 未安装')

      // 30s 后自动复查：此时用户已在终端装好 acli
      await vi.advanceTimersByTimeAsync(31000)
      await flushPromises()
      expect(probeCount()).toBe(2)
      replyProbe({ status: 'ok', log_data: probePayload() })
      await flushPromises()
      await vi.advanceTimersByTimeAsync(10)
      await flushPromises()

      expect(wrapper.text()).not.toContain('acli 未安装，无法自动读取节点清单')

      // 成功标准达成（通道通 + acli 已装 + 节点清单非空）后不再复查
      await vi.advanceTimersByTimeAsync(120000)
      expect(probeCount()).toBe(2)
    } finally {
      wrapper.unmount()
      vi.useRealTimers()
    }
  })

  // 回归：两抽屉同时展开时左栏被压到 500px 上下，
  // descriptions 固定 2 列会把「发起节点」压成一字一行。列数必须跟随抽屉宽度。
  it('描述表列数随抽屉宽度切换：窄容器单列、宽容器两列', async () => {
    const probeResult = {
      architecture: 'x86_64',
      acli_installed: true,
      acli_version: '1.0.0',
      available_mb: 1024,
      hosts: [],
    }
    // 描述表只在 probe 成功后渲染：先推一帧探测结果，再断言列数
    const mountAndProbe = async (width: number) => {
      mockStore.leftPanelWidth = width
      const wrapper = mountPanel()
      await flushPromises()
      await new Promise((resolve) => setTimeout(resolve, 5))
      fakeSocket.onmessage?.({
        data: JSON.stringify({
          type: 'log_collect_result',
          log_action: 'probe',
          status: 'ok',
          log_data: probeResult,
        }),
      })
      await flushPromises()
      return wrapper
    }

    const narrow = await mountAndProbe(460)
    const narrowDescs = narrow.findAll('.desc-stub')
    expect(narrowDescs).toHaveLength(1)
    expect(narrowDescs[0].attributes('data-column')).toBe('1')
    narrow.unmount()

    const wide = await mountAndProbe(620)
    const wideDescs = wide.findAll('.desc-stub')
    expect(wideDescs).toHaveLength(1)
    expect(wideDescs[0].attributes('data-column')).toBe('2')
    wide.unmount()
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

  // ── 故障场景字典（平台维护的文案）────────────────────────────────────────
  // 这一组锁的是「能力边界在集群、展示文案在平台」这个分层：
  // 早期界面写着“目录来自集群 acli”，让客户以为连场景说明也是集群给的。

  // 合成字典：线上 seed 里**没有任何互斥项**（other 已确认为叠加语义），
  // 这里故意把 other 置 exclusive 只为锁住“互斥机制按字典数据驱动”这条能力。
  const scenarioDictFixture = [
    {
      value: 'other',
      label: '默认收集专项',
      summary: '不收集上述场景日志',
      collects: '基础系统日志',
      typical_symptoms: '尚未定位到具体方向',
      duration_hint: null,
      exclusive: true,
      sort_order: 10,
      enabled: true,
      review_state: 'approved' as const,
      updated_by: null,
      updated_at: null,
    },
    {
      value: 'vm_suspend',
      label: '虚拟机挂起恢复专项',
      summary: '挂起失败或恢复失败的专项日志',
      collects: 'VMS 进程日志、虚拟机配置',
      typical_symptoms: '虚拟机卡在挂起中',
      duration_hint: '单天约 4 分钟',
      exclusive: false,
      sort_order: 20,
      enabled: true,
      review_state: 'approved' as const,
      updated_by: null,
      updated_at: null,
    },
  ]

  async function mountAndProbe({
    modules,
    catalogSource = 'acli_help',
  }: {
    modules: Array<{ value: string; label: string }>
    catalogSource?: string
  }) {
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
          catalog_source: catalogSource,
          modules,
          hosts: [],
        },
      }),
    })
    await flushPromises()
    return wrapper
  }

  it('字典合并：能力边界取 probe，文案与顺序取字典，未维护项追加在尾部', async () => {
    vi.mocked(logCollectApi.listScenarios).mockResolvedValue(scenarioDictFixture)
    const wrapper = await mountAndProbe({
      modules: [
        { value: 'vm_suspend', label: '虚拟机挂起专项' },
        { value: 'network', label: '网络专项' },
        { value: 'other', label: '默认收集专项（不收集上述场景日志）' },
      ],
    })

    const items = wrapper.find('.lc-module-group').findAll('.cbx')
    // 顺序取字典 sort_order（other=10 先于 vm_suspend=20），字典缺失项按 probe 原序排尾
    expect(items.map((item) => item.attributes('data-value'))).toEqual([
      'other',
      'vm_suspend',
      'network',
    ])
    // 文案取字典（不再是 bridge 的短名）
    expect(wrapper.text()).toContain('虚拟机挂起恢复专项')
    expect(wrapper.text()).toContain('挂起失败或恢复失败的专项日志')
    // 字典没维护的枚举必须被点名，不能默默显示一个无说明的短名
    expect(wrapper.text()).toContain('1 个场景平台尚未维护说明')
  })

  it('能力边界只认 probe：字典有、集群没返回的枚举不得展示', async () => {
    vi.mocked(logCollectApi.listScenarios).mockResolvedValue(scenarioDictFixture)
    const wrapper = await mountAndProbe({
      modules: [{ value: 'vm_suspend', label: '虚拟机挂起专项' }],
    })

    const values = wrapper
      .find('.lc-module-group')
      .findAll('.cbx')
      .map((item) => item.attributes('data-value'))
    // other 只在字典里、不在集群目录里 ⇒ 展示它等于承诺一个 create 必然失败的选项
    expect(values).toEqual(['vm_suspend'])
  })

  it('exclusive 双向互斥：选中普通项时互斥项禁用，选中互斥项时其它项全禁用', async () => {
    vi.mocked(logCollectApi.listScenarios).mockResolvedValue(scenarioDictFixture)
    const wrapper = await mountAndProbe({
      modules: [
        { value: 'vm_suspend', label: '虚拟机挂起专项' },
        { value: 'other', label: '默认收集专项' },
      ],
    })

    const group = wrapper.find('.lc-module-group')
    const other = group.find('.cbx[data-value="other"]')
    const suspend = group.find('.cbx[data-value="vm_suspend"]')

    // 默认勾选 vm_suspend（首个非互斥项）⇒ other 必须禁用
    expect(suspend.attributes('data-checked')).toBe('1')
    expect(other.attributes('data-disabled')).toBe('1')

    // 取消勾选全部具体专项后，互斥项才重新可选（空选语义）
    await suspend.trigger('click')
    await flushPromises()
    expect(group.find('.cbx[data-value="other"]').attributes('data-disabled')).toBe('0')

    // 勾上 other ⇒ 具体专项反过来被禁用，两个方向都不能同时成立
    await group.find('.cbx[data-value="other"]').trigger('click')
    await flushPromises()
    expect(group.find('.cbx[data-value="other"]').attributes('data-checked')).toBe('1')
    expect(group.find('.cbx[data-value="vm_suspend"]').attributes('data-disabled')).toBe('1')
  })

  it('字典为空时回落 bridge 短名，并说明平台暂未维护', async () => {
    const wrapper = await mountAndProbe({
      modules: [{ value: 'vm_suspend', label: '虚拟机挂起专项' }],
    })

    expect(wrapper.find('.lc-module-group').text()).toContain('虚拟机挂起专项')
    expect(wrapper.text()).toContain('平台场景字典暂未维护')
  })

  it('字典接口失败不阻塞探测：仍能完成探测并给出降级说明', async () => {
    vi.mocked(logCollectApi.listScenarios).mockRejectedValue(new Error('503'))
    const wrapper = await mountAndProbe({
      modules: [{ value: 'vm_suspend', label: '虚拟机挂起专项' }],
    })

    // 探测结论仍然落地（右侧“环境探测”区），只是说明文案退化
    expect(wrapper.find('.lc-probe').text()).toContain('1.0.1')
    expect(wrapper.text()).toContain('场景说明暂不可用')
    expect(wrapper.find('.lc-module-group').findAll('.cbx').length).toBe(1)
  })

  it('来源文案拆成能力与文案两句，不再出现“目录来自集群 acli”', async () => {
    vi.mocked(logCollectApi.listScenarios).mockResolvedValue(scenarioDictFixture)
    const wrapper = await mountAndProbe({
      modules: [{ value: 'vm_suspend', label: '虚拟机挂起专项' }],
    })

    const text = wrapper.text()
    expect(text).not.toContain('目录来自集群')
    expect(text).toContain('可选场景由集群 acli 提供')
    expect(text).toContain('场景说明来自平台字典')
  })

  it('空选时提示按默认收集专项下发', async () => {
    vi.mocked(logCollectApi.listScenarios).mockResolvedValue(scenarioDictFixture)
    const wrapper = await mountAndProbe({
      modules: [{ value: 'vm_suspend', label: '虚拟机挂起专项' }],
    })

    const suspend = wrapper.find('.lc-module-group .cbx[data-value="vm_suspend"]')
    expect(suspend.attributes('data-checked')).toBe('1')
    await suspend.trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('未选择场景时按「默认收集专项」下发')
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

  // 回归：acli 版本必须显示完整事实（版本 + 编译时间），并说明这份结论来自哪个二进制。
  // 现场问题：面板只显示 1.0.0，运维手敲 `acli --version` 是 1.0.1，无法判断差异来源。
  it('acli 展示版本 + build，并附上实际命中的二进制路径', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    await new Promise((resolve) => setTimeout(resolve, 5))

    fakeSocket.onmessage?.({
      data: JSON.stringify({
        type: 'log_collect_result',
        log_action: 'probe',
        status: 'ok',
        log_data: {
          architecture: 'aarch64',
          acli_installed: true,
          acli_version: '1.0.1',
          acli_build_time: '2026-09-15 21:00:04',
          acli_binary: '/sf/bin/acli',
          available_mb: 60989,
          log_server_reachable: true,
          http_code: 200,
          catalog_source: 'acli_help',
          modules: [{ value: 'vm_suspend', label: '虚拟机挂起专项' }],
          hosts: [],
        },
      }),
    })
    await flushPromises()

    const probe = wrapper.find('.lc-probe')
    expect(probe.text()).toContain('1.0.1 (build 2026-09-15 21:00:04)')
    expect(wrapper.text()).toContain('/sf/bin/acli')
    expect(wrapper.text()).toContain('重新探测')
  })

  it('后端未回传 build 时只显示版本号，不出现 undefined', async () => {
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
          acli_version: '1.0.0',
          available_mb: 1024,
          log_server_reachable: false,
          http_code: 0,
          catalog_source: 'platform_default',
          modules: [{ value: 'vm_suspend', label: '虚拟机挂起专项' }],
          hosts: [],
        },
      }),
    })
    await flushPromises()

    const probe = wrapper.find('.lc-probe').text()
    expect(probe).toContain('1.0.0')
    expect(probe).not.toContain('undefined')
    expect(probe).not.toContain('build')
  })

  // 回归：节点清单唯一来源是 `acli platform node list`，acli 未安装时清单必然为空。
  // 旧文案「请按 acli -i 格式手填 IP」把内部参数名写给用户，既没说清原因，也没给可照抄的格式。
  it('acli 未安装导致清单为空时，提示给的是原因与填写格式，不是内部参数名', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    await new Promise((resolve) => setTimeout(resolve, 5))

    fakeSocket.onmessage?.({
      data: JSON.stringify({
        type: 'log_collect_result',
        log_action: 'probe',
        status: 'ok',
        log_data: {
          architecture: 'aarch64',
          acli_installed: false,
          acli_version: '',
          available_mb: 61020,
          log_server_reachable: true,
          http_code: 200,
          catalog_source: 'platform_default',
          modules: [{ value: 'vm_suspend', label: '虚拟机挂起专项' }],
          hosts: [],
        },
      }),
    })
    await flushPromises()

    const hint = wrapper.findAll('.lc-hint').map((node) => node.text()).join(' | ')
    expect(hint).not.toContain('acli -i')
    // 必须给出可直接照抄的示例，而不是让用户猜格式
    expect(hint).toContain('172.28.25.2,172.28.25.3')
    // 必须说清"为什么没有清单"，避免用户误判为平台故障
    expect(hint).toContain('acli 未安装')
  })

  // 「建议路径」取值的唯一出口：表格里的那一格。取值定死两个字符串，
  // 不加「（建议）」「待探测」之类修饰——探测结论已经由「是哪一个」说清楚。
  const suggestedPathText = (wrapper: any): string => {
    const cell = wrapper
      .findAll('.desc-item')
      .find((node: any) => node.find('.desc-label').text() === '建议路径')
    return cell ? cell.find('.desc-val').text() : ''
  }

  // 回归（Q2026102801062）：内网 HCI 连不上日志服务器是最常见的现场，不是故障。
  // 契约：探测结果只体现在「建议路径」的取值 + 交付路径的默认值/可选性上，
  // 不再额外弹一条需要用户判读的告警。
  it('日志服务器不可达时默认选 P2 且 P1 置灰，不额外弹告警', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    await new Promise((resolve) => setTimeout(resolve, 5))

    replyProbe({
      status: 'ok',
      log_data: probePayload({
        log_server_reachable: false,
        log_server_http_code: 0,
        note: 'curl 探测失败（连续 3 次）：000 5.001368',
      }),
    })
    await flushPromises()

    // 不能多出第二条文案：探测结果只靠「建议路径」说话
    const alerts = wrapper.findAll('.lc-prepare').map((node) => node.text())
    expect(alerts.some((text) => text.includes('日志服务器'))).toBe(false)
    expect(suggestedPathText(wrapper)).toBe('P2 经平台中转')

    // 默认落在 P2；P1 因为必然失败而被禁选（点了也不变）
    expect(wrapper.find('.radio-btn[data-value="P2"]').classes()).toContain('is-active')
    const p1 = wrapper.find('.radio-btn[data-value="P1"]')
    expect(p1.attributes('data-disabled')).toBe('1')
    await p1.trigger('click')
    expect(wrapper.find('.radio-btn[data-value="P1"]').classes()).not.toContain('is-active')
    expect(wrapper.find('.radio-btn[data-value="P2"]').classes()).toContain('is-active')
  })

  it('日志服务器可达时默认选 P1，用户改选 P2 后不被重探覆盖', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    await new Promise((resolve) => setTimeout(resolve, 5))

    replyProbe({ status: 'ok', log_data: probePayload({ log_server_http_code: 200 }) })
    await flushPromises()

    expect(suggestedPathText(wrapper)).toBe('P1 直传日志服务器')
    expect(wrapper.find('.radio-btn[data-value="P1"]').classes()).toContain('is-active')
    // 可达才能手选：P2 是备选，不是默认
    expect(wrapper.find('.radio-btn[data-value="P1"]').attributes('data-disabled')).toBe('0')

    await wrapper.find('.radio-btn[data-value="P2"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('.radio-btn[data-value="P2"]').classes()).toContain('is-active')

    // 再次探测（结论不变）：用户已明确选的路径不能被默认值改回去
    const reprobe = wrapper.findAll('button').find((item) => item.text() === '重新探测')
    await reprobe!.trigger('click')
    await flushPromises()
    replyProbe({ status: 'ok', log_data: probePayload({ log_server_http_code: 200 }) })
    await flushPromises()
    expect(wrapper.find('.radio-btn[data-value="P2"]').classes()).toContain('is-active')
  })

  // 5xx 也归入「不可达」：「所有异常一律 P2 + P1 置灰」不能让用户去分辨 HTTP 码
  it('日志服务器回 5xx 时同样默认 P2、P1 置灰，不向用户解释 HTTP 码', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    await new Promise((resolve) => setTimeout(resolve, 5))

    replyProbe({
      status: 'ok',
      log_data: probePayload({ log_server_reachable: false, log_server_http_code: 502 }),
    })
    await flushPromises()

    expect(suggestedPathText(wrapper)).toBe('P2 经平台中转')
    expect(wrapper.find('.radio-btn[data-value="P2"]').classes()).toContain('is-active')
    expect(wrapper.find('.radio-btn[data-value="P1"]').attributes('data-disabled')).toBe('1')
    // HTTP 码属于排障信息，不进入面板文案
    expect(wrapper.text()).not.toContain('HTTP 502')
  })

  // 回归：探测先判不可达（程序置 P2）后又查到可达时，默认值必须回到 P1。
  // 旧实现用 `!pathMode` 做门槛，会永久停在比实际能力差一跳的 P2 上。
  it('重探恢复可达后，未被手改的默认路径回到 P1', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    await new Promise((resolve) => setTimeout(resolve, 5))

    replyProbe({
      status: 'ok',
      log_data: probePayload({ log_server_reachable: false, log_server_http_code: 0 }),
    })
    await flushPromises()
    expect(wrapper.find('.radio-btn[data-value="P2"]').classes()).toContain('is-active')

    const reprobe = wrapper.findAll('button').find((item) => item.text() === '重新探测')
    await reprobe!.trigger('click')
    await flushPromises()
    replyProbe({ status: 'ok', log_data: probePayload({ log_server_http_code: 200 }) })
    await flushPromises()

    expect(suggestedPathText(wrapper)).toBe('P1 直传日志服务器')
    expect(wrapper.find('.radio-btn[data-value="P1"]').classes()).toContain('is-active')
    expect(wrapper.find('.radio-btn[data-value="P1"]').attributes('data-disabled')).toBe('0')
  })

  // 「（建议）」「待探测」这类修饰一旦回到表格里，说明取值不再定死两个。
  it('建议路径的取值不带任何修饰词', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    await new Promise((resolve) => setTimeout(resolve, 5))

    replyProbe({ status: 'ok', log_data: probePayload({ log_server_http_code: 200 }) })
    await flushPromises()

    expect(suggestedPathText(wrapper)).toBe('P1 直传日志服务器')
    expect(wrapper.text()).not.toContain('（建议）')
    expect(wrapper.text()).not.toContain('待探测')
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
    expect(result.text()).toContain('P2 经平台中转')
    expect(result.text()).toContain('全部在线节点')
    expect(result.text()).toContain('虚拟机挂起专项')
    expect(result.text()).toContain('/sf/data/local/acli/tasks/Q2026102300001.tar.gz')

    const labels = wrapper.findAll('button').map((button) => button.text())
    expect(labels).toContain('重新收集')
    // 完成态下不应把"再收一次"当成默认动作
    expect(labels).not.toContain('开始收集')
    wrapper.unmount()
  })

  /**
   * 现场回归（Q2026102701193，2026-10-27）：
   * 用户 16:40 发起 P2 采集（16:45 failed），而更早 10:03 发起的 P1 任务 18:05 才完成。
   * 面板原先取"任意一条 completed"，于是展示早上那条 P1 的"已由插件直传"，
   * 用户看到交付路径是 P1，直接质疑"我明明选的 P2，你怎么判的"。
   *
   * 契约：展示**最近一次发起**的终态任务；标题按真实结论渲染；给出记录切换入口。
   */
  it('最新发起的任务失败时展示失败那条，而不是更早的完成记录', async () => {
    const oldCompleted = taskFixture({
      task_id: 'task-old',
      status: 'completed',
      created_at: '2026-10-27T10:03:00+08:00',
      finished_at: '2026-10-27T18:05:00+08:00',
      upload_mode: 'acli_direct',
      remote_url: 'http://acli.sangfor.com.cn:5888',
      delivery_status: 'delivered',
    } as Partial<LogCollectTask>)
    const newFailed = taskFixture({
      task_id: 'task-new',
      status: 'failed',
      created_at: '2026-10-27T16:40:00+08:00',
      finished_at: '2026-10-27T16:45:00+08:00',
      upload_mode: 'bridge_relay',
      delivery_status: 'queued',
    } as Partial<LogCollectTask>)
    vi.mocked(logCollectApi.listTasks).mockResolvedValue({ total: 2, items: [oldCompleted, newFailed] })

    const wrapper = mountPanel()
    await flushPromises()
    await flushPromises()

    const result = wrapper.find('.lc-result')
    expect(result.exists()).toBe(true)
    // 标题必须是"失败"，否则用户会以为自己这次成功了
    expect(result.text()).toContain('该工单上次采集失败')
    expect(result.text()).not.toContain('已完成日志收集')
    // 交付路径必须来自最新那条（P2），这正是用户质疑的点
    expect(result.text()).toContain('P2 经平台中转')
    // queued 必须显式化，不能只显示"尚未交付"
    expect(result.text()).toContain('交付已登记但未完成')
    // 记录切换入口：让用户能确认面板说的到底是哪一次
    expect(result.text()).toContain('展示记录')
    expect(wrapper.findAll('button').map((b) => b.text()).join('|')).toContain('P2')
    wrapper.unmount()
  })

  /**
   * 现场回归（截图二，2026-10-28）：P1 采集中途失败（66%、5/6 节点失败），
   * 平台判 failed。结果卡原先按 upload_mode=acli_direct 把"无产物"讲成
   * "直传后压缩包已删除 / 由插件上传后自动清理"——可 P1 从头到尾没发生，
   * 这是把失败解释成"成功后的清理"。契约：失败任务必须按终态说，不套用 P1 成功叙事。
   */
  it('失败的 P1 任务不把无产物讲成"直传后已删除"', async () => {
    const failedP1 = taskFixture({
      task_id: 'task-failed-p1',
      status: 'failed',
      created_at: '2026-10-28T09:50:00+08:00',
      finished_at: '2026-10-28T09:55:00+08:00',
      upload_mode: 'acli_direct',
      delivery_status: undefined,
      archive_size: 0,
      archive_path: '',
    } as Partial<LogCollectTask>)
    vi.mocked(logCollectApi.listTasks).mockResolvedValue({ total: 1, items: [failedP1] })

    const wrapper = mountPanel()
    await flushPromises()
    await flushPromises()

    const result = wrapper.find('.lc-result')
    expect(result.exists()).toBe(true)
    const text = result.text()
    // 失败终态：这三条必须按"失败"措辞，禁止 P1 成功叙事
    expect(text).toContain('本次采集失败，未产出压缩包')
    expect(text).toContain('采集未成功，无产物')
    expect(text).toContain('采集失败，未归档')
    // 不得再出现把失败解释成"上传成功后自删"的话术
    expect(text).not.toContain('直传后压缩包已删除')
    expect(text).not.toContain('由插件上传后自动清理')
    wrapper.unmount()
  })

  // 现场反馈：产物位置是长路径，排在右列会把行高撑成两行且右侧留白；
  // 换成「产物位置在左、归档状态在右」后路径靠左读，短文案收口在右。
  it('结果态字段顺序把产物位置排在归档状态之前', async () => {
    vi.mocked(logCollectApi.listTasks).mockResolvedValue({ total: 1, items: [taskFixture()] })

    const wrapper = mountPanel()
    await flushPromises()
    await flushPromises()

    const labels = wrapper.findAll('.desc-label').map((node) => node.text())
    // 完整性排最后：它是"归档之后还有没有对账"的补充结论，不能挤占产物/归档主信息。
    expect(labels.slice(-3)).toEqual(['产物位置', '归档状态', '完整性'])
    // 大小正常展示成 MB，不再落到「未记录」
    expect(wrapper.text()).toContain('100.0 MB')
    wrapper.unmount()
  })

  // 对抗性审查：没有基线时必须显性说"未校验"，不能留空或写成已校验——
  // 空白会被用户读成"没问题"，而这正是旧链路最大的隐患。
  it('完整性未校验时给出显性否定文案', async () => {
    vi.mocked(logCollectApi.listTasks).mockResolvedValue({ total: 1, items: [taskFixture()] })

    const wrapper = mountPanel()
    await flushPromises()
    await flushPromises()

    expect(wrapper.text()).toContain('未校验源包')
    expect(wrapper.text()).not.toContain('已校验：源包摘要与平台副本一致')
    wrapper.unmount()
  })

  it('完整性 verified 时展示已校验', async () => {
    vi.mocked(logCollectApi.listTasks).mockResolvedValue({
      total: 1,
      items: [taskFixture({ integrity_state: 'verified' })],
    })

    const wrapper = mountPanel()
    await flushPromises()
    await flushPromises()

    expect(wrapper.text()).toContain('已校验：源包摘要与平台副本一致')
    wrapper.unmount()
  })

  // P1 的大小只能在上传窗口内 stat 到，插件删包后就永久缺失；
  // 文案必须说明原因，否则用户会以为平台漏采。
  it('P1 直传未量到压缩包大小时给出原因文案', async () => {
    vi.mocked(logCollectApi.listTasks).mockResolvedValue({
      total: 1,
      items: [
        taskFixture({
          upload_mode: 'acli_direct',
          remote_url: 'http://172.28.25.1:5888/upload',
          archive_path: '/sf/data/local/acli/tmp/data/log_collect/Q2026102300001.zip',
          archive_size: null,
          archive_sha256: null,
          relay_status: 'none',
        }),
      ],
    })

    const wrapper = mountPanel()
    await flushPromises()
    await flushPromises()

    const sizeItem = wrapper.findAll('.desc-item').find((node) => node.text().includes('压缩包大小'))
    expect(sizeItem?.text()).toContain('未记录（直传后压缩包已删除）')
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

  // 回归：进度阶段条原先渲染在表单最底部，抽屉内容一长（探测表 + 表单 + 对账提示）
  // 就得往下滚才能看见"到哪一步了"，等于看不到。位置必须在集群信息表格之后、表单之前。
  it('阶段条位于集群信息表格之后、收集表单之前', async () => {
    const running = taskFixture({ task_id: 'task-running', status: 'collecting', progress: 42, finished_at: null })
    vi.mocked(logCollectApi.listTasks).mockResolvedValue({ total: 1, items: [running] })
    vi.mocked(logCollectApi.getTask).mockResolvedValue(running)

    const wrapper = mountPanel()
    await flushPromises()
    await new Promise((resolve) => setTimeout(resolve, 5))

    // 先把集群信息表格渲染出来
    fakeSocket.onmessage?.({
      data: JSON.stringify({
        type: 'log_collect_result',
        log_action: 'probe',
        status: 'ok',
        log_data: {
          architecture: 'x86_64',
          acli_installed: true,
          acli_version: '1.0.1',
          available_mb: 10240,
          log_server_reachable: true,
          http_code: 200,
          catalog_source: 'acli_help',
          modules: [{ value: 'vm_suspend', label: '虚拟机挂起专项' }],
          hosts: [],
        },
      }),
    })
    await flushPromises()

    const probeTable = wrapper.find('.lc-probe').element
    const progress = wrapper.find('.lc-progress').element
    const formGroup = wrapper.find('.lc-module-group').element

    // 表格 → 阶段条 → 表单，顺序不能颠倒
    expect(probeTable.compareDocumentPosition(progress) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(progress.compareDocumentPosition(formGroup) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    wrapper.unmount()
  })

  // 2026-10-23 之前完成的任务没有 upload_mode/archive_path（字段是后补的），
  // 结果态必须按 remote_url 兜底成 P1，而不是把内部枚举 none 摆给用户。
  it('老记录缺 upload_mode 时按 remote_url 兜底展示 P1，不暴露内部枚举', async () => {
    vi.mocked(logCollectApi.listTasks).mockResolvedValue({
      total: 1,
      items: [
        taskFixture({
          upload_mode: null,
          archive_path: null,
          archive_size: null,
          archive_sha256: null,
          relay_status: 'none',
          remote_url: 'http://acli.sangfor.com.cn:5888',
        }),
      ],
    })

    const wrapper = mountPanel()
    await flushPromises()
    await flushPromises()

    const text = wrapper.find('.lc-result').text()
    expect(text).toContain('P1 直传日志服务器')
    expect(text).toContain('已由插件直传日志服务器')
    expect(text).toContain('由插件上传后自动清理')
    expect(text).not.toContain('none')
    wrapper.unmount()
  })

  /**
   * 现场回归（工单 Q2026101401683 / 2026-10-26 20:38）：
   * 点「重新收集」后新任务 2.8 秒就被写成 completed，节点上其实还在 0% 采集。
   * 根因是 create 之后 acli 仍打印**上一次任务**的完成报告（两次报告 raw 完全相同），
   * 而旧判据在"包时间缺失（P1 上传后自删）+ total 恰好等于目标节点数"时直接放行。
   *
   * 这里锁定：下发后报告没刷新 → 绝不写 completed。
   */
  it('下发后节点仍打印上一次的完成报告时，不把平台写成 completed', async () => {
    mockStore.sshConnectionState = 'connected'
    mockStore.sshCurrentConfig = { host: '172.28.25.1', port: 22, username: 'root' }
    // 上一次任务的完成报告原文：下发前后一模一样
    const staleReport = '状态：收集任务已完成！\n归档状态：发送成功'
    const staleSnapshot = {
      status: '收集任务已完成！',
      percent: 100,
      done: 1,
      total: 1,
      node_state: 'completed',
      archive_path: '/sf/data/local/acli/tmp/data/log_collect/Q2026102300001.zip',
      upload_state: '发送成功',
      hosts: [{ ip: '172.28.25.1', status: '收集完成', percent: 100 }],
      raw: staleReport,
    }
    const created = taskFixture({ status: 'pending', progress: 0, finished_at: null, archive_path: null })
    vi.mocked(logCollectApi.createTask).mockResolvedValue(created)
    vi.mocked(logCollectApi.patchTask).mockResolvedValue({ ...created, status: 'collecting' })

    const wrapper = mountPanel()
    await flushPromises()
    await new Promise((resolve) => setTimeout(resolve, 5))

    // 探测：1 台主机 + P1 可达（与现场一致）
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
          modules: [{ value: 'network', label: '网络专项' }],
          hosts: [{ ip: '172.28.25.1', name: 'node-1', master: true }],
        },
      }),
    })
    await flushPromises()

    const start = wrapper.findAll('button').find((item) => item.text() === '开始收集')
    expect(start).toBeTruthy()
    await start!.trigger('click')
    await flushPromises()

    // 1) 下发前的对账读到的就是这份旧完成报告 → 它成为基线
    fakeSocket.onmessage?.({
      data: JSON.stringify({ type: 'log_collect_result', log_action: 'progress', status: 'ok', log_data: staleSnapshot }),
    })
    await flushPromises()

    // 2) create 成功
    fakeSocket.onmessage?.({
      data: JSON.stringify({ type: 'log_collect_result', log_action: 'create', status: 'ok', log_data: {} }),
    })
    await flushPromises()

    // 3) 下发后的第一次 progress —— 报告与基线完全相同
    fakeSocket.onmessage?.({
      data: JSON.stringify({ type: 'log_collect_result', log_action: 'progress', status: 'ok', log_data: staleSnapshot }),
    })
    await flushPromises()

    const completedPatches = vi
      .mocked(logCollectApi.patchTask)
      .mock.calls.filter(([, body]: any) => body?.status === 'completed')
    expect(completedPatches).toHaveLength(0)
    // 必须给出"为什么还在等"的可见理由，而不是静默卡住
    expect(wrapper.text()).toContain('上一次任务')
    wrapper.unmount()
  })

  /**
   * 现场回归（工单 Q2026101401683 / 2026-10-26 21:57）：
   * 节点 100% 完成、P1 直传成功、压缩包被插件自删（拿不到任何 mtime 证据）。
   * 旧判据要求"完成必须自证归属"，于是平台永远停在 collecting、面板提示"继续等待"。
   * 新契约：接管态下节点报告不是下发前那份，节点说完成就是完成。
   */
  it('接管态下节点已完成（压缩包已被插件删除）时，自动把平台修正为 completed', async () => {
    mockStore.sshConnectionState = 'connected'
    mockStore.sshCurrentConfig = { host: '172.28.25.1', port: 22, username: 'root' }
    const live = taskFixture({
      task_id: 'task-live',
      status: 'collecting',
      progress: 0,
      finished_at: null,
      archive_path: null,
      upload_mode: 'acli_direct',
      remote_url: 'http://acli.sangfor.com.cn:5888',
      ips: ['172.28.25.1'],
      detail: {},
    })
    vi.mocked(logCollectApi.listTasks).mockResolvedValue({ total: 1, items: [live] })
    // 让 getTask 晚一拍返回：bridge socket 在 openSocket 的 onopen 之后才有值，
    // 若接管对账早于连接建立，readNodeSnapshot 会直接返回 null（真实环境同样如此）
    vi.mocked(logCollectApi.getTask).mockImplementation(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20))
      return live
    })
    vi.mocked(logCollectApi.patchTask).mockResolvedValue({ ...live, status: 'completed', progress: 100 })

    const wrapper = mountPanel()
    await flushPromises()
    await new Promise((resolve) => setTimeout(resolve, 40))
    await flushPromises()
    // 对账必须真的向 bridge 发了 progress
    expect(sentMessages.some((raw) => JSON.parse(raw).log_action === 'progress')).toBe(true)

    // 接管时读到的节点报告：任务已完成，包路径还在，但包已被插件删除
    fakeSocket.onmessage?.({
      data: JSON.stringify({
        type: 'log_collect_result',
        log_action: 'progress',
        status: 'ok',
        log_data: {
          status: '收集任务已完成！',
          percent: 100,
          done: 1,
          total: 1,
          node_state: 'completed',
          archive_path: '/sf/data/local/acli/tmp/data/log_collect/Q2026102300001.zip',
          upload_state: '发送成功',
          hosts: [{ ip: '172.28.25.1', status: '收集完成', percent: 100 }],
          raw: '状态：收集任务已完成！\n总进度：100% - 1/1\n归档状态：发送成功',
        },
      }),
    })
    await flushPromises()

    const completed = vi
      .mocked(logCollectApi.patchTask)
      .mock.calls.filter(([, body]: any) => body?.status === 'completed')
    expect(completed).toHaveLength(1)
    // 完成即切结果视图：必须与「刷新页面重新进入」完全一致，
    // 而不是继续停在收集表单上、按钮还写着「开始收集」
    expect(wrapper.text()).toContain('该工单已完成日志收集')
    expect(wrapper.text()).toContain('重新收集')
    expect(wrapper.text()).not.toContain('开始收集')
    wrapper.unmount()
  })

  /**
   * 现场回归（工单 Q2026102701294 / 2026-10-27）：
   * 节点采集 100%、289 MiB 包仍在盘，平台侧却是终态且零产物。
   *
   * 契约（本轮改造）：点「上传到平台」只做搬运——
   *   1. 不重新下发 create、不再轮询 progress；
   *   2. bridge 侧用 delivery_mode=direct（collect 模式会因"包早于本次作业创建"
   *      被判成上一次任务的产物，包采集完超过 5 分钟就必然失败）；
   *   3. 采集状态不被改写（completed 原样保留，failed/cancelled 才走唯一合法边）。
   */
  it('已采集完成但未交付时点「上传到平台」走 direct 补交付，不重新采集', async () => {
    mockStore.sshConnectionState = 'connected'
    mockStore.sshCurrentConfig = { host: '172.28.25.1', port: 22, username: 'root' }
    const archivePath = '/sf/data/local/acli/tmp/data/log_collect/Q2026102701294.zip'
    const done = taskFixture({
      task_id: 'task-done',
      status: 'completed',
      progress: 100,
      archive_path: archivePath,
      upload_mode: 'acli_direct',
      remote_url: 'http://acli.sangfor.com.cn:5888',
      relay_status: 'none',
      delivery_status: 'none',
      ips: ['172.28.25.1'],
      detail: {},
    })
    vi.mocked(logCollectApi.listTasks).mockResolvedValue({ total: 1, items: [done] })
    vi.mocked(logCollectApi.getTask).mockResolvedValue(done)
    vi.mocked(logCollectApi.patchTask).mockImplementation(async (_id, body: any) => ({ ...done, ...body }))
    vi.mocked(logCollectApi.deliverTask).mockResolvedValue({
      already_delivered: false,
      delivery_status: 'queued',
      delivery_attempts: 1,
      source_path: archivePath,
      upload: {
        upload_id: 'up-1',
        upload_token: 'token-123456',
        chunk_size: 33554432,
        part_count: 1,
        total_size: 303104000,
        uploaded_parts: [],
        missing_parts: [1],
      },
    })
    vi.mocked(logCollectApi.getRelay).mockResolvedValue({ relay_status: 'success' } as any)

    const wrapper = mountPanel()
    await flushPromises()
    // bridge socket 的 onopen 是延后一拍触发的：不等它，progress 请求根本发不出去
    await new Promise((resolve) => setTimeout(resolve, 30))
    await flushPromises()

    // 节点侧：采集已完成、包仍在（P1 没传出去）
    const frozen = {
      status: '收集任务已完成！',
      percent: 100,
      done: 1,
      total: 1,
      node_state: 'completed',
      archive_path: archivePath,
      // 包存在性是 stat 结论；路径字符串本身不构成证据
      archive_exists: true,
      upload_state: '发送失败',
      hosts: [{ ip: '172.28.25.1', status: '收集完成', percent: 100 }],
      raw: '状态：收集任务已完成！\n总进度：100% - 1/1\n归档状态：发送失败',
    }
    fakeSocket.onmessage?.({
      data: JSON.stringify({ type: 'log_collect_result', log_action: 'progress', status: 'ok', log_data: frozen }),
    })
    await flushPromises()

    // 平台已完成但未交付 + 节点有包 → 必须给出「上传到平台」这个入口
    const upload = wrapper.findAll('button').find((item) => item.text() === '上传到平台')
    expect(upload).toBeTruthy()

    // 只统计本次动作本身：清掉挂载阶段对账留下的历史记录
    vi.mocked(logCollectApi.patchTask).mockClear()
    sentMessages.length = 0
    await upload!.trigger('click')
    await flushPromises()

    // 1) 先量包大小
    expect(sentMessages.some((m) => m.includes('"log_action":"stat"'))).toBe(true)
    fakeSocket.onmessage?.({
      data: JSON.stringify({
        type: 'log_collect_result',
        log_action: 'stat',
        status: 'ok',
        log_data: { size: 303104000, exists: true },
      }),
    })
    await flushPromises()

    // 2) 平台签发会话：必须是 /deliver（补交付），且不改采集状态
    await vi.waitFor(() => expect(logCollectApi.deliverTask).toHaveBeenCalledTimes(1))
    expect(vi.mocked(logCollectApi.deliverTask).mock.calls[0][0]).toBe('task-done')
    expect(vi.mocked(logCollectApi.patchTask).mock.calls.some(([, body]: any) => body?.status)).toBe(false)

    // 3) 交付作业以 direct 模式下发（不是 collect），且协议字段逐个到位
    await vi.waitFor(() =>
      expect(sentMessages.some((m) => m.includes('"delivery_mode":"direct"'))).toBe(true),
    )
    const submitMsg = sentMessages.find((m) => m.includes('"type":"delivery_submit"')) || ''
    expect(submitMsg).toContain(`"source_path":"${archivePath}"`)
    expect(submitMsg).toContain('"upload_token":"token-123456"')
    expect(submitMsg).toContain('"total_size":303104000')
    expect(submitMsg).toContain('"tls_insecure":false')
    expect(sentMessages.some((m) => m.includes('"delivery_mode":"collect"'))).toBe(false)
    fakeSocket.onmessage?.({
      data: JSON.stringify({
        type: 'delivery_result',
        log_action: 'submit',
        status: 'ok',
        log_data: { job_id: 'dj-task-done' },
      }),
    })
    await flushPromises()

    // 4) 作业完成后等代传终态
    await vi.waitFor(() => expect(sentMessages.some((m) => m.includes('"log_action":"status"'))).toBe(true))
    fakeSocket.onmessage?.({
      data: JSON.stringify({
        type: 'delivery_result',
        log_action: 'status',
        status: 'ok',
        log_data: { job_id: 'dj-task-done', phase: 'done' },
      }),
    })
    await flushPromises()
    await vi.waitFor(() => expect(logCollectApi.getRelay).toHaveBeenCalled())

    // 5) 归档成功才收敛为完成，并登记实际交付路径
    const completed = vi
      .mocked(logCollectApi.patchTask)
      .mock.calls.filter(([, body]: any) => body?.status === 'completed')
    expect(completed).toHaveLength(1)
    expect(completed[0][1]).toMatchObject({
      upload_mode: 'bridge_relay',
      delivery_status: 'delivered',
    })

    // 全程没有重新下发采集、也没有再轮询 progress
    expect(sentMessages.some((m) => m.includes('"log_action":"create"'))).toBe(false)
    expect(sentMessages.some((m) => m.includes('"log_action":"progress"'))).toBe(false)
    expect(wrapper.text()).toContain('该工单已完成日志收集')
    wrapper.unmount()
  })

  /**
   * 现场回归（工单 Q2026102701193，2026-10-27）：
   * 节点 acli 报「日志收集单发送状态：发送成功」，插件随即自删 zip；平台因漏记
   * delivery_status 被判成"未交付 + 包仍在节点"，于是结果页一直挂「上传到平台」，
   * 点下去 stat exit 1，点重新收集又被前置守卫拦住 —— 完全死锁。
   *
   * 契约：P1 的交付结论只认节点报告。报告成功就补登 delivered，不给上传入口。
   */
  it('节点报 P1 发送成功但平台漏记时，补登已交付且不提供「上传到平台」', async () => {
    mockStore.sshConnectionState = 'connected'
    mockStore.sshCurrentConfig = { host: '172.28.24.194', port: 22, username: 'root' }
    const archivePath = '/sf/data/local/acli/tmp/data/log_collect/Q2026102701193.zip'
    const done = taskFixture({
      task_id: 'task-p1-done',
      status: 'completed',
      progress: 100,
      archive_path: archivePath,
      upload_mode: 'acli_direct',
      remote_url: 'http://acli.sangfor.com.cn:5888',
      relay_status: 'none',
      delivery_status: 'none',
      ips: ['172.28.24.194'],
      detail: {},
    })
    vi.mocked(logCollectApi.listTasks).mockResolvedValue({ total: 1, items: [done] })
    vi.mocked(logCollectApi.getTask).mockResolvedValue(done)
    vi.mocked(logCollectApi.patchTask).mockImplementation(async (_id, body: any) => ({ ...done, ...body }))

    const wrapper = mountPanel()
    await flushPromises()
    await new Promise((resolve) => setTimeout(resolve, 30))
    await flushPromises()

    // 现场真实报告：发送成功；路径仍在报告里，但文件已被插件删除
    fakeSocket.onmessage?.({
      data: JSON.stringify({
        type: 'log_collect_result',
        log_action: 'progress',
        status: 'ok',
        log_data: {
          status: '收集已完成！',
          percent: 100,
          done: 1,
          total: 1,
          node_state: 'completed',
          archive_path: archivePath,
          archive_exists: false,
          upload_state: '发送成功',
          hosts: [{ ip: '172.28.24.194', status: '收集完成', percent: 100 }],
          raw: '状态：收集已完成！\n压缩包路径：/sf/data/local/acli/tmp/data/log_collect/Q2026102701193.zip\n发送地址：http://acli.sangfor.com.cn:5888\n状态：成功',
        },
      }),
    })
    await flushPromises()
    await new Promise((resolve) => setTimeout(resolve, 20))
    await flushPromises()

    const patches = vi.mocked(logCollectApi.patchTask).mock.calls.map(([, body]: any) => body)
    expect(patches.some((body: any) => body?.delivery_status === 'delivered')).toBe(true)
    // 关键：不得再出现补交付入口
    expect(wrapper.findAll('button').some((item) => item.text() === '上传到平台')).toBe(false)
    expect(wrapper.text()).toContain('已由插件直传日志服务器')
    wrapper.unmount()
  })

  /**
   * 现场反馈（2026-10-26 22:40）：刷新页面重新登录后接管进行中任务，
   * 只能看到「采集 0%」，表单里的故障场景/时间范围/目标主机全是空默认值，
   * 用户无法确认"这次到底下发了什么"。
   *
   * 平台任务里一直存着 modules / date_from / date_to / ips，缺的是接管时的回填。
   */
  it('接管进行中任务时把平台记录的下发参数回填到表单', async () => {
    mockStore.sshConnectionState = 'connected'
    mockStore.sshCurrentConfig = { host: '172.28.24.129', port: 22, username: 'admin' }
    const live = taskFixture({
      task_id: 'task-live',
      status: 'collecting',
      progress: 0,
      finished_at: null,
      archive_path: null,
      upload_mode: null,
      // 与现场一致：只选 1 台，而表单默认是 all（会显示"已全选"）
      modules: ['vm_start'],
      date_from: '2026-10-25',
      date_to: '2026-10-26',
      ips: ['172.28.24.131'],
    })
    vi.mocked(logCollectApi.listTasks).mockResolvedValue({ total: 1, items: [live] })
    vi.mocked(logCollectApi.getTask).mockResolvedValue(live)

    const wrapper = mountPanel()
    await flushPromises()
    await new Promise((resolve) => setTimeout(resolve, 20))
    await flushPromises()

    // 目标主机不能再停留在默认的 all
    expect(wrapper.text()).not.toContain('已全选')
    // 必须明确告诉用户：这些参数来自平台任务记录，而不是待填草稿
    expect(wrapper.text()).toContain('参数来自平台任务记录')
    wrapper.unmount()
  })

  /**
   * 现场（Q2026101401683 / 2026-10-26 23:22）：P1 直传把包发给日志服务器期间，
   * acli 仍输出「收集任务进行中！总进度 85%」并停留 30 多秒，步骤条却停在「采集」，
   * 用户以为采集卡死——实际节点已经在发送了。
   *
   * 契约：upload_state 出现「发送中」时必须切到「上传」步骤并显示发送状态。
   */
  it('P1 直传进入发送阶段时步骤条切到「上传」，不再停在采集 85%', async () => {
    mockStore.sshConnectionState = 'connected'
    mockStore.sshCurrentConfig = { host: '172.28.25.1', port: 22, username: 'root' }
    const live = taskFixture({
      task_id: 'task-live',
      status: 'collecting',
      progress: 0,
      finished_at: null,
      archive_path: null,
      upload_mode: 'acli_direct',
      remote_url: 'http://acli.sangfor.com.cn:5888',
      ips: ['172.28.25.1', '172.28.25.2'],
      detail: {},
    })
    vi.mocked(logCollectApi.listTasks).mockResolvedValue({ total: 1, items: [live] })
    vi.mocked(logCollectApi.getTask).mockImplementation(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20))
      return live
    })
    vi.mocked(logCollectApi.patchTask).mockResolvedValue({ ...live, progress: 85 })

    const wrapper = mountPanel()
    await flushPromises()
    await new Promise((resolve) => setTimeout(resolve, 40))
    await flushPromises()

    fakeSocket.onmessage?.({
      data: JSON.stringify({
        type: 'log_collect_result',
        log_action: 'progress',
        status: 'ok',
        log_data: {
          status: '收集任务进行中！',
          percent: 85,
          done: 2,
          total: 2,
          node_state: 'running',
          upload_state: '发送中',
          archive_path: '/sf/data/local/acli/tmp/data/log_collect/Q2026102300001.zip',
          hosts: [
            { ip: '172.28.25.1', status: '收集完成', percent: 100 },
            { ip: '172.28.25.2', status: '收集完成', percent: 100 },
          ],
          raw: '状态：收集任务进行中！\n总进度：85% - 2/2\n日志发送中',
        },
      }),
    })
    await flushPromises()

    expect(wrapper.text()).toContain('正在发送到日志服务器')
    expect(wrapper.text()).toContain('发送中')
    wrapper.unmount()
  })

  /**
   * 现场回归（工单 Q2026101401683）：「压缩包大小」长期显示「未记录（直传后压缩包已删除）」。
   *
   * staging 里该工单 40 条 progress 事件的 archive_size 全为空，成因有两层：
   * ① 插件在「进行中 / 上传中」只打印路径标题、不给真实路径，bridge 无从 stat
   *    （由 bridge 侧 fallbackLogCollectArchivePath 解决）；
   * ② 即使过程中量到过，前端也只取「完成那一刻」的快照去 PATCH —— 而那一刻 P1 已上传成功、
   *    插件已自删包，size 必然为空。观测值就这样被时间冲掉了。
   *
   * 本用例锁住 ②：过程中量到的值必须穿过时间写进完成记录。
   */
  it('过程中量到过压缩包大小时，完成 PATCH 必须带上它（而不是完成瞬间的空值）', async () => {
    mockStore.sshConnectionState = 'connected'
    mockStore.sshCurrentConfig = { host: '172.28.25.1', port: 22, username: 'root' }
    const live = taskFixture({
      task_id: 'task-archive-size',
      status: 'collecting',
      progress: 85,
      finished_at: null,
      archive_path: null,
      upload_mode: 'acli_direct',
      ips: ['172.28.25.1'],
      detail: {},
    })
    vi.mocked(logCollectApi.listTasks).mockResolvedValue({ total: 1, items: [live] })
    vi.mocked(logCollectApi.getTask).mockResolvedValue(live)
    // 调用记录跨用例累积，这里先清空，否则会把别的用例的 PATCH 一起数进来
    vi.mocked(logCollectApi.patchTask).mockReset()
    vi.mocked(logCollectApi.patchTask).mockResolvedValue({
      ...live,
      status: 'completed',
      progress: 100,
      archive_size: 314572800,
    })

    const wrapper = mountPanel()
    await flushPromises()
    await new Promise((resolve) => setTimeout(resolve, 30))
    await flushPromises()

    const refresh = () => wrapper.findAll('button').find((item) => item.text() === '刷新进度')
    const pushProgress = (logData: Record<string, unknown>) =>
      fakeSocket.onmessage?.({
        data: JSON.stringify({ type: 'log_collect_result', log_action: 'progress', status: 'ok', log_data: logData }),
      })

    // 1) 上传窗口内的一次采样：包还在，量到了大小
    expect(refresh()).toBeTruthy()
    await refresh()!.trigger('click')
    await flushPromises()
    pushProgress({
      status: '收集任务进行中！',
      percent: 85,
      done: 1,
      total: 1,
      node_state: 'running',
      archive_path: '',
      archive_size: 314572800,
      upload_state: '',
      hosts: [{ ip: '172.28.25.1', status: '收集完成', percent: 100 }],
      raw: '状态：收集任务进行中！\n总进度：85% - 1/1',
    })
    await flushPromises()
    await new Promise((resolve) => setTimeout(resolve, 30))

    // 2) 完成那一刻：P1 已上传成功、插件已自删包，这次采样量不到大小
    await refresh()!.trigger('click')
    await flushPromises()
    pushProgress({
      status: '收集任务已完成！',
      percent: 100,
      done: 1,
      total: 1,
      node_state: 'completed',
      archive_path: '/sf/data/local/acli/tmp/data/log_collect/Q2026102300001.zip',
      upload_state: '发送成功',
      hosts: [{ ip: '172.28.25.1', status: '收集完成', percent: 100 }],
      raw: '状态：收集任务已完成！\n归档状态：发送成功',
    })
    await flushPromises()

    const completed = vi
      .mocked(logCollectApi.patchTask)
      .mock.calls.filter(([, body]: any) => body?.status === 'completed')
    expect(completed).toHaveLength(1)
    // 关键：带上过程中量到的 300MB，而不是完成瞬间的空值
    expect(completed[0][1].archive_size).toBe(314572800)
    wrapper.unmount()
  })
})

/**
 * 布局契约：底条与内容必须分属两层。
 *
 * 历史问题：底条用 `position:sticky` + 负 `bottom` 钉在滚动容器底部。sticky 粘住时
 * 只是把元素"平移"上去，并不腾出它原本占的流动空间，所以只要不是滚到最顶/最底，
 * 它就会盖住此刻位于它下方的那条内容——实测就是「节点任务进行中」这类提示条下半截
 * 被吃掉；提示文案换行时提示条更高，被吃掉的比例更明显（用户反馈的原话）。
 *
 * 修法不是调 sticky 的偏移量（那只是在换一个被吃掉的像素数），而是让内容区自己滚、
 * 底条作为 flex 尾项占据真实高度：两者没有共同的滚动层，就没有抢占关系。
 */
describe('LogCollectPanel 底条与内容分层', () => {
  const SOURCE = readFileSync(resolve(process.cwd(), 'src/components/LogCollectPanel.vue'), 'utf-8')

  it('内容区 .lc-scroll 与底条 .lc-composer 是兄弟节点', () => {
    const wrapper = mountPanel()
    const scroll = wrapper.find('.lc-scroll')
    expect(scroll.exists()).toBe(true)
    // 底条一旦落在滚动容器里，滚动时必然与内容互相覆盖
    expect(scroll.find('.lc-composer').exists()).toBe(false)
    expect(wrapper.find('.lc-panel > .lc-composer').exists()).toBe(true)
    wrapper.unmount()
  })

  it('滚动由内容区自己承担，根容器只负责切分两行', () => {
    const wrapper = mountPanel()
    expect(wrapper.find('.lc-scroll').classes()).toContain('lc-scroll')
    // 根容器含 .lc-scroll 与 .lc-composer 两个子层
    expect(wrapper.findAll('.lc-panel > div')).toHaveLength(2)
    wrapper.unmount()
  })

  it('底条不再用 sticky + 负 bottom 假装贴底（负偏移必然压住一条内容）', () => {
    const rule = SOURCE.match(/\.lc-composer\s*\{([\s\S]*?)\}/)?.[1] ?? ''
    expect(rule).not.toMatch(/position:\s*sticky/)
    expect(rule).not.toMatch(/bottom:\s*-/)
    expect(rule).not.toMatch(/z-index/)
  })

  it('面板 padding 下沉到滚动容器，底条才能通栏贴边', () => {
    const panelRule = SOURCE.match(/\.lc-panel\s*\{([\s\S]*?)\}/)?.[1] ?? ''
    const scrollRule = SOURCE.match(/\.lc-scroll\s*\{([\s\S]*?)\}/)?.[1] ?? ''
    expect(panelRule).not.toMatch(/padding:/)
    expect(scrollRule).toMatch(/padding:\s*12px 16px/)
    expect(scrollRule).toMatch(/overflow-y:\s*auto/)
  })
})

/**
 * 固定提示区：第一次修底条时只把"内容"和"底条"分成两层，
 * 但报错与状态提示仍留在滚动内容里，于是换了个方向继续出问题——
 * 滚到顶部时报错被内容区上边界切掉上半行，滚到底部时底部提示被切掉下半行。
 *
 * 裁切来自"滚动容器边界"，不是元素互相遮挡，所以再调 padding / z-index 都治不好；
 * 唯一解是把必须被读到的提示移出滚动层。这组断言把这个结构钉死。
 */
describe('LogCollectPanel 固定提示区', () => {
  const SOURCE = readFileSync(resolve(process.cwd(), 'src/components/LogCollectPanel.vue'), 'utf-8')
  const alertsBlock = SOURCE.split('class="lc-alerts"')[1]?.split('<div class="lc-scroll">')[0] ?? ''
  const scrollBlock =
    SOURCE.split('<div class="lc-scroll">')[1]?.split('class="panel-composer lc-composer"')[0] ?? ''

  it('固定的提示区与滚动内容区是兄弟，不在滚动层内', () => {
    expect(alertsBlock).not.toBe('')
    expect(scrollBlock).not.toBe('')
    // 固定区在前、滚动区在后：提示永远出现在内容上方，不随滚动移动
    expect(SOURCE.indexOf('class="lc-alerts"')).toBeLessThan(SOURCE.indexOf('class="lc-scroll"'))
  })

  it('报错 / 对账 / 清理提示都在固定区，不在滚动区', () => {
    for (const marker of ['lc-sync', 'lc-cleanup', '环境准备失败']) {
      expect(alertsBlock).toContain(marker)
      expect(scrollBlock).not.toContain(marker)
    }
  })

  it('固定区自身不吃 flex 空间，且有高度上限兜底', () => {
    const rule = SOURCE.match(/\.lc-alerts\s*\{([\s\S]*?)\}/)?.[1] ?? ''
    expect(rule).toMatch(/flex:\s*0 0 auto/)
    expect(rule).toMatch(/max-height:\s*45%/)
    expect(rule).toMatch(/overflow-y:\s*auto/)
  })
})

/**
 * 滚动容器子项禁止被压缩：现场第三次反馈的真正病灶，也是前两次修复（#1295 底条分层、
 * #1299 固定提示区）都漏掉的那一层。
 *
 * 机制：`.lc-scroll` / `.lc-alerts` 都是 flex 列 + `overflow-y:auto`，内容超出产生负剩余
 * 空间，由子项 `flex-shrink` 消化。`el-form` 等子项 `overflow:visible`，`min-height:auto`
 * 等于内容高，压不动；`el-alert` 自带 `overflow:hidden`，自动最小尺寸退化为 0，负空间
 * 便全部灌到提示条上——64px 被压成 16px，再被 `align-items:center` 上下对半裁。
 * 盒子整齐堆叠、永不重叠，所以过去"overlap=0"的验收必然通过：指标错了，不是改得不够。
 *
 * happy-dom 没有排版引擎，量不出 clientHeight，这里只能当"门铃"：守卫规则 + 它守卫的
 * 容器结构同时存在，才有意义（去掉任何一半，压缩就会回来）。真正的几何结论由
 * `scripts/verify/verify_log_collect_alert_layout.py` 在 Chromium 里量 clientHeight ≥
 * scrollHeight 得出，两者分工不可互相替代。
 */
describe('LogCollectPanel 提示条不得被 flex 压缩', () => {
  const SOURCE = readFileSync(resolve(process.cwd(), 'src/components/LogCollectPanel.vue'), 'utf-8')
  const ruleOf = (selector: string) =>
    SOURCE.match(new RegExp(`${selector}\\s*\\{([\\s\\S]*?)\\}`))?.[1] ?? ''
  const guardRule = SOURCE.match(/\.lc-scroll > \*,[\s\S]*?\{([\s\S]*?)\}/)?.[1] ?? ''

  it('两个容器仍是 flex 列 + 可滚动（压缩的前提还在）', () => {
    for (const selector of ['.lc-scroll', '.lc-alerts']) {
      const rule = ruleOf(selector)
      expect(rule).toMatch(/flex-direction:\s*column/)
      expect(rule).toMatch(/overflow-y:\s*auto/)
    }
  })

  it('两个容器的子项都被禁止收缩', () => {
    // 一条规则同时覆盖两个容器：只护住其中一个，提示就会在另一个"家"里被压扁
    expect(SOURCE).toMatch(/\.lc-scroll > \*/)
    expect(SOURCE).toMatch(/\.lc-alerts > \*/)
    expect(guardRule).toMatch(/flex-shrink:\s*0/)
  })

  it('固定提示区里确实有提示条，守卫不是写了个空规则', () => {
    const alertsBlock = SOURCE.split('class="lc-alerts"')[1]?.split('<div class="lc-scroll">')[0] ?? ''
    expect(alertsBlock).toContain('el-alert')
  })
})

/**
 * acli 安装失败兜底（2026-10-29 Q2026102901049 现场）。
 *
 * 现场形态：上传 92s×3 超时后界面只弹一句错误就结束，用户唯一的出路是重头
 * 再点一次“开始收集”，于是又拉一遍 20MB 并再传一遍。契约：失败必须同时留下
 * 「为什么失败（含具体 md5/sha256）」与「从哪一跳继续」两个出口。
 */
describe('LogCollectPanel acli 安装失败兜底', () => {
  // 新 describe 不继承上面的 beforeEach：消息箱与平台字典必须逐例复位，
  // 否则上一条用例的 sent/probe 会渗到下一条，断言就失去意义。
  beforeEach(() => {
    sentMessages.length = 0
    fakeSocket.readyState = 1
    mockStore.lastSshHost = ''
    mockStore.sshConnectionState = 'disconnected'
    mockStore.sshCurrentConfig = null
    mockStore.leftPanelWidth = 520
    vi.mocked(logCollectApi.listTasks).mockReset()
    vi.mocked(logCollectApi.listTasks).mockResolvedValue({ total: 0, items: [] })
    vi.mocked(logCollectApi.listScenarios).mockReset()
    vi.mocked(logCollectApi.listScenarios).mockResolvedValue([])
    vi.mocked(logCollectApi.getTask).mockReset()
  })

  const mountReady = async () => {
    mockStore.sshConnectionState = 'connected'
    mockStore.sshCurrentConfig = { host: '172.28.25.1', port: 22, username: 'root' }
    const wrapper = mountPanel()
    await flushPromises()
    await new Promise((resolve) => setTimeout(resolve, 5))
    await flushPromises()
    return wrapper
  }

  const pushAcliResult = (payload: Record<string, unknown>) => {
    fakeSocket.onmessage?.({ data: JSON.stringify({ type: 'acli_sync_result', ...payload }) })
  }

  it('上传落盘校验不符时展示摘要，并给出「重试上传」而不是从头再来', async () => {
    const wrapper = await mountReady()

    pushAcliResult({
      case_id: 'Q2026102300001',
      status: 'error',
      stage: 'verify',
      error_code: 'LC-INSTALL-007',
      error_retryable: 'true',
      attempts: 3,
      package_size: 18718164,
      package_sha256: 'a1b2c3d4e5f6',
      package_md5: '0f1e2d3c4b5a',
      package_filename: 'acli-installer_v1.0.1.acli',
      package_path: '/sf/data/local/acli-installer_v1.0.1.acli',
      remote_size: 18700000,
      remote_algo: 'sha256',
      remote_digest: 'deadbeefcafe',
      message: '上传内容校验不符：已丢弃远端残包',
    })
    await flushPromises()

    const card = wrapper.find('.lc-acli-fail')
    expect(card.exists()).toBe(true)
    // 摘要必须原样可见：事后核对“传上去的是哪个包”只能靠这几行
    expect(card.text()).toContain('LC-INSTALL-007')
    expect(card.text()).toContain('a1b2c3d4e5f6')
    expect(card.text()).toContain('0f1e2d3c4b5a')
    expect(card.text()).toContain('deadbeefcafe')
    expect(card.text()).toContain('已尝试 3 次')
    expect(card.text()).toContain('17.9 MB')

    const buttons = card.findAll('button').map((btn) => btn.text())
    expect(buttons.some((text) => text.includes('重试上传'))).toBe(true)
    // 包还没被证明正确，不应该先建议“只重装”
    expect(buttons.some((text) => text.includes('重试安装'))).toBe(false)

    // 点「重试上传」必须把 retry_stage 真的发出去（否则 bridge 只能从头下载）
    const uploadButton = card.findAll('button').find((btn) => btn.text().includes('重试上传'))
    await uploadButton!.trigger('click')
    await flushPromises()

    const retry = sentMessages
      .map((raw) => JSON.parse(raw))
      .filter((message) => message.type === 'acli_sync')
      .pop()
    expect(retry?.retry_stage).toBe('upload')
    expect(retry?.node_ip).toBe('172.28.25.1')
  })

  it('安装命令失败时只给「重试安装」（包已校验通过，重传无意义）', async () => {
    const wrapper = await mountReady()

    pushAcliResult({
      case_id: 'Q2026102300001',
      status: 'error',
      stage: 'install',
      error_code: 'LC-INSTALL-002',
      error_retryable: 'true',
      install_exit_code: 2,
      install_output: 'distribute to node 172.28.25.2 failed',
      integrity_state: 'sha256',
      package_sha256: 'feedface',
      message: 'acli 安装命令退出码 2',
    })
    await flushPromises()

    const card = wrapper.find('.lc-acli-fail')
    expect(card.exists()).toBe(true)
    // 安装输出必须上屏：真正的失败原因只出现在这里
    expect(card.text()).toContain('distribute to node 172.28.25.2 failed')
    expect(card.text()).toContain('已通过 SHA-256 内容校验')

    const installButton = card.findAll('button').find((btn) => btn.text().includes('重试安装'))
    expect(installButton).toBeTruthy()
    await installButton!.trigger('click')
    await flushPromises()

    const retry = sentMessages
      .map((raw) => JSON.parse(raw))
      .filter((message) => message.type === 'acli_sync')
      .pop()
    expect(retry?.retry_stage).toBe('install')
  })

  it('不可重试的失败（磁盘不足）不提供重试按钮', async () => {
    const wrapper = await mountReady()

    pushAcliResult({
      case_id: 'Q2026102300001',
      status: 'disk_space_insufficient',
      error_code: 'LC-ENV-007',
      error_retryable: 'false',
      message: '/sf/data/local 目录可用空间不足',
    })
    await flushPromises()

    const card = wrapper.find('.lc-acli-fail')
    expect(card.exists()).toBe(true)
    const labels = card.findAll('button').map((btn) => btn.text())
    expect(labels).toContain('知道了')
    expect(labels.some((text) => text.includes('重试'))).toBe(false)
  })
})
