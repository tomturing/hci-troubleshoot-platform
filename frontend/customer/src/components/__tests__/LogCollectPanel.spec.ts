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
    patchTask: vi.fn(),
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

  it('工具条提供「刷新进度」入口，「关闭」由外壳的收起按钮承担', async () => {
    const wrapper = mountPanel()
    await flushPromises()

    const bar = wrapper.find('.lc-case-bar')
    expect(bar.text()).toContain('归档工单')
    expect(bar.text()).toContain('刷新进度')
    // 收起动作由 PanelShell 提供，组件内不该再出现"关闭"按钮导致两个入口
    expect(bar.text()).not.toContain('关闭')
    expect(bar.text()).not.toContain('钉住')
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

  // 现场反馈：产物位置是长路径，排在右列会把行高撑成两行且右侧留白；
  // 换成「产物位置在左、归档状态在右」后路径靠左读，短文案收口在右。
  it('结果态字段顺序把产物位置排在归档状态之前', async () => {
    vi.mocked(logCollectApi.listTasks).mockResolvedValue({ total: 1, items: [taskFixture()] })

    const wrapper = mountPanel()
    await flushPromises()
    await flushPromises()

    const labels = wrapper.findAll('.desc-label').map((node) => node.text())
    expect(labels.slice(-2)).toEqual(['产物位置', '归档状态'])
    // 大小正常展示成 MB，不再落到「未记录」
    expect(wrapper.text()).toContain('100.0 MB')
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
})
