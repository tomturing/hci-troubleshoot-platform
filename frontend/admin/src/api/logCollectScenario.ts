/**
 * 日志收集「故障场景」字典的管理端 API。
 *
 * 场景枚举由集群 acli 固定，平台只维护文案，不定义能力边界；
 * `value` 是 acli -m 的枚举值，也是历史任务里存的值，创建后不可改。
 * 鉴权走管理端 JWT，由网关 `require_admin` 校验。
 */
import { createApiClient } from '@hci/shared'
import { getAccessToken } from '@/utils/auth'

/** 字典项（管理端列表中停用项也会返回）。 */
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
}

/** 新建载荷：只允许补录集群已支持但字典缺项的枚举，由服务端白名单校验。 */
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
}

/** 更新载荷：全字段可选，只提交改过的列，避免覆盖别人刚改的文案。 */
export type ScenarioUpdatePayload = Partial<Omit<ScenarioCreatePayload, 'value'>>

const client = createApiClient('/api/v1', undefined, getAccessToken)
const BASE = '/log-collect-scenarios'

/** 列表（含停用项，否则运营无法把停用的场景重新启用）。 */
export async function listScenarios(): Promise<LogCollectScenario[]> {
  const { data } = await client.get(BASE)
  return (data?.scenarios ?? []) as LogCollectScenario[]
}

/** 新建：服务端按 acli 白名单校验，非法枚举返回 400，重复值返回 409。 */
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

/** 启用/停用：只提交 enabled 一个字段。 */
export async function toggleScenario(value: string, enabled: boolean): Promise<LogCollectScenario> {
  return updateScenario(value, { enabled })
}
