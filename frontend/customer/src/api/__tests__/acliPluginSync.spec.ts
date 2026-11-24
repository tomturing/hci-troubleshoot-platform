/**
 * 阶段二凭据（取票）的客户侧契约。
 *
 * 三条不可让渡的性质：
 *  1. 占位工单不发请求：票据绑定工单，拿 `terminal-global` 去要只会换一个 404，
 *     再把 404 当成"平台故障"报给用户，比不启用更难排查；
 *  2. 成功必须给出**成对**凭据：只带 origin 或只带 ticket 都不下发（桥侧当未启用）；
 *  3. 失败必须 fail-soft 且**不静默**：返回空凭据 + 原因文本，由调用方上屏。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

const { postMock } = vi.hoisted(() => ({ postMock: vi.fn() }))

vi.mock('@hci/shared', () => ({
  createApiClient: () => ({ post: postMock }),
}))

import {
  platformRootUrl,
  requestAcliPluginTicket,
  resolveAcliPluginSyncCredentials,
} from '../acliPluginSync'
import { rememberPlatformCertTrust } from '@/utils/logCollectDelivery'

describe('platformRootUrl', () => {
  it('取浏览器正在访问的 origin，并去掉末尾斜杠', () => {
    // 末尾斜杠会拼成 https://host//api/... ：桥按精确路径匹配平台端点，
    // 双斜杠在部分网关上直接 404，表现成"取到票却拉不到清单"。
    expect(platformRootUrl().endsWith('/')).toBe(false)
    expect(platformRootUrl()).toBe(window.location.origin.replace(/\/+$/, ''))
  })
})

describe('requestAcliPluginTicket', () => {
  beforeEach(() => {
    postMock.mockReset()
  })

  it('POST /acli-plugins/ticket 并回传票据字段', async () => {
    postMock.mockResolvedValue({ data: { ticket: 'tk-1', expires_at: 1760000000, case_id: 'Q1' } })
    const issued = await requestAcliPluginTicket('Q1')
    expect(postMock).toHaveBeenCalledWith('/acli-plugins/ticket', { case_id: 'Q1' })
    expect(issued).toEqual({ ticket: 'tk-1', expires_at: 1760000000, case_id: 'Q1' })
  })

  it('响应里没有票据时抛错，而不是返回一张空票', async () => {
    // 空票会被当成"已启用阶段二"下发给桥，桥必然吃 401，报出来的却是平台故障。
    postMock.mockResolvedValue({ data: { expires_at: 1 } })
    await expect(requestAcliPluginTicket('Q1')).rejects.toThrow('未返回插件同步票据')
  })
})

describe('resolveAcliPluginSyncCredentials', () => {
  beforeEach(() => {
    postMock.mockReset()
  })

  it('占位工单不取票：阶段二不启用，也不报任何错误', async () => {
    for (const placeholder of ['terminal-global', 'ssh-create-temp', '']) {
      const resolved = await resolveAcliPluginSyncCredentials(placeholder)
      expect(resolved).toEqual({ credentials: {}, error: '' })
    }
    expect(postMock).not.toHaveBeenCalled()
  })

  it('真实工单：给出成对凭据', async () => {
    postMock.mockResolvedValue({ data: { ticket: 'tk-9', case_id: 'Q2026111800001' } })
    const resolved = await resolveAcliPluginSyncCredentials('Q2026111800001')
    expect(resolved.error).toBe('')
    expect(resolved.credentials.pluginTicket).toBe('tk-9')
    expect(resolved.credentials.platformUrl).toBe(platformRootUrl())
  })

  it('取票失败只降级不留静默：返回空凭据 + 原因', async () => {
    postMock.mockRejectedValue(new Error('503 Case Service unavailable'))
    const resolved = await resolveAcliPluginSyncCredentials('Q2026111800002')
    expect(resolved.credentials).toEqual({})
    expect(resolved.error).toContain('Case Service unavailable')
  })

  // 正式环境（staging/prod）平台是自签 HTTPS：桥拉 manifest 走严格 TLS 必然 x509。
  // 授权位必须随凭据一起下发，否则正式环境永远装不上插件（只有回环 http 能跑通）。
  it('自签证书：会话内已授权才带 tls_insecure，未授权时保持严格校验', async () => {
    sessionStorage.clear()
    postMock.mockResolvedValue({ data: { ticket: 'tk-9', case_id: 'Q1' } })

    const before = await resolveAcliPluginSyncCredentials('Q1')
    expect(before.credentials.tlsInsecure).toBe(false)

    rememberPlatformCertTrust(platformRootUrl())
    const after = await resolveAcliPluginSyncCredentials('Q1')
    expect(after.credentials.tlsInsecure).toBe(true)
    sessionStorage.clear()
  })
})
