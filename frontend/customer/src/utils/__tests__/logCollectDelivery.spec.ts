import { afterEach, describe, expect, it } from 'vitest'
import {
  buildDeliverySubmitMessage,
  buildDeliverySubmitPayload,
  isCertificateUntrusted,
  platformCertTrusted,
  rememberPlatformCertTrust,
} from '../logCollectDelivery'
import type { UploadInitResult } from '@/api/logCollect'

const session: UploadInitResult = {
  upload_id: 'u-1',
  chunk_size: 10485760,
  part_count: 15,
  total_size: 150000000,
  uploaded_parts: [1, 2],
  missing_parts: [3],
  upload_token: 'tok-abc',
}

const baseArgs = {
  taskId: 'task-1',
  sourcePath: '/sf/data/local/acli/tmp/data/log_collect/Q1.zip',
  totalSize: 150000000,
  session,
  mode: 'collect' as const,
}

describe('buildDeliverySubmitPayload', () => {
  // 这条断言直接对应 2026-10-27 现场：上限 15 片全部报 x509，
  // 根因就是载荷里没有 tls_insecure，bridge 的放宽分支永远进不去。
  // 一旦有人"清理无用字段"把它删掉，这里必须立刻红。
  // ⚠️ 字段名必须是 snake_case，与 bridge（terminal_bridge/main.go 的 InMessage
  // Transfer* 的 json tag）严格一致；历史上前端发 camelCase 导致这些字段全部收不到，
  // delivery 从 #1318 起就没真正把参数送到 bridge。
  it('必须携带 tls_insecure，且如实反映授权状态', () => {
    expect(buildDeliverySubmitPayload(baseArgs, 'https://platform:4443', true).tls_insecure).toBe(true)
    expect(buildDeliverySubmitPayload(baseArgs, 'https://platform:4443', false).tls_insecure).toBe(false)
  })

  it('平台地址取当前页面 origin，不硬编码', () => {
    const payload = buildDeliverySubmitPayload(baseArgs, 'https://acli.example.cn:4443', true)
    expect(payload.platform_url).toBe('https://acli.example.cn:4443')
  })

  it('分片参数完整透传（snake_case），缺一 bridge 就无法搬运', () => {
    const payload = buildDeliverySubmitPayload(baseArgs, 'https://p', false)
    expect(payload.exec_id).toBe('task-1')
    expect(payload.source_path).toBe(baseArgs.sourcePath)
    expect(payload.upload_token).toBe('tok-abc')
    expect(payload.chunk_size).toBe(10485760)
    expect(payload.part_count).toBe(15)
    expect(payload.total_size).toBe(150000000)
  })

  it('已传分片用于续传，不能丢', () => {
    expect(buildDeliverySubmitPayload(baseArgs, 'https://p', false).uploaded_parts).toEqual([1, 2])
  })

  it('平台没返回已传分片时降级为空数组，而不是 undefined', () => {
    const payload = buildDeliverySubmitPayload(
      { ...baseArgs, session: { ...session, uploaded_parts: undefined as unknown as number[] } },
      'https://p',
      false,
    )
    expect(payload.uploaded_parts).toEqual([])
  })

  it('delivery_mode 区分采集衔接与补交付', () => {
    expect(buildDeliverySubmitPayload(baseArgs, 'https://p', false).delivery_mode).toBe('collect')
    expect(
      buildDeliverySubmitPayload({ ...baseArgs, mode: 'direct' }, 'https://p', false).delivery_mode,
    ).toBe('direct')
  })
})

describe('isCertificateUntrusted', () => {
  // 文案取自 bridge 真实回包（Go 标准库原文），不是臆造的样例
  it('识别现场真实错误', () => {
    expect(
      isCertificateUntrusted(
        '分片 2 上传失败: Put "https://acli.sangfor.com.cn:4443/api/direct/log-collect-uploads/x/parts/2": ' +
          'tls: failed to verify certificate: x509: certificate signed by unknown authority',
      ),
    ).toBe(true)
  })

  it('识别两种常见变体', () => {
    expect(isCertificateUntrusted('x509: certificate signed by unknown authority')).toBe(true)
    expect(isCertificateUntrusted('tls: failed to verify certificate')).toBe(true)
  })

  it('不把网络错误误判成证书问题', () => {
    expect(isCertificateUntrusted('dial tcp 10.0.0.1:4443: connect: connection refused')).toBe(false)
    expect(isCertificateUntrusted('源包不完整（尾部无 zip 结束标记）')).toBe(false)
    expect(isCertificateUntrusted(undefined)).toBe(false)
  })
})

describe('平台证书信任授权', () => {
  afterEach(() => {
    sessionStorage.clear()
  })

  it('授权按 origin 隔离', () => {
    rememberPlatformCertTrust('https://a.example.cn:4443')
    expect(platformCertTrusted('https://a.example.cn:4443')).toBe(true)
    // 换成另一个平台地址不能自动继承授权，否则一次授权等于全局放行
    expect(platformCertTrusted('https://b.example.cn:4443')).toBe(false)
  })

  it('默认未授权', () => {
    expect(platformCertTrusted('https://c.example.cn:4443')).toBe(false)
  })

  // 授权必须随会话结束而消失：写 localStorage 等于给域名留永久后门
  it('不写入 localStorage', () => {
    rememberPlatformCertTrust('https://d.example.cn:4443')
    expect(localStorage.getItem('hci.logCollect.trustPlatformCert:https://d.example.cn:4443')).toBeNull()
  })
})
// 现场回归（Q2026102701193，2026-10-27）：`source_path 不能为空`。
//
// 根因不在载荷构造，而在**载荷→WS 消息**这一跳：历史上 submit 复用了
// buildDeliveryMessage（按 camelCase 取字段），snake_case 载荷被逐字段丢空，
// bridge 只收到 {type, case_id, node_ip}。更要命的是 delivery_mode / tls_insecure
// 在那个 options 类型里根本不存在，所以 P2 交付与证书授权从未送达。
//
// 这条用例刻意用**真实函数组合**（payload → message）而不是分别断言，
// 因为上一轮的组件测试 mock 了消息层、直接透传 payload，把断裂完全掩盖了。
describe('buildDeliverySubmitMessage', () => {
  const decode = (raw: string) => JSON.parse(raw) as Record<string, unknown>

  it('协议载荷的每个字段都必须原样出现在消息里', () => {
    const payload = buildDeliverySubmitPayload(baseArgs, 'https://platform.example.cn', true)
    const msg = decode(buildDeliverySubmitMessage(payload, 'Q2026102701193', '172.28.24.194'))

    expect(msg.type).toBe('delivery_submit')
    expect(msg.case_id).toBe('Q2026102701193')
    expect(msg.node_ip).toBe('172.28.24.194')
    // 逐个字段对照 bridge 的 InMessage json tag
    expect(msg.exec_id).toBe('task-1')
    expect(msg.source_path).toBe(baseArgs.sourcePath)
    expect(msg.platform_url).toBe('https://platform.example.cn')
    expect(msg.upload_token).toBe('tok-abc')
    expect(msg.chunk_size).toBe(10485760)
    expect(msg.part_count).toBe(15)
    expect(msg.total_size).toBe(150000000)
    expect(msg.uploaded_parts).toEqual([1, 2])
    expect(msg.delivery_mode).toBe('collect')
    expect(msg.tls_insecure).toBe(true)
  })

  it('direct 模式必须送达（补交付的唯一开关）', () => {
    const payload = buildDeliverySubmitPayload({ ...baseArgs, mode: 'direct' }, 'https://p.example.cn', false)
    const msg = decode(buildDeliverySubmitMessage(payload, 'Q1'))
    expect(msg.delivery_mode).toBe('direct')
    // tls_insecure=false 也必须显式传：缺字段时 bridge 会走严格分支
    expect(msg.tls_insecure).toBe(false)
  })

  it('uploaded_parts 为空数组也要传：漏传会让续传变成全量重传', () => {
    const payload = buildDeliverySubmitPayload(
      { ...baseArgs, session: { ...session, uploaded_parts: [] } },
      'https://p.example.cn',
      false,
    )
    const msg = decode(buildDeliverySubmitMessage(payload, 'Q1'))
    expect(msg.uploaded_parts).toEqual([])
  })
})
