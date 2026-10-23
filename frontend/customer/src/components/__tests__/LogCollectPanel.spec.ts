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
      logCollectActiveTask: null,
      openLogCollectCaseDialog: vi.fn(),
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
          ElDrawer: passthrough('ElDrawer'),
          ElForm: passthrough('ElForm'),
          ElFormItem: passthrough('ElFormItem'),
          ElAlert: passthrough('ElAlert'),
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

  it('抽屉从左侧弹出（direction=ltr）', async () => {
    const wrapper = mountPanel()
    await flushPromises()

    expect(wrapper.html()).toContain('direction="ltr"')
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
})
