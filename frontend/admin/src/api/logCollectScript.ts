/**
 * 日志收集「收集脚本」库管理端 API。
 *
 * 语义与默认场景完全一致：
 * - `customized=false` 的行是随镜像发布的平台默认快照，只读；
 * - 编辑任一行 → 生成新 revision（覆写内置脚本 / 平台新增脚本继续迭代）；
 * - 恢复默认 → 删除该 code 的全部覆写行，退回快照。
 *
 * 路径参数一律走 query：`code` 是节点文件名，基线里含子目录条目，
 * 塞进 path 段会被 URL 编码规则吃掉。
 */
import { createApiClient } from '@hci/shared'
import { getAccessToken } from '@/utils/auth'

const client = createApiClient('/api/v1', undefined, getAccessToken)
const BASE = '/log-collect-scripts'

export interface LogCollectScriptSummary {
  code: string
  name: string
  interpreter: string
  content_bytes: number
  content_sha256: string
  revision: number
  customized: boolean
  source: string
  enabled: boolean
  binary: boolean
  ref_usable: boolean
  in_baseline: boolean
  updated_at?: string | null
  change_note?: string | null
}

export interface LogCollectScriptDetail extends LogCollectScriptSummary {
  content: string | null
}

export interface LogCollectScriptRevision {
  code: string
  revision: number
  name: string
  interpreter: string
  content_sha256: string
  content_bytes: number
  change_note?: string | null
  source: string
  enabled: boolean
  created_by?: string | null
  created_at?: string | null
  content?: string
}

export interface LogCollectScriptList {
  scripts: LogCollectScriptSummary[]
  total: number
  baseline_total: number
  /** demo = 仓库示例；env = 环境注入的真实清单；missing = 两者都没有 */
  baseline_source: 'demo' | 'env' | 'missing' | string
}

export interface LogCollectScriptValidation {
  ok: boolean
  code: string
  interpreter: string
  content_sha256: string
  content_bytes: number
  issues: string[]
}

export interface LogCollectScriptReference {
  kind: 'default' | 'custom'
  target: string
  revision: number
}

export interface LogCollectScriptSavePayload {
  code: string
  content: string
  name?: string | null
  interpreter?: string | null
  change_note?: string | null
  source?: 'manual' | 'import'
}

export async function listScripts(): Promise<LogCollectScriptList> {
  const { data } = await client.get(BASE)
  return {
    scripts: data?.scripts ?? [],
    total: data?.total ?? 0,
    baseline_total: data?.baseline_total ?? 0,
    baseline_source: data?.baseline_source ?? 'demo',
  }
}

export async function getScript(code: string, revision?: number): Promise<LogCollectScriptDetail> {
  const { data } = await client.get(`${BASE}/detail`, { params: { code, revision } })
  return data
}

export async function listScriptRevisions(code: string): Promise<LogCollectScriptRevision[]> {
  const { data } = await client.get(`${BASE}/revisions`, { params: { code } })
  return data?.revisions ?? []
}

export async function getScriptRevision(code: string, revision: number): Promise<LogCollectScriptRevision> {
  const { data } = await client.get(`${BASE}/revision`, { params: { code, revision } })
  return data
}

export async function validateScript(payload: LogCollectScriptSavePayload): Promise<LogCollectScriptValidation> {
  const { data } = await client.post(`${BASE}/validate`, payload)
  return data
}

export async function createScript(payload: LogCollectScriptSavePayload) {
  const { data } = await client.post(`${BASE}/create`, payload)
  return data
}

export async function importScript(payload: LogCollectScriptSavePayload) {
  const { data } = await client.post(`${BASE}/import`, { ...payload, source: 'import' })
  return data
}

export async function saveScript(payload: LogCollectScriptSavePayload) {
  const { data } = await client.post(`${BASE}/save`, payload)
  return data
}

export async function resetScript(code: string) {
  const { data } = await client.post(`${BASE}/reset`, null, { params: { code } })
  return data
}

export async function setScriptEnabled(code: string, enabled: boolean) {
  const { data } = await client.patch(`${BASE}/enabled`, null, { params: { code, enabled } })
  return data
}

export async function listScriptReferences(code: string): Promise<LogCollectScriptReference[]> {
  const { data } = await client.get(`${BASE}/references`, { params: { code } })
  return data?.references ?? []
}

export async function deleteScript(code: string) {
  const { data } = await client.delete(BASE, { params: { code } })
  return data
}
