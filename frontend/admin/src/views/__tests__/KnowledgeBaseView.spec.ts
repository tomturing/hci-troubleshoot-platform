import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import KnowledgeBaseView from '../KnowledgeBaseView.vue'

// 组件只发一个请求：GET /weknora/launch。全部走内存 mock，不碰真实网络。
const shared = vi.hoisted(() => ({ launch: vi.fn() }))
vi.mock('@hci/shared', () => ({ createApiClient: () => ({ get: shared.launch }) }))

async function mountView() {
  const wrapper = mount(KnowledgeBaseView, { global: { plugins: [ElementPlus] } })
  await flushPromises()
  return wrapper
}

describe('KnowledgeBaseView 知识库内嵌', () => {
  const wrappers: Array<ReturnType<typeof mount>> = []

  beforeEach(() => {
    vi.clearAllMocks()
  })

  afterEach(() => {
    for (const wrapper of wrappers.splice(0)) wrapper.unmount()
  })

  it('已开通渠道时用后端下发的短令牌地址渲染问答 iframe', async () => {
    shared.launch.mockResolvedValue({
      data: {
        console_url: '/weknora/',
        embed_url: '/weknora/embed/ch-1?token=ems_short',
      },
    })

    const wrapper = await mountView()
    wrappers.push(wrapper)

    const frame = wrapper.find('iframe')
    expect(frame.exists()).toBe(true)
    expect(frame.attributes('src')).toBe('/weknora/embed/ch-1?token=ems_short')
    // 浏览器侧只应看到短效 ems_ 令牌，长期发布令 em_ 只存在于集群 Secret。
    expect(frame.attributes('src')).toContain('token=ems_')
  })

  it('未创建渠道时展示空态文案，而不是报错', async () => {
    shared.launch.mockResolvedValue({ data: { console_url: '/weknora/', embed_url: null } })

    const wrapper = await mountView()
    wrappers.push(wrapper)

    expect(wrapper.find('iframe').exists()).toBe(false)
    expect(wrapper.text()).toContain('尚未创建知识库问答渠道')
  })

  it('503 时给出可重试提示且不渲染 iframe', async () => {
    shared.launch.mockRejectedValue({ response: { status: 503 } })

    const wrapper = await mountView()
    wrappers.push(wrapper)

    expect(wrapper.find('iframe').exists()).toBe(false)
    expect(wrapper.text()).toContain('知识库服务正在启动')
  })

  it('切到管理台页签才加载整站 iframe，避免首次进页就双份加载', async () => {
    shared.launch.mockResolvedValue({
      data: { console_url: '/weknora/', embed_url: '/weknora/embed/ch-1?token=ems_short' },
    })

    const wrapper = await mountView()
    wrappers.push(wrapper)

    // 默认问答页签：只有问答 iframe
    expect(wrapper.findAll('iframe')).toHaveLength(1)

    const consoleTab = wrapper.findAll('.el-tabs__item').find((item) => item.text().includes('知识库管理'))
    if (!consoleTab) throw new Error('未找到知识库管理页签')
    await consoleTab.trigger('click')
    await flushPromises()

    const sources = wrapper.findAll('iframe').map((item) => item.attributes('src'))
    expect(sources).toContain('/weknora/')
  })
})
