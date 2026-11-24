/**
 * 采集前台等待预算：到点后该如何下结论（纯函数，便于单测）。
 *
 * 第一性原理：前台的「等待预算」回答的是「人愿意在页面上盯多久」，
 * 而不是「这次采集应该多久跑完」。两者混用就会在预算耗尽时替节点下结论。
 *
 * 2026-11-24 两起现场（Q2026112400468 9.86 GiB、Q2026091700436 9.84 GiB）都是
 * P1 直传大包：节点 9/9 已采完、包已合成、正在按 ~2 MB/s 发送，`exit_code=0`；
 * 前端第 720 次轮询耗尽后抛出「采集未完成」，后端按关键词归因成
 * `LC-PLUGIN-010`（插件返回非零，PLUGIN 层）。事实与结论完全相反，用户被推向
 * 「重新采集」——而节点插件单任务互斥，且几十 GB 已传的字节只能从头再来。
 *
 * 所以到点只有两条合法出路：
 * - 节点自己已给出完成结论 → 交给既有收尾（finalize），不改写事实；
 * - 其余一律转入后台观察（observe）：不写终态、失败码留给节点的真实失败。
 */

import { classifyUploadState, type NodeState } from '@/services/logCollectSync'
import { formatByteCount } from '@/services/logCollectProgress'

/**
 * 前台等待预算：1 小时。
 *
 * 取值依据不是「大包能传完」，而是「用户愿意盯着的时长」。绝大多数小包
 * （<1 GiB）在这段时间内能走完整条链路；大包超时后转后台观察，用户随时可以
 * 关掉页面或回来看结果，不必有人守着。
 */
export const COLLECT_FOREGROUND_BUDGET_MS = 60 * 60 * 1000

/**
 * 观察模式的对账间隔。
 *
 * 比 5s 的主流程轮询慢：此时已经没有人在等一个即时结论，而降频能把
 * SSH exec 通道的压力从「每小时 720 次」降到 240 次以内。
 */
export const OBSERVE_POLL_INTERVAL_MS = 15 * 1000

/**
 * 循环安全阀（与时间预算互为兜底）。
 *
 * 时间预算是唯一判据；这个上限只在 `Date.now()` 被冻结（测试里的假时钟）时
 * 防止死循环，正常路径永远不会触碰。
 */
export const COLLECT_POLL_SAFETY_MAX = 4000

/** 到点后的两种合法出路。 */
export type CollectWaitExpiryMode = 'finalize' | 'observe'

export interface CollectWaitExpiryInput {
  /** 最后一次成功采到的节点状态；一次都没采到时为 null。 */
  nodeState?: NodeState | null
  /** 节点自报的上传阶段原文（中文「发送中」/ 英文 `Sending`）。 */
  uploadState?: string | null
  /** 已完成的采集台数 / 应采集台数。 */
  done?: number | null
  total?: number | null
  /** 节点侧压缩包字节数（包合成后才有值）。 */
  archiveSize?: number | null
}

export interface CollectWaitExpiryPlan {
  mode: CollectWaitExpiryMode
  /** 给用户看的一句话：只用可核对的事实（台数、包大小），不猜进度。 */
  notice: string
}

/** 转入后台观察时写入 detail 的审计快照，供事后回答“为什么降频/为什么没判失败”。 */
export interface ObserveHandoffAudit {
  at: string
  node_state: NodeState | 'unknown'
  upload_state: string | null
  done: number | null
  total: number | null
  archive_size: number | null
  notice: string
  trace_id: string | null
}

function finiteNumber(value: unknown): number | null {
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

/**
 * 构造观察交接审计。
 *
 * 只写事实，不写推测：节点态、上传原文、台数、包大小、提示语与 trace_id。
 * 审计写 detail 而不是 status/error_code，避免把“本端停止等待”伪装成节点失败。
 */
export function buildObserveHandoffAudit(
  input: CollectWaitExpiryInput & {
    notice: string
    traceId?: string | null
    at?: string
  },
): ObserveHandoffAudit {
  return {
    at: input.at ?? new Date().toISOString(),
    node_state: input.nodeState ?? 'unknown',
    upload_state: input.uploadState ?? null,
    done: finiteNumber(input.done),
    total: finiteNumber(input.total),
    archive_size: finiteNumber(input.archiveSize),
    notice: input.notice,
    trace_id: input.traceId ?? null,
  }
}

/** 页面重开时判断任务是否已交接过后台观察；只有 collecting 才应恢复降频。 */
export function hasObserveHandoff(detail: Record<string, unknown> | null | undefined): boolean {
  const audit = detail?.['observation_handoff']
  return Boolean(audit && typeof audit === 'object')
}

/**
 * 预算到点时的结论与文案。
 *
 * 唯一的 `finalize` 条件是节点已经 `completed`：那说明收尾逻辑本该在这一轮
 * 之前就返回，只是最后一拍被睡眠切开。除此之外**任何**状态（running / unknown /
 * 一次都没采到）都只能 observe——没有失败证据时判失败，就是替节点撒谎。
 */
export function planCollectWaitExpiry(input: CollectWaitExpiryInput): CollectWaitExpiryPlan {
  if ((input.nodeState ?? 'unknown') === 'completed') {
    return {
      mode: 'finalize',
      notice: '节点已报告采集完成，正在确认日志服务器归档件',
    }
  }

  const done = Number.isFinite(Number(input.done)) ? Math.max(0, Number(input.done)) : 0
  const total = Number.isFinite(Number(input.total)) ? Math.max(0, Number(input.total)) : 0
  const uploading = classifyUploadState(input.uploadState) === 'uploading'
  const sizeText =
    Number.isFinite(Number(input.archiveSize)) && Number(input.archiveSize) > 0
      ? formatByteCount(Number(input.archiveSize))
      : ''

  // 文案只陈述可核对事实：采集台数、包大小、平台接下来做什么。
  // 不写「预计还需 N 分钟」——直传没有字节级进度，任何估算都是编的。
  const facts: string[] = []
  if (total > 0) facts.push(`已采集 ${done}/${total} 台`)
  if (sizeText) facts.push(`日志包 ${sizeText}`)
  const stateText = uploading ? '正在发送到日志服务器' : '节点仍在采集'
  const factText = facts.length ? `（${facts.join('，')}）` : ''

  return {
    mode: 'observe',
    notice: `${stateText}${factText}；平台已转入后台跟踪，每 15 秒自动刷新，可稍后回来看结果，不需要重新采集`,
  }
}
