import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import SimulationTestView from '../SimulationTestView.vue'

function response(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function sse(frames: string[]) {
  const encoder = new TextEncoder()
  return new Response(new ReadableStream({
    start(controller) {
      for (const frame of frames) controller.enqueue(encoder.encode(frame))
      controller.close()
    },
  }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } })
}

type SocketHarness = {
  readyState: number
  onopen: (() => void) | null
  onmessage: ((event: { data: string }) => void) | null
  sent: string[]
}

const capabilityBody = {
  support_id: '27123', requested_revision: 25, runtime_revision: 25,
  bundle_digest: 'sha256:test', bundle_status: 'published', authority_scope: 'dev_golden',
  buildable: true, capability_gap: [],
}
const buildBody = {
  test_run_id: 'run-27123',
  environment_context: { test_run_id: 'run-27123', support_id: '27123', kbd_revision: 1, case_id: '' },
  connection: { host: 'hci-sim', port: 2222, username: 'sim', password: 'htp2.test', execution_mode: 'sim-ssh', test_run_id: 'run-27123' },
}

describe('SimulationTestView 可恢复状态机', () => {
  let sockets: SocketHarness[]

  beforeEach(() => {
    sockets = []
    localStorage.clear()
    document.body.innerHTML = ''
    vi.restoreAllMocks()
    vi.stubGlobal('fetch', vi.fn())
    class TestWebSocket {
      static OPEN = 1
      static CONNECTING = 0
      readyState = 1
      onopen: (() => void) | null = null
      onmessage: ((event: { data: string }) => void) | null = null
      onerror: (() => void) | null = null
      onclose: (() => void) | null = null
      sent: string[] = []
      constructor() { sockets.push(this) }
      send(payload: string) { this.sent.push(payload) }
      close() { this.readyState = 3; this.onclose?.() }
    }
    vi.stubGlobal('WebSocket', TestWebSocket)
  })

  function mountView() {
    return mount(SimulationTestView, {
      attachTo: document.body,
      global: { plugins: [ElementPlus] },
    })
  }

  function clickDocumentButton(label: string) {
    const button = Array.from(document.querySelectorAll('button')).find((item) => item.textContent?.trim() === label)
    if (!button) throw new Error(`找不到按钮：${label}`)
    ;(button as HTMLButtonElement).click()
  }

  function setDocumentInput(selector: string, value: string) {
    const input = document.querySelector(selector) as HTMLInputElement | HTMLTextAreaElement | null
    if (!input) throw new Error(`找不到输入框：${selector}`)
    input.value = value
    input.dispatchEvent(new Event('input', { bubbles: true }))
  }

  async function build(wrapper: ReturnType<typeof mountView>) {
    await wrapper.get('input').setValue('27123')
    await wrapper.findAll('button').find((button) => button.text() === '环境构建')!.trigger('click')
    await flushPromises()
  }

  async function openAndCreateCase(wrapper: ReturnType<typeof mountView>) {
    clickDocumentButton('开始测试')
    await flushPromises()
    setDocumentInput('.case-form input', 'KBD 27123 仿真验证')
    setDocumentInput('.case-form textarea', '请诊断启动虚拟机失败并验证关键信号')
    clickDocumentButton('创建工单并进入测试')
    await flushPromises()
  }

  it('以两个显式阶段呈现，并且开始测试只打开工单步骤', async () => {
    const fetchMock = vi.mocked(fetch)
    fetchMock.mockResolvedValueOnce(response(capabilityBody)).mockResolvedValueOnce(response(buildBody))
    const wrapper = mountView()
    await build(wrapper)
    clickDocumentButton('开始测试')
    await flushPromises()

    expect(wrapper.text()).toContain('环境构建')
    expect(wrapper.text()).toContain('开始测试')
    expect(document.body.textContent).toContain('创建工单并进入测试')
    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes('/test-runs'))).toBe(false)
  })

  it('能力预检阻断时不发送环境构建请求，并展示结构化差距', async () => {
    const fetchMock = vi.mocked(fetch)
    fetchMock.mockResolvedValueOnce(response({ ...capabilityBody, support_id: '23821', bundle_status: 'missing', buildable: false, capability_gap: ['no_published_immutable_bundle'] }))
    const wrapper = mountView()
    await wrapper.get('input').setValue('23821')
    await wrapper.findAll('button').find((button) => button.text() === '环境构建')!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('no_published_immutable_bundle')
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('非安全 HTTP 环境没有 crypto.subtle 时仍由 Gateway 完成 Result', async () => {
    vi.stubGlobal('crypto', {})
    const fetchMock = vi.mocked(fetch)
    fetchMock
      .mockResolvedValueOnce(response(capabilityBody))
      .mockResolvedValueOnce(response(buildBody))
      .mockResolvedValueOnce(response({ test_run_id: 'run-27123', case_id: 'Q2026081100001' }))
      .mockResolvedValueOnce(response({ conversation_id: '00000000-0000-0000-0000-000000027123' }, 201))
      .mockResolvedValueOnce(sse(['event: agent_exec_command\ndata: {"execId":"exec-1","command":"acli system lsof","riskLevel":1}\n\n', 'event: message\ndata: {"content":"诊断完成"}\n\n', 'data: [DONE]\n\n']))
      .mockResolvedValueOnce(response({ status: 'accepted' }))
      .mockResolvedValueOnce(response({ status: 'passed' }))

    const wrapper = mountView()
    await build(wrapper)
    await openAndCreateCase(wrapper)
    expect(sockets).toHaveLength(1)
    sockets[0].onopen?.()
    const connect = JSON.parse(sockets[0].sent[0])
    expect(connect.password).toBe('htp2.test')
    sockets[0].onmessage?.({ data: JSON.stringify({ type: 'ssh_connected' }) })
    await flushPromises()
    sockets[0].onmessage?.({ data: JSON.stringify({ type: 'exec_result', exec_id: 'exec-1', exit_code: 0, stdout: 'ok' }) })
    await flushPromises()

    const resultCall = fetchMock.mock.calls.find(([url]) => String(url).endsWith('/test-runs/run-27123/result'))
    expect(resultCall).toBeTruthy()
    const resultPayload = JSON.parse(String((resultCall![1] as RequestInit).body))
    expect(resultPayload.report_summary).toMatchObject({ case_id: 'Q2026081100001', agent_stream_completed: true })
    expect(resultPayload).not.toHaveProperty('report_digest')
    expect(wrapper.text()).toContain('已通过')
  })

  it('结构化人工升级以 inconclusive 闭环，不能假报 passed', async () => {
    const fetchMock = vi.mocked(fetch)
    fetchMock
      .mockResolvedValueOnce(response(capabilityBody))
      .mockResolvedValueOnce(response(buildBody))
      .mockResolvedValueOnce(response({ test_run_id: 'run-27123', case_id: 'Q2026081100003' }))
      .mockResolvedValueOnce(response({ conversation_id: '00000000-0000-0000-0000-000000027125' }, 201))
      .mockResolvedValueOnce(sse([
        'event: interactive_request\ndata: {"kind":"human_escalation","prompt":"自动诊断证据不足"}\n\n',
        'data: [DONE]\n\n',
      ]))
      .mockResolvedValueOnce(response({ status: 'inconclusive' }))

    const wrapper = mountView()
    await build(wrapper)
    await openAndCreateCase(wrapper)
    sockets[0].onopen?.()
    sockets[0].onmessage?.({ data: JSON.stringify({ type: 'ssh_connected' }) })
    await flushPromises()

    const resultCall = fetchMock.mock.calls.find(([url]) => String(url).endsWith('/test-runs/run-27123/result'))
    expect(resultCall).toBeTruthy()
    const resultPayload = JSON.parse(String((resultCall![1] as RequestInit).body))
    expect(resultPayload.outcome).toBe('inconclusive')
    expect(resultPayload.report_summary).toMatchObject({ outcome: 'inconclusive', command_count: 0 })
    expect(wrapper.text()).toContain('证据不足')
    expect(wrapper.text()).not.toContain('已通过')
  })

  it('呈现 S0 稳定分类选项并按 Customer UI 协议推进到 S1', async () => {
    const fetchMock = vi.mocked(fetch)
    fetchMock
      .mockResolvedValueOnce(response(capabilityBody))
      .mockResolvedValueOnce(response(buildBody))
      .mockResolvedValueOnce(response({ test_run_id: 'run-27123', case_id: 'Q2026081100002' }))
      .mockResolvedValueOnce(response({ conversation_id: '00000000-0000-0000-0000-000000027124' }, 201))
      .mockResolvedValueOnce(sse([
        'data: {"content":"请确认分类"}\n\n',
        'event: metadata\ndata: {"kind":"choice_options","requestId":"triage-27123","options":[{"optionId":"虚拟机-003","name":"虚拟机-003 虚拟机开机失败"},{"optionId":"__none__","name":"以上不是，重新描述"}]}\n\n',
        'data: [DONE]\n\n',
      ]))
      .mockResolvedValueOnce(sse(['data: {"content":"已进入故障定位"}\n\n', 'data: [DONE]\n\n']))

    const wrapper = mountView()
    await build(wrapper)
    await openAndCreateCase(wrapper)
    sockets[0].onopen?.()
    sockets[0].onmessage?.({ data: JSON.stringify({ type: 'ssh_connected' }) })
    await flushPromises()

    const choice = wrapper.findAll('button').find((button) => button.text() === '虚拟机-003 虚拟机开机失败')
    expect(choice).toBeTruthy()
    await choice!.trigger('click')
    await flushPromises()

    const secondMessageCall = fetchMock.mock.calls.filter(([url]) => String(url).includes('/message'))[1]
    const payload = JSON.parse(String((secondMessageCall[1] as RequestInit).body))
    expect(payload.metadata).toMatchObject({
      kind: 'intent_selection_response',
      selectedOptionId: '虚拟机-003',
      selectedCategoryCode: '虚拟机-003',
      sourceRequestId: 'triage-27123',
      execution_mode: 'sim-ssh',
      test_run_id: 'run-27123',
    })
  })

  it('S0 的以上都不是必须补充症状后才提交', async () => {
    const fetchMock = vi.mocked(fetch)
    fetchMock
      .mockResolvedValueOnce(response(capabilityBody))
      .mockResolvedValueOnce(response(buildBody))
      .mockResolvedValueOnce(response({ test_run_id: 'run-27123', case_id: 'Q2026081100004' }))
      .mockResolvedValueOnce(response({ conversation_id: '00000000-0000-0000-0000-000000027126' }, 201))
      .mockResolvedValueOnce(sse([
        'event: metadata\ndata: {"kind":"choice_options","requestId":"triage-27124","options":[{"optionId":"虚拟机-003","name":"虚拟机-003 虚拟机开机失败"},{"optionId":"__none__","name":"以上都不是（请补充症状描述）"}]}\n\n',
        'data: [DONE]\n\n',
      ]))
      .mockResolvedValueOnce(sse(['data: {"content":"请提供更多信息"}\n\n', 'data: [DONE]\n\n']))

    const wrapper = mountView()
    await build(wrapper)
    await openAndCreateCase(wrapper)
    sockets[0].onopen?.()
    sockets[0].onmessage?.({ data: JSON.stringify({ type: 'ssh_connected' }) })
    await flushPromises()

    const noneChoice = wrapper.findAll('button').find((button) => button.text() === '以上都不是（请补充症状描述）')
    expect(noneChoice).toBeTruthy()
    await noneChoice!.trigger('click')
    await flushPromises()

    expect(fetchMock.mock.calls.filter(([url]) => String(url).includes('/message'))).toHaveLength(1)
    const symptomInput = wrapper.get('textarea[placeholder="请输入具体症状描述..."]')
    await symptomInput.setValue('虚拟机可以开机，但迁移任务持续失败')
    await wrapper.findAll('button').find((button) => button.text() === '提交')!.trigger('click')
    await flushPromises()

    const secondMessageCall = fetchMock.mock.calls.filter(([url]) => String(url).includes('/message'))[1]
    const payload = JSON.parse(String((secondMessageCall[1] as RequestInit).body))
    expect(payload.content).toBe('以上都不是（请补充症状描述） 虚拟机可以开机，但迁移任务持续失败')
    expect(payload.metadata).toMatchObject({
      kind: 'intent_selection_response',
      selectedOptionId: '__none__',
      isNoneOfAbove: true,
      freeText: '虚拟机可以开机，但迁移任务持续失败',
      sourceRequestId: 'triage-27124',
      execution_mode: 'sim-ssh',
      test_run_id: 'run-27123',
    })
  })

  it('Result 失败后只重试 Result，不重复创建工单、WebSocket 或 Agent 执行', async () => {
    const fetchMock = vi.mocked(fetch)
    fetchMock
      .mockResolvedValueOnce(response(capabilityBody))
      .mockResolvedValueOnce(response(buildBody))
      .mockResolvedValueOnce(response({ test_run_id: 'run-27123', case_id: 'Q2026081100001' }))
      .mockResolvedValueOnce(response({ conversation_id: '00000000-0000-0000-0000-000000027123' }, 201))
      .mockResolvedValueOnce(sse(['event: agent_exec_command\ndata: {"execId":"exec-1","command":"acli system lsof","riskLevel":1}\n\n', 'data: {"content":"完成"}\n\n']))
      .mockResolvedValueOnce(response({ status: 'accepted' }))
      .mockResolvedValueOnce(response({ detail: 'temporary unavailable' }, 503))
      .mockResolvedValueOnce(response({ status: 'passed' }))

    const wrapper = mountView()
    await build(wrapper)
    await openAndCreateCase(wrapper)
    sockets[0].onopen?.()
    sockets[0].onmessage?.({ data: JSON.stringify({ type: 'ssh_connected' }) })
    await flushPromises()
    sockets[0].onmessage?.({ data: JSON.stringify({ type: 'exec_result', exec_id: 'exec-1', exit_code: 0, stdout: 'ok' }) })
    await flushPromises()
    expect(wrapper.text()).toContain('结果待提交')

    await wrapper.findAll('button').find((button) => button.text() === '重试提交结果')!.trigger('click')
    await flushPromises()
    expect(sockets).toHaveLength(1)
    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith('/test-runs')).length).toBe(1)
    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith('/result')).length).toBe(2)
  })

  it('Bridge 缺少整数 exit_code 时严格失败，且 Lease 清除后不发生空密码重连', async () => {
    const fetchMock = vi.mocked(fetch)
    fetchMock
      .mockResolvedValueOnce(response(capabilityBody))
      .mockResolvedValueOnce(response(buildBody))
      .mockResolvedValueOnce(response({ test_run_id: 'run-27123', case_id: 'Q2026081100001' }))
      .mockResolvedValueOnce(response({ conversation_id: '00000000-0000-0000-0000-000000027123' }, 201))
      .mockResolvedValueOnce(sse([
        'event: agent_exec_command\ndata: {"execId":"exec-1","command":"acli system lsof","riskLevel":1}\n\n',
      ]))

    const wrapper = mountView()
    await build(wrapper)
    await openAndCreateCase(wrapper)
    sockets[0].onopen?.()
    sockets[0].onmessage?.({ data: JSON.stringify({ type: 'ssh_connected' }) })
    await flushPromises()
    expect(sockets[0].sent.map((item) => JSON.parse(item)).some((item) => item.type === 'ssh_exec_process')).toBe(true)
    sockets[0].onmessage?.({ data: JSON.stringify({ type: 'exec_result', exec_id: 'exec-1', case_id: 'Q2026081100001' }) })
    await flushPromises()

    expect(wrapper.text()).toContain('terminal_bridge exec_result 缺少有效的整数 exit_code')
    expect(sockets).toHaveLength(1)
    const connectMessages = sockets.flatMap((socket) => socket.sent.map((item) => JSON.parse(item))).filter((item) => item.type === 'ssh_connect')
    expect(connectMessages).toHaveLength(1)
    expect(connectMessages[0].password).toBe('htp2.test')
    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith('/result'))).toBe(false)
  })
})
