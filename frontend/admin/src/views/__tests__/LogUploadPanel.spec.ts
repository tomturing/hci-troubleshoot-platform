/**
 * 「日志上传」管理页契约测试。
 *
 * 守住四条已确认的边界：
 * 1. 客户名与 client_id 同时展示；未关联客户时显示占位而不是编造名字；
 * 2. 真实进度只对「进行中」的行查询（终态行不查，避免打爆单进程 5888）；
 * 3. 「查不到 ≠ 失败」：no_record/unsupported 只陈述观测，不出现失败结论；
 * 4. 页面全只读：没有重新核验/终止/重传/导出入口，也不出现删除命令；
 *    定时器在卸载时必须清理，切走页签后不能继续轮询。
 */

import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  listAdminTasks: vi.fn(),
  getDestinationProgress: vi.fn(),
}))

vi.mock('@/api/logCollectAdminTasks', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/logCollectAdminTasks')>()
  return { ...actual, ...api }
})

const LogUploadPanel = (await import('../components/LogUploadPanel.vue')).default

const activeTask = {
  task_id: 'task-active',
  case_id: 'Q2026111000243',
  client_id: 'client-abc',
  customer_name: '示例客户',
  origin_node_ip: '172.28.25.1',
  upload_mode: 'acli_direct',
  status: 'uploading',
  progress: 80,
  archive_size: 885094930,
  delivery_status: 'uploading',
  relay_status: 'none',
  relay_upload_id: null,
  integrity_state: 'unverified',
  archive_check: null,
  error_code: null,
  error_message: null,
  trace_id: 'trace-1',
  created_at: '2026-11-10T02:46:37Z',
  finished_at: null,
  delivery_finished_at: null,
}

const doneTask = {
  ...activeTask,
  task_id: 'task-done',
  case_id: 'Q2026111000244',
  customer_name: null,
  client_id: 'client-xyz',
  status: 'completed',
  progress: 100,
  delivery_status: 'delivered',
  finished_at: '2026-11-10T11:02:02Z',
  delivery_finished_at: '2026-11-10T11:02:02Z',
}

const summary = {
  total: 2, transferring: 1, delivered: 1, unverified: 0, failed: 0,
  p1: { total: 2, transferring: 1, delivered: 1, unverified: 0, failed: 0 },
  p2: { total: 0, transferring: 0, delivered: 0, unverified: 0, failed: 0 },
}

beforeEach(() => {
  api.listAdminTasks.mockReset()
  api.getDestinationProgress.mockReset()
  api.listAdminTasks.mockResolvedValue({
    items: [activeTask, doneTask], total: 2, page: 1, page_size: 20, summary,
  })
  api.getDestinationProgress.mockResolvedValue({
    task_id: 'task-active',
    path: 'P1',
    state: 'done',
    progress: 100,
    uploaded_size: 885094930,
    file_size: 885094930,
    received_chunks: 85,
    chunk_total: 85,
    target_path: '/sf/log/Q2026111000243/Q2026111000243.zip',
    sha256: 'c'.repeat(64),
    finished_at: '2026-11-10T11:02:02Z',
    checked_at: '2026-11-10T11:02:03Z',
    reason: null,
  })
})

async function mountPanel() {
  const wrapper = mount(LogUploadPanel, { global: { plugins: [ElementPlus] } })
  await flushPromises()
  await flushPromises()
  return wrapper
}

describe('LogUploadPanel', () => {
  it('展示汇总条、客户名与 client_id，未关联客户显示占位', async () => {
    const wrapper = await mountPanel()
    const text = wrapper.text()
    expect(text).toContain('示例客户')
    expect(text).toContain('client-abc')
    expect(text).toContain('未关联客户')
    expect(text).toContain('P1 直传 / P2 中转')
    wrapper.unmount()
  })

  it('真实进度只查进行中的行', async () => {
    const wrapper = await mountPanel()
    expect(api.getDestinationProgress).toHaveBeenCalledTimes(1)
    expect(api.getDestinationProgress).toHaveBeenCalledWith('task-active')
    expect(wrapper.text()).toContain('日志服务器已收妥（sha256 与源包一致）')
    wrapper.unmount()
  })

  it('查不到只陈述观测结果，不写失败', async () => {
    api.getDestinationProgress.mockResolvedValue({
      task_id: 'task-active', path: 'P1', state: 'unsupported',
      progress: null, uploaded_size: null, file_size: null,
      received_chunks: null, chunk_total: null, target_path: null, sha256: null,
      finished_at: null, checked_at: '2026-11-10T11:02:03Z',
      reason: '该日志服务器不支持进度查询',
    })
    const wrapper = await mountPanel()
    expect(wrapper.text()).toContain('该日志服务器不支持进度查询')
    expect(wrapper.text()).not.toContain('上传失败')
    wrapper.unmount()
  })

  it('页面全只读：无写操作入口，也不出现删除命令', async () => {
    const wrapper = await mountPanel()
    const text = wrapper.text()
    for (const forbidden of ['重新核验', '终止', '重传', '导出 CSV', 'rm -', 'del ']) {
      expect(text).not.toContain(forbidden)
    }
    const buttons = wrapper.findAll('button').map((item) => item.text())
    expect(buttons).not.toContain('删除')
    wrapper.unmount()
  })

  it('卸载时清理两个定时器，切走页签后不再轮询', async () => {
    const clearSpy = vi.spyOn(globalThis, 'clearInterval')
    const wrapper = await mountPanel()
    wrapper.unmount()
    expect(clearSpy).toHaveBeenCalledTimes(2)
    clearSpy.mockRestore()
  })
})
