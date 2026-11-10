/**
 * 日志收集「自定义场景」的管理端 API。
 *
 * 第一性原理：平台有**两条物理上互不相通的通道**，产物形态完全不同，
 * 不是同一份配置的两种写法：
 *  - `-c`（source=visual/json）：节点上一个 JSON 文件，内容是 `{"paths":"a,b,c"}`，
 *    插件只做逐条 `cp -r`，不认 filters、不 glob、不执行命令；
 *  - spc（source=spc）：覆写节点 `spc/<slot>.json`，是**数组**，保留
 *    `action`（copy/command/script）、`filters`、`{date}` 占位与 `note` 分组。
 *
 * 因此：
 *  1. `code` 创建后不可改：历史任务按 `code + revision` 回溯；
 *  2. 每次更新都会生成**新 revision**，不会原地覆盖；
 *  3. 下发文本一律由服务端序列化后经 `validateCustomScenario` 返回，
 *     前端**不复刻协议** —— 两处各写一份必然漂移，管理员看到的预览
 *     就会与节点上实际写入的内容不一致。
 */
import { createApiClient } from '@hci/shared'
import { getAccessToken } from '@/utils/auth'

export type CustomScenarioCategory = 'log' | 'config' | 'file'
export type CustomScenarioSource = 'visual' | 'json' | 'spc'
export type CustomScenarioAction = 'copy' | 'command' | 'script'
export type CustomScenarioReviewState = 'pending_review' | 'approved'

export interface CustomScenarioItem {
  /** 只在 spc 通道生效；-c 通道后端会强制要求 copy。 */
  action?: CustomScenarioAction
  path: string
  category: CustomScenarioCategory
  note?: string | null
  /** spc 的时间/条件过滤，原样透传，插件按 -d 注入时间范围。 */
  filters?: Array<Record<string, unknown>>
  date_placeholders?: Array<Record<string, unknown>>
  docker?: string | null
  interpreter?: string | null
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
  preview_kind: 'config' | 'spc'
  /** 服务端真实序列化产物，直接展示即可。 */
  preview_text: string
}

/** spc 清单快照中的一条条目（随镜像发布的 11 份清单）。 */
export interface SpcCatalogItem {
  module: string
  group: string
  action: CustomScenarioAction
  path: string
  filters: Array<Record<string, unknown>>
  date_placeholders: Array<Record<string, unknown>>
  docker: string | null
  interpreter: string | null
  has_wildcard: boolean
  needs_date_expand: boolean
  /** true = 这条也能放进 -c 通道（copy、无通配符、无 filters）。 */
  usable_by_config: boolean
}

export interface SpcCatalogGroup {
  note: string
  items: SpcCatalogItem[]
}

export interface SpcCatalogModule {
  module: string
  label: string
  /** 在 acli `-m` 白名单内；false 表示只能被 sfd_main -s 使用。 */
  m_whitelisted: boolean
  item_count: number
  config_usable_count: number
  groups: SpcCatalogGroup[]
}

export interface SpcCatalog {
  modules: SpcCatalogModule[]
  sources: Array<{ file: string; sha256: string; items: number }>
  total_items: number
}

const client = createApiClient('/api/v1', undefined, getAccessToken)
const BASE = '/log-collect-custom-scenarios'

/** 是否走 spc 通道。source 复用了历史枚举值，判断口径集中在这里。 */
export function isSpcSource(source: CustomScenarioSource | null | undefined): boolean {
  return source === 'spc'
}

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

/**
 * 保存前试算：返回条数、字节数与**服务端真实序列化文本**，不落库。
 *
 * 通道与条目不匹配时返回 `ok=false` 与 issues（后端不再抛 500），
 * 编辑器应把它当校验提示展示，而不是"接口坏了"。
 */
export async function validateCustomScenario(
  payload: CustomScenarioCreatePayload,
): Promise<CustomScenarioValidation> {
  const { data } = await client.post(`${BASE}/validate`, payload)
  return data
}

/**
 * spc 清单快照（11 份，随镜像发布）。
 *
 * 为什么不读节点实时文件：管理员配置场景不该依赖某台机器在线；
 * 节点清单漂移由「从集群刷新」后用 sha256 对比发现。
 */
export async function getSpcCatalog(): Promise<SpcCatalog> {
  const { data } = await client.get(`${BASE}/catalog`)
  return data
}
