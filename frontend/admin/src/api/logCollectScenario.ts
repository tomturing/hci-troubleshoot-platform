/**
 * 日志收集「故障场景」字典的管理端 API。
 *
 * 三件事必须在注释里说清，否则后面维护者容易改错方向：
 *  1. `value` 是 acli `-m` 的枚举值，也是历史任务表里存的值 —— **创建后不可改**，
 *     因此更新请求体里刻意不带 `value`（服务端 schema 里也没有这个字段）。
 *  2. `collects` / `typical_symptoms` 无法从 acli 取得，只能人工维护，
 *     所以每条都带 `review_state`：`pending_review` 表示文案还没经支持工程师确认。
 *  3. 鉴权走管理端 JWT，由网关 `require_admin` 校验；请求经 `/api/v1/log-collect-scenarios`
 *     转发到 case-service 的内部端点，前端不直连下游。
 */
import { createApiClient } from '@hci/shared'
import { getAccessToken } from '@/utils/auth'

export type ScenarioReviewState = 'pending_review' | 'approved'

/** 字典项全量字段（管理端列表中停用项也会返回）。 */
export interface LogCollectScenario {
  value: string
  label: string
  summary: string | null
  collects: string | null
  typical_symptoms: string | null
  duration_hint: string | null
  exclusive: boolean
  sort_order: number
  enabled: boolean
  review_state: ScenarioReviewState
  updated_by: string | null
  updated_at: string | null
}

/** 新建载荷：`value` 只在创建时提供。 */
export interface ScenarioCreatePayload {
  value: string
  label: string
  summary?: string | null
  collects?: string | null
  typical_symptoms?: string | null
  duration_hint?: string | null
  exclusive?: boolean
  sort_order?: number
  enabled?: boolean
  review_state?: ScenarioReviewState
}

/**
 * 更新载荷：不含 `value`（服务端按路径参数定位记录），且**全字段可选**。
 *
 * 全部可选是刻意的：表格里改一个开关就只提交那一个字段，
 * 若把字段声明成必填，调用方会被迫把没动过的文案一起发上去造成覆盖。
 */
export type ScenarioUpdatePayload = Partial<Omit<ScenarioCreatePayload, 'value'>>

// 管理端 axios 客户端的拦截器负责注入 JWT；与 DshDiagnosisView 等页面保持一致。
const client = createApiClient('/api/v1', undefined, getAccessToken)
const BASE = '/log-collect-scenarios'

/** 列表（含停用项，否则运营无法把停用的场景重新启用）。 */
export async function listScenarios(): Promise<LogCollectScenario[]> {
  const { data } = await client.get(BASE)
  return (data?.scenarios ?? []) as LogCollectScenario[]
}

/** 新建。`value` 已存在时服务端返回 409。 */
export async function createScenario(payload: ScenarioCreatePayload): Promise<LogCollectScenario> {
  const { data } = await client.post(BASE, payload)
  return data as LogCollectScenario
}

/** 更新（按需字段更新，未提交的字段保持原值）。 */
export async function updateScenario(
  value: string,
  payload: ScenarioUpdatePayload,
): Promise<LogCollectScenario> {
  const { data } = await client.put(`${BASE}/${encodeURIComponent(value)}`, payload)
  return data as LogCollectScenario
}

/**
 * 启用/停用。
 *
 * 单独封一层的原因：列表行内的 switch 只改这一个字段，若走全量更新会把
 * 别的列（可能是别人正在编辑的文案）一起覆盖掉。
 */
export async function toggleScenario(value: string, enabled: boolean): Promise<LogCollectScenario> {
  return updateScenario(value, { enabled })
}
