import { afterEach, describe, expect, it } from 'vitest'
import {
  buildDeliverySubmitMessage,
  buildDeliverySubmitPayload,
  describeRelayProgress,
  describeUploadProgress,
  formatFailureReason,
  hasDeliveryEvidence,
  isStatusTransitionRejected,
  buildResultHeadline,
  canResumeDeliveryAfterReject,
  describeArchiveCheck,
  isCertificateUntrusted,
  isTerminalDeliveryFailure,
  p1ArchiveCheckOf,
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

describe('isTerminalDeliveryFailure', () => {
  // 现场回归：bridge 在退避重排期间对外报告 retrying。
  // 若前端把它当失败，就会提前抛错并回写 delivery_status=failed，
  // 而字节其实还在往平台传 —— 三份状态互相打脸。
  it('retrying 是中间态，不能判死', () => {
    expect(isTerminalDeliveryFailure('retrying')).toBe(false)
  })

  it('只有 failed 是终态失败', () => {
    expect(isTerminalDeliveryFailure('failed')).toBe(true)
  })

  it('进行中与已完成都不算失败', () => {
    for (const phase of ['waiting_collect', 'pulling', 'uploading', 'completing', 'done', '', undefined]) {
      expect(isTerminalDeliveryFailure(phase)).toBe(false)
    }
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

describe('formatFailureReason', () => {
  const FALLBACK = '归档到日志服务器失败，平台未记录原因（可在桥日志中按工单号检索）'

  it('码与原因都有时拼成 [码] 原因', () => {
    expect(formatFailureReason({ error_code: 'LC-TRANSFER-003', error_message: '5888 返回 503' }, FALLBACK)).toBe(
      '[LC-TRANSFER-003] 5888 返回 503',
    )
  })

  // 这条是本次改动的核心：Q2026102801062 界面指向"任务记录"，记录里却什么都没有。
  it('无码无因时如实说明「平台未记录」，不编造原因', () => {
    expect(formatFailureReason(null, FALLBACK)).toBe(FALLBACK)
    expect(formatFailureReason(undefined, FALLBACK)).toBe(FALLBACK)
    expect(formatFailureReason({ error_code: null, error_message: '   ' }, FALLBACK)).toBe(FALLBACK)
  })

  it('只有码没有原因时保留码（码本身就能报给支持），再补兜底文案', () => {
    expect(formatFailureReason({ error_code: 'LC-TRANSFER-002' }, FALLBACK)).toBe(`[LC-TRANSFER-002] ${FALLBACK}`)
  })

  it('只有原因没有码时不加空方括号', () => {
    expect(formatFailureReason({ error_message: '平台副本为空文件' }, FALLBACK)).toBe('平台副本为空文件')
  })

  it('原因两端空白会被裁掉，避免展示出多余空行', () => {
    expect(formatFailureReason({ error_message: '  归档超时  ' }, FALLBACK)).toBe('归档超时')
  })
})

describe('describeRelayProgress', () => {
  it('正常进度只显示片号，不显示「第 1 次尝试」这类噪声', () => {
    expect(describeRelayProgress({ relay_chunk_index: 2, relay_chunk_total: 10, relay_attempt: 1 })).toBe(
      '第 3/10 片',
    )
  })

  it('重试过的轮次要显式写出来', () => {
    expect(describeRelayProgress({ relay_chunk_index: 2, relay_chunk_total: 10, relay_attempt: 3 })).toBe(
      '第 3/10 片 · 第 3 次尝试',
    )
  })

  // 这条对应 Q2026102801062 的体感：单片 300s 超时期间 chunk_index 不动，
  // 界面与"已卡死"同形，必须给出上一轮的失败原因。
  it('带上上一轮失败原因', () => {
    expect(
      describeRelayProgress({
        relay_chunk_index: 0,
        relay_chunk_total: 409,
        relay_attempt: 2,
        relay_last_error: '5888 返回 503',
      }),
    ).toBe('第 1/409 片 · 第 2 次尝试 · 上次失败：5888 返回 503')
  })

  it('只有总片数（还没开始传）时说明准备中', () => {
    expect(describeRelayProgress({ relay_chunk_total: 409 })).toBe('共 409 片，准备中')
  })

  it('什么都不知道时不能说谎，只能写准备中', () => {
    expect(describeRelayProgress(null)).toBe('准备中')
    expect(describeRelayProgress({})).toBe('准备中')
  })

  it('超长错误截断，避免把单行进度挤没', () => {
    const text = describeRelayProgress({ relay_chunk_total: 1, relay_last_error: 'x'.repeat(300) })
    expect(text).toContain('…')
    expect(text.length).toBeLessThan(200)
  })
})

describe('describeUploadProgress', () => {
  const now = Date.parse('2026-11-06T13:40:00Z')

  it('没有进度快照时返回空串，让调用方回落到原文案', () => {
    expect(describeUploadProgress(null)).toBe('')
    expect(describeUploadProgress({})).toBe('')
    expect(describeUploadProgress({ uploaded: 0, total: 233 })).toBe('')
    expect(describeUploadProgress({ uploaded: 19, total: 0 })).toBe('')
  })

  it('一分钟内刚写过：说"刚刚"，不报分钟数', () => {
    expect(
      describeUploadProgress({ uploaded: 19, total: 233, updated_at: '2026-11-06T13:39:30Z' }, now),
    ).toBe('平台已收到 19/233 片（刚刚仍在写入）')
  })

  it('十分钟内没动：如实说几分钟前，但不预设失败', () => {
    expect(
      describeUploadProgress({ uploaded: 19, total: 233, updated_at: '2026-11-06T13:33:00Z' }, now),
    ).toBe('平台已收到 19/233 片（7 分钟前仍在写入）')
  })

  // Q2026110600112：卡在 19/233 十小时，界面全程只说「交付已登记但未完成」，
  // 用户无法判断"再等等"还是"这条链已经废了"。
  it('超过十分钟没动：明确提示链路可能停滞', () => {
    expect(
      describeUploadProgress({ uploaded: 19, total: 233, updated_at: '2026-11-06T03:40:00Z' }, now),
    ).toBe('平台已收到 19/233 片（已 600 分钟没有新增分片，链路可能已停滞）')
  })

  it('浏览器时钟落后于服务端时不许出现负数', () => {
    expect(
      describeUploadProgress({ uploaded: 5, total: 233, updated_at: '2026-11-06T13:45:00Z' }, now),
    ).toBe('平台已收到 5/233 片（刚刚仍在写入）')
  })

  it('时间戳缺失或非法时只报片数', () => {
    expect(describeUploadProgress({ uploaded: 5, total: 233 }, now)).toBe('平台已收到 5/233 片')
    expect(describeUploadProgress({ uploaded: 5, total: 233, updated_at: 'oops' }, now)).toBe(
      '平台已收到 5/233 片',
    )
  })
})

// 现场回归（Q2026110900356，2026-11-09）：`status=completed` + 平台零产物 + 5888
// `found=false`，界面却给了绿色成功横幅。根因是"采集完成"被当成了"日志已交付"。
describe('hasDeliveryEvidence', () => {
  it('P2 只认 relay 成功或 delivered，采集完成不算交付', () => {
    expect(hasDeliveryEvidence({ upload_mode: 'bridge_relay', relay_status: 'none', delivery_status: 'queued' })).toBe(
      false,
    )
    expect(hasDeliveryEvidence({ upload_mode: 'bridge_relay', relay_status: 'running' })).toBe(false)
    expect(hasDeliveryEvidence({ upload_mode: 'bridge_relay', relay_status: 'success' })).toBe(true)
    expect(hasDeliveryEvidence({ upload_mode: 'bridge_relay', delivery_status: 'delivered' })).toBe(true)
  })

  it('P1 用 delivered 或"插件自删压缩包"兜底，老记录按 P1 口径', () => {
    expect(hasDeliveryEvidence({ upload_mode: 'acli_direct', delivery_status: 'delivered' })).toBe(true)
    expect(hasDeliveryEvidence({ upload_mode: 'acli_direct', archive_deleted: true })).toBe(true)
    expect(hasDeliveryEvidence({ upload_mode: 'acli_direct' })).toBe(false)
    expect(hasDeliveryEvidence({ remote_url: 'http://acli.sangfor.com.cn:5888' })).toBe(true)
  })

  it('空任务不给任何成功结论', () => {
    expect(hasDeliveryEvidence(null)).toBe(false)
    expect(hasDeliveryEvidence({})).toBe(false)
  })
})

describe('isStatusTransitionRejected', () => {
  const axiosError = (status: number, code?: string) => ({
    isAxiosError: true,
    response: { status, data: code ? { detail: { code } } : undefined },
    problem: code ? { code } : undefined,
  })

  // 交付与采集正交：completed → uploading 被拒不能中断搬运
  it('只认 409 + LC-PLATFORM-003', () => {
    expect(isStatusTransitionRejected(axiosError(409, 'LC-PLATFORM-003'))).toBe(true)
  })

  it('同是 409 的业务冲突必须原样抛出（LC-PLUGIN-007 已有进行中任务）', () => {
    expect(isStatusTransitionRejected(axiosError(409, 'LC-PLUGIN-007'))).toBe(false)
  })

  it('其它状态码与普通 Error 不算状态机拒绝', () => {
    expect(isStatusTransitionRejected(axiosError(500, 'LC-PLATFORM-003'))).toBe(false)
    expect(isStatusTransitionRejected(new Error('非法状态转换'))).toBe(false)
    expect(isStatusTransitionRejected(null)).toBe(false)
  })
})

describe('buildResultHeadline', () => {
  const WHEN = '11-09 03:39'

  // 本次事故的核心断言：采完但没交付，必须是 warning 而不是绿色 success
  it('采集完成但没有交付证据：不允许出现 success 横幅', () => {
    const headline = buildResultHeadline({ status: 'completed', delivered: false, when: WHEN })
    expect(headline.type).toBe('warning')
    expect(headline.title).toContain('未核验到归档件')
  })

  /**
   * 现场回归（工单 Q2026111000243，2026-11-10）：
   * P1 已上传成功、包就在 /sf/log，界面却写「平台没有收到日志包，日志服务器上也没有归档包」，
   * 用户据此认定本次上传失败。平台对外部系统的断言只能到"本次回查没看到"这一层。
   */
  it('没有交付证据时不得断言"日志服务器上也没有归档件"', () => {
    const headline = buildResultHeadline({ status: 'completed', delivered: false, when: WHEN })
    expect(headline.description).not.toContain('没有收到日志包')
    expect(headline.description).not.toContain('也没有归档件')
  })

  it('带上核验快照时按观测时刻与结果如实表述', () => {
    const headline = buildResultHeadline({
      status: 'completed',
      delivered: false,
      when: WHEN,
      archiveCheck: {
        verified: false,
        reason: 'archive_missing',
        checked_at: '2026-11-10T03:01:00Z',
      },
    })
    expect(headline.description).toContain('最近一次核验')
    expect(headline.description).toContain('暂未查到该工单的归档件')
    // 只是一次观测，不能升级成"上传失败"
    expect(headline.description).not.toContain('上传失败')
  })

  it('没有「上传到平台」按钮时不得让用户去点它', () => {
    const headline = buildResultHeadline({
      status: 'completed',
      delivered: false,
      when: WHEN,
      canUpload: false,
    })
    expect(headline.description).toContain('重新核验归档')
    expect(headline.description).not.toContain('上传到平台')
  })

  it('包仍在节点（有补交付入口）时才提「上传到平台」', () => {
    const headline = buildResultHeadline({
      status: 'completed',
      delivered: false,
      when: WHEN,
      canUpload: true,
    })
    expect(headline.description).toContain('上传到平台')
  })

  it('采集完成且已交付才给 success', () => {
    const headline = buildResultHeadline({ status: 'completed', delivered: true, when: WHEN })
    expect(headline.type).toBe('success')
    expect(headline.title).toContain('并已交付')
  })

  it('失败/取消的语气保持不变', () => {
    expect(buildResultHeadline({ status: 'failed', delivered: false, when: WHEN }).type).toBe('error')
    expect(buildResultHeadline({ status: 'cancelled', delivered: false, when: WHEN }).type).toBe('info')
    expect(buildResultHeadline({ status: null }).title).toBe('')
  })

  /**
   * 回归（2026-11-12 Q2026110302155）：横幅原本写"可点重新收集再试一次"，
   * 客户 100 秒内连点 5 次，三次失败其实踩在三个不同阶段。
   */
  it('失败横幅带错误码与结论，且不再劝用户盲目重试', () => {
    const headline = buildResultHeadline({
      status: 'failed',
      delivered: false,
      when: WHEN,
      errorCode: 'LC-SPC-010',
      hintTitle: '多台节点场景清单写入失败',
    })
    expect(headline.description).toContain('LC-SPC-010')
    expect(headline.description).toContain('多台节点场景清单写入失败')
    expect(headline.description).toContain('如何检查')
    expect(headline.description).toContain('如何处理')
    expect(headline.description).toContain('确认原因消除后再点')
    expect(headline.description).not.toContain('再试一次')
  })

  it('没有可识别错误码时横幅指向任务详情，不编造结论', () => {
    const headline = buildResultHeadline({ status: 'failed', when: WHEN })
    expect(headline.description).toContain('平台未给出可识别的错误码')
    expect(headline.description).toContain('error_message')
    expect(headline.description).not.toContain('LC-')
  })

  it('有码但映射表没有结论时只报码，不补猜的原因', () => {
    const headline = buildResultHeadline({
      status: 'failed',
      when: WHEN,
      errorCode: 'LC-UNKNOWN-999',
    })
    expect(headline.description).toContain('LC-UNKNOWN-999')
    expect(headline.description).not.toContain('（')
  })

  it('取消态先让人确认原因，而不是直接催重新收集', () => {
    const headline = buildResultHeadline({ status: 'cancelled', when: WHEN })
    expect(headline.description).toContain('若非本人取消')
    expect(headline.description).toContain('如何检查')
    expect(headline.description).not.toContain('再试一次')
  })
})

describe('P1 归档核验快照', () => {
  it('结构不对一律当"没有快照"，不猜', () => {
    expect(p1ArchiveCheckOf(null)).toBeNull()
    expect(p1ArchiveCheckOf({})).toBeNull()
    expect(p1ArchiveCheckOf({ detail: { p1_archive_check: 'oops' } })).toBeNull()
    expect(p1ArchiveCheckOf({ detail: { p1_archive_check: { verified: false } } })).not.toBeNull()
  })

  it('体积不符时把两个数都摆出来，供人工对照', () => {
    const text = describeArchiveCheck({
      verified: false,
      reason: 'size_mismatch',
      checked_at: '2026-11-10T03:01:00Z',
      expected_size: 885_000_000,
      archived_size: 884_000_000,
    })
    expect(text).toContain('843.0 MB')
    expect(text).toContain('844.0 MB')
    expect(text).toContain('小于平台记录')
  })

  it('查不到只说"暂未查到"，不替日志服务器下"没有"的结论', () => {
    const text = describeArchiveCheck({ verified: false, reason: 'archive_missing' })
    expect(text).toContain('暂未查到')
  })

  it('核验通过时明确说已查到归档件', () => {
    expect(describeArchiveCheck({ verified: true })).toContain('已查到该工单的归档件')
    expect(describeArchiveCheck(null)).toBe('')
  })
})

// 对抗性审查（2026-11-09）：`cancelled → uploading` 同样是 LC-PLATFORM-003。
// 吞掉这个码之后必须用服务端最新状态裁决，否则会把用户已取消的包照常传出去。
describe('canResumeDeliveryAfterReject', () => {
  it('只有采集已完成才继续交付', () => {
    expect(canResumeDeliveryAfterReject('completed')).toBe(true)
  })

  it('取消/失败/未知一律中止，不用交付把终态复活', () => {
    expect(canResumeDeliveryAfterReject('cancelled')).toBe(false)
    expect(canResumeDeliveryAfterReject('failed')).toBe(false)
    expect(canResumeDeliveryAfterReject('uploading')).toBe(false)
    expect(canResumeDeliveryAfterReject(null)).toBe(false)
    expect(canResumeDeliveryAfterReject(undefined)).toBe(false)
    expect(canResumeDeliveryAfterReject('  ')).toBe(false)
  })
})
