import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import DshDiagnosisView from '../DshDiagnosisView.vue'
import type { SyncJob } from '@/api/ticketSync'

// createApiClient 在组件里既用于取 DSH 会话入口，也被 ticketSync 模块用来构造客户端。
// 全部走内存 mock，测试不碰真实网络。
const shared = vi.hoisted(() => ({ launch: vi.fn() }))
vi.mock('@hci/shared', () => ({ createApiClient: () => ({ get: shared.launch }) }))

const ticketApi = vi.hoisted(() => ({
  createSyncJob: vi.fn(),
  fetchSyncJob: vi.fn(),
  cancelSyncJob: vi.fn(),
}))
// 只替换三个网络调用，保留 TICKET_ID_PATTERN / PHASE_TEXT / errorText 等纯函数，
// 这样测试校验的是页面真实使用的规则，而不是另写一份。
vi.mock('@/api/ticketSync', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/ticketSync')>()
  return { ...actual, ...ticketApi }
})

function makeJob(phase: SyncJob['phase'], patch: Partial<SyncJob> = {}): SyncJob {
  return {
    ticketId: 'Q2026052801304',
    phase,
    receivedBytes: 0,
    totalBytes: 0,
    extractedBytes: 0,
    fileCount: 0,
    currentArchive: null,
    errorCode: null,
    message: null,
    acceptedAt: '2026-10-23T12:00:00+08:00',
    startedAt: null,
    updatedAt: '2026-10-23T12:00:00+08:00',
    ...patch,
  }
}

async function mountView() {
  const wrapper = mount(DshDiagnosisView, { global: { plugins: [ElementPlus] } })
  await flushPromises()
  return wrapper
}

function buttonByText(wrapper: ReturnType<typeof mount>, text: string) {
  const button = wrapper.findAll('button').find((item) => item.text().includes(text))
  if (!button) throw new Error(`未找到按钮: ${text}`)
  return button
}

describe('DshDiagnosisView 工单同步', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    shared.launch.mockResolvedValue({ data: { url: 'http://dsh.local/session' } })
    ticketApi.createSyncJob.mockResolvedValue(undefined)
    ticketApi.cancelSyncJob.mockResolvedValue(undefined)
  })

  it('工单号格式不合法时只提示，不发请求', async () => {
    const wrapper = await mountView()
    await wrapper.find('input').setValue('Q2026')

    await buttonByText(wrapper, '同步日志').trigger('click')
    await flushPromises()

    expect(ticketApi.createSyncJob).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('工单号格式错误')
  })

  it('同步完成后展示文件数与解压体积', async () => {
    ticketApi.fetchSyncJob.mockResolvedValue(
      makeJob('ready', { extractedBytes: 584656850, fileCount: 4323 })
    )

    const wrapper = await mountView()
    await wrapper.find('input').setValue('Q2026052801304')
    await buttonByText(wrapper, '同步日志').trigger('click')
    await flushPromises()

    expect(ticketApi.createSyncJob).toHaveBeenCalledWith('Q2026052801304')
    expect(wrapper.text()).toContain('已就绪')
    expect(wrapper.text()).toContain('4323 个文件')
    expect(wrapper.text()).toContain('557.6 MiB')
  })

  it('失败任务按错误码给出可读文案', async () => {
    ticketApi.fetchSyncJob.mockResolvedValue(
      makeJob('failed', { errorCode: 'bad-password', message: '归档密码错误，请联系运维' })
    )

    const wrapper = await mountView()
    await wrapper.find('input').setValue('Q2026052801304')
    await buttonByText(wrapper, '同步日志').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('归档密码错误，请联系运维')
  })

  it('解压中展示当前归档，并可取消任务', async () => {
    ticketApi.fetchSyncJob
      .mockResolvedValueOnce(
        makeJob('extracting', {
          extractedBytes: 104857600,
          fileCount: 1200,
          currentArchive: 'sp_192.168.41.67/sf/log/blackbox/LOG_cgroup.txt.zip',
        })
      )
      .mockResolvedValueOnce(makeJob('cancelled'))

    const wrapper = await mountView()
    await wrapper.find('input').setValue('Q2026052801304')
    await buttonByText(wrapper, '同步日志').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('解压中')
    expect(wrapper.text()).toContain('LOG_cgroup.txt.zip')

    await buttonByText(wrapper, '取消').trigger('click')
    await flushPromises()

    expect(ticketApi.cancelSyncJob).toHaveBeenCalledWith('Q2026052801304')
    expect(wrapper.text()).toContain('已取消')

    wrapper.unmount()
  })
})
