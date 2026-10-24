import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mockStore = vi.hoisted(() => ({
  environmentContext: null as any,
  collectionState: 'idle' as 'idle' | 'collecting' | 'success' | 'error',
  sshConnectionState: 'disconnected',
  currentCase: { case_id: 'CASE-1' },
  hasActiveCase: true,
  refreshEnvironmentData: vi.fn(),
}))

vi.mock('@/stores/chat', () => ({ useChatStore: () => mockStore }))

const mountCard = async () => {
  const EnvironmentSummary = (await import('@/components/EnvironmentSummary.vue')).default
  return mount(EnvironmentSummary, {
    global: {
      stubs: {
        ElTooltip: { template: '<span><slot /></span>' },
        ElButton: { template: '<button><slot /></button>' },
      },
    },
  })
}

/** 后端未采集到数据时的形态：字段存在但全是「未知」占位 */
const envContextWithPlaceholders = {
  env_info: { hci_version: '未知', cluster_name: '未知', host_count: '未知' },
  alert_logs: [],
  task_logs: [],
}

describe('环境数据卡片的状态展示', () => {
  beforeEach(() => {
    mockStore.environmentContext = envContextWithPlaceholders
    mockStore.collectionState = 'idle'
  })

  it('采集请求成功但字段全是「未知」时，不能显示「已采集」', async () => {
    mockStore.collectionState = 'success'
    const wrapper = await mountCard()
    const header = wrapper.find('.header-left').text()
    expect(header).toContain('暂无数据')
    expect(header).not.toContain('已采集')
  })

  it('真的采到集群版本时显示「已采集」', async () => {
    mockStore.collectionState = 'success'
    mockStore.environmentContext = {
      env_info: { hci_version: '6.10.0', cluster_name: '未知', host_count: '未知' },
      alert_logs: [],
      task_logs: [],
    }
    const wrapper = await mountCard()
    expect(wrapper.find('.header-left').text()).toContain('已采集')
  })

  it('只有告警数据也算采到内容', async () => {
    mockStore.collectionState = 'idle'
    mockStore.environmentContext = {
      env_info: { hci_version: '未知' },
      alert_logs: [{ level: '严重', content: '硬盘故障' }],
      task_logs: [],
    }
    const wrapper = await mountCard()
    expect(wrapper.find('.header-left').text()).toContain('已采集')
  })

  it('页面刷新后 collectionState 回到 idle，但已有数据仍显示「已采集」', async () => {
    mockStore.collectionState = 'idle'
    mockStore.environmentContext = {
      env_info: { hci_version: '6.10.0' },
      alert_logs: [],
      task_logs: [],
    }
    const wrapper = await mountCard()
    expect(wrapper.find('.header-left').text()).toContain('已采集')
  })

  it('采集中与采集失败各有独立状态', async () => {
    mockStore.collectionState = 'collecting'
    expect((await mountCard()).find('.header-left').text()).toContain('采集中')

    mockStore.collectionState = 'error'
    expect((await mountCard()).find('.header-left').text()).toContain('采集失败')
  })

  it('状态标签必须带文字，不允许出现只有图标的空色块', async () => {
    const wrapper = await mountCard()
    // 测试环境未注册 Element Plus，el-tag 保持原生标签名
    const tag = wrapper.find('.header-left el-tag')
    expect(tag.exists()).toBe(true)
    // 标签内必须有可见文字（图标是 svg，不算文字）
    expect(tag.text().trim().length).toBeGreaterThan(0)
  })
})
