import { afterEach, describe, expect, it } from 'vitest'
import {
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
  it('必须携带 tlsInsecure，且如实反映授权状态', () => {
    expect(buildDeliverySubmitPayload(baseArgs, 'https://platform:4443', true).tlsInsecure).toBe(true)
    expect(buildDeliverySubmitPayload(baseArgs, 'https://platform:4443', false).tlsInsecure).toBe(false)
  })

  it('平台地址取当前页面 origin，不硬编码', () => {
    const payload = buildDeliverySubmitPayload(baseArgs, 'https://acli.example.cn:4443', true)
    expect(payload.platformUrl).toBe('https://acli.example.cn:4443')
  })

  it('分片参数完整透传，缺一 bridge 就无法搬运', () => {
    const payload = buildDeliverySubmitPayload(baseArgs, 'https://p', false)
    expect(payload.execId).toBe('task-1')
    expect(payload.sourcePath).toBe(baseArgs.sourcePath)
    expect(payload.uploadToken).toBe('tok-abc')
    expect(payload.chunkSize).toBe(10485760)
    expect(payload.partCount).toBe(15)
    expect(payload.totalSize).toBe(150000000)
  })

  it('已传分片用于续传，不能丢', () => {
    expect(buildDeliverySubmitPayload(baseArgs, 'https://p', false).uploadedParts).toEqual([1, 2])
  })

  it('平台没返回已传分片时降级为空数组，而不是 undefined', () => {
    const payload = buildDeliverySubmitPayload(
      { ...baseArgs, session: { ...session, uploaded_parts: undefined as unknown as number[] } },
      'https://p',
      false,
    )
    expect(payload.uploadedParts).toEqual([])
  })

  it('deliveryMode 区分采集衔接与补交付', () => {
    expect(buildDeliverySubmitPayload(baseArgs, 'https://p', false).deliveryMode).toBe('collect')
    expect(
      buildDeliverySubmitPayload({ ...baseArgs, mode: 'direct' }, 'https://p', false).deliveryMode,
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