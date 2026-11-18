/**
 * acli 插件第二阶段（S4b/S4c）客户侧凭据：按工单取一张短期只读票据。
 *
 * 第一性原理：改节点的是 **terminal_bridge**，它跑在客户机上，是不受信进程。
 * 让它读到平台插件期望态的唯一方式是给它一张短期、只读、绑定工单的票据——
 * 不是管理令牌（那等于把运维权限发到客户机上），也不是客户身份 Cookie
 * （浏览器会话身份不该出现在桥的 HTTP 头里）。票据只能换 manifest 与插件包，
 * TTL 由平台收敛到 ≤10 分钟，桥不持有它也无法伪造它。
 *
 * 失败必须 fail-soft：取不到票就当本次没启用阶段二（阶段一与终端/日志业务照旧）。
 * 但**不能静默**：把原因回给调用方上屏，否则"插件为什么没更新"又变成一个只能登
 * 节点翻日志的问题（这正是本轮链路收口要消灭的那类现场）。
 */
import { createApiClient } from '@hci/shared'

const client = createApiClient('/api')

export interface AcliPluginTicketResponse {
  ticket: string
  expires_at: number
  case_id: string
}

/** 随 acli_sync 下发的阶段二凭据；两者必须成对，缺一个桥就当未启用。 */
export interface AcliPluginSyncCredentials {
  platformUrl?: string
  pluginTicket?: string
}

/**
 * 桥应当回连的平台根地址。
 *
 * 用浏览器正在访问的 origin：浏览器与桥同在客户机上，浏览器能打开平台，
 * 桥按这个地址就打得通；反之另配一个内网地址，客户机上往往根本路由不到。
 * 协议由 origin 决定——http 的公网站点会在桥侧被拒（只允许回环 http），
 * 这是刻意的：票据虽然只读，明文链路下一样能被中间人拿去拉包。
 */
export function platformRootUrl(): string {
  return window.location.origin.replace(/\/+$/, '')
}

/** 取票。失败抛出，由 resolveAcliPluginSyncCredentials 收敛成"未启用"。 */
export async function requestAcliPluginTicket(caseId: string): Promise<AcliPluginTicketResponse> {
  const { data } = await client.post('/acli-plugins/ticket', { case_id: caseId })
  const ticket = String(data?.ticket || '')
  if (!ticket) {
    throw new Error('平台未返回插件同步票据')
  }
  return {
    ticket,
    expires_at: Number(data?.expires_at || 0),
    case_id: String(data?.case_id || caseId),
  }
}

/**
 * 组装阶段二凭据。
 *
 * 没有真实工单号（如未建单的临时终端会话）就不去取票：票据绑定工单，
 * 拿 `terminal-global` 这种占位值调平台只会换一个 404，再把 404 当成"平台故障"
 * 报给用户。这里直接返回空凭据，阶段二不启用。
 */
export async function resolveAcliPluginSyncCredentials(
  caseId: string,
): Promise<{ credentials: AcliPluginSyncCredentials; error: string }> {
  const wanted = (caseId || '').trim()
  if (!wanted || wanted === 'terminal-global' || wanted === 'ssh-create-temp') {
    return { credentials: {}, error: '' }
  }
  try {
    const issued = await requestAcliPluginTicket(wanted)
    return {
      credentials: { platformUrl: platformRootUrl(), pluginTicket: issued.ticket },
      error: '',
    }
  } catch (error) {
    return { credentials: {}, error: error instanceof Error ? error.message : String(error) }
  }
}
