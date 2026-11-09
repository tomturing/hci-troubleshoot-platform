/**
 * 日志收集「自定义场景」（acli `-c`）的管理端 API。
 *
 * 三条约束（与后端 schema 同源，前端只做展示与轻校验）：
 *  1. `code` 创建后不可改：历史任务按 `code + revision` 回溯；
 *  2. 每次更新都会生成**新 revision**，不会原地覆盖；
 *  3. 下发内容固定为 `{"paths":"a,b,c"}`，前端只预览，不自行拼装协议——
 *     真正的序列化在服务端 `serialize_config`，两处各写一份必然漂移。
 */
import { createApiClient } from '@hci/shared'
import { getAccessToken } from '@/utils/auth'

export type CustomScenarioCategory = 'log' | 'config' | 'file'
export type CustomScenarioSource = 'visual' | 'json'
export type CustomScenarioReviewState = 'pending_review' | 'approved'

export interface CustomScenarioItem {
  path: string
  category: CustomScenarioCategory
  note?: string | null
}

export interface CustomScenario {
  scenario_id: string
  revision: number
  code: string
  label: string
  summary: string | null
  applies_to: string | null
  items: CustomScenarioItem[]
  item_count: number
  config_sha256: string | null
  config_bytes: number | null
  source: CustomScenarioSource
  enabled: boolean
  sort_order: number
  review_state: CustomScenarioReviewState
  created_by: string | null
  updated_by: string | null
  created_at: string | null
  updated_at: string | null
}

export interface CustomScenarioCreatePayload {
  code: string
  label: string
  summary?: string | null
  applies_to?: string | null
  items: CustomScenarioItem[]
  source?: CustomScenarioSource
  enabled?: boolean
  sort_order?: number
  review_state?: CustomScenarioReviewState
}

export type CustomScenarioUpdatePayload = Partial<Omit<CustomScenarioCreatePayload, 'code'>>

export interface CustomScenarioValidation {
  ok: boolean
  issues: Array<{ index: number | null; field: string; message: string }>
  item_count: number
  config_bytes: number
}

const client = createApiClient('/api/v1', undefined, getAccessToken)
const BASE = '/log-collect-custom-scenarios'

export async function listCustomScenarios(): Promise<CustomScenario[]> {
  const { data } = await client.get(BASE)
  return data?.scenarios ?? []
}

export async function createCustomScenario(payload: CustomScenarioCreatePayload): Promise<CustomScenario> {
  const { data } = await client.post(BASE, payload)
  return data
}

export async function updateCustomScenario(
  code: string,
  payload: CustomScenarioUpdatePayload,
): Promise<CustomScenario> {
  const { data } = await client.put(`${BASE}/${encodeURIComponent(code)}`, payload)
  return data
}

export async function deleteCustomScenario(code: string): Promise<void> {
  await client.delete(`${BASE}/${encodeURIComponent(code)}`)
}

/** 保存前试算：返回条数与下发字节数，不落库。 */
export async function validateCustomScenario(
  payload: CustomScenarioCreatePayload,
): Promise<CustomScenarioValidation> {
  const { data } = await client.post(`${BASE}/validate`, payload)
  return data
}

/**
 * 本地预览下发内容。
 *
 * 仅用于编辑器展示"客户最终会拿到的配置长什么样"；
 * 真正下发的是服务端序列化结果（`ensure_ascii=True`），两者口径一致但真源在服务端。
 */
export function previewConfigText(items: CustomScenarioItem[]): string {
  const paths = items.map((item) => item.path.trim()).filter(Boolean)
  return JSON.stringify({ paths: paths.join(',') })
}
