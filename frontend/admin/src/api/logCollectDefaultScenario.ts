/**
 * 日志收集「默认场景清单」管理端 API。
 *
 * 与自定义场景不同：默认场景按 acli 模块唯一，不能新建/删除，
 * 「恢复默认」是删除覆写行、回到随镜像发布的清单快照。
 * 下发原文一律由服务端序列化，前端不复刻协议。
 */
import { createApiClient } from '@hci/shared'
import { getAccessToken } from '@/utils/auth'

const client = createApiClient('/api/v1', undefined, getAccessToken)
const BASE = '/log-collect-default-scenarios'

export type DefaultScenarioAction = 'copy' | 'command' | 'script'
export type DefaultScenarioCategory = 'log' | 'config' | 'file'

export interface DefaultScenarioItem {
  action?: DefaultScenarioAction
  path: string
  category: DefaultScenarioCategory
  note?: string | null
  filters?: Array<Record<string, unknown>>
  date_placeholders?: Array<Record<string, unknown>>
  docker?: string | null
  interpreter?: string | null
}

export interface DefaultScenarioManifest {
  module: string
  label: string
  customized: boolean
  revision: number
  item_count: number
  sha256: string
  items: DefaultScenarioItem[]
}

export interface DefaultScenarioList {
  manifests: DefaultScenarioManifest[]
  sources: Array<Record<string, unknown>>
  total_items: number
}

export interface DefaultScenarioValidation {
  ok: boolean
  item_count: number
  sha256: string
  bytes: number
  preview_text: string
  issues: string[]
}

export interface DefaultScenarioSaveResult {
  module: string
  revision: number
  item_count: number
  sha256: string
  bytes: number
}

export async function listDefaultManifests(): Promise<DefaultScenarioList> {
  const { data } = await client.get(BASE)
  return {
    manifests: data?.manifests ?? [],
    sources: data?.sources ?? [],
    total_items: data?.total_items ?? 0,
  }
}

/** 保存前试算：返回服务端真实序列化产物，编辑器展示的就是将下发的内容。 */
export async function validateDefaultManifest(items: DefaultScenarioItem[]): Promise<DefaultScenarioValidation> {
  const { data } = await client.post(`${BASE}/validate`, { items })
  return data
}

export async function saveDefaultManifest(
  module: string,
  items: DefaultScenarioItem[],
): Promise<DefaultScenarioSaveResult> {
  const { data } = await client.put(`${BASE}/${encodeURIComponent(module)}`, { items })
  return data
}

export async function resetDefaultManifest(module: string): Promise<void> {
  await client.delete(`${BASE}/${encodeURIComponent(module)}`)
}
