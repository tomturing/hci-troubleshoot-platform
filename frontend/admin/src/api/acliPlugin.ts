/**
 * acli 插件管理端 API（仅规格维护）。
 *
 * 契约边界：页面只提交「插件规格 spec」，服务端做规格校验并落成不可变
 * revision；**插件到达节点不由本页驱动**——旧「保存并下发 + 浏览器连桥执行 +
 * 任务轮询」链路已随链路收口删除（D1），节点收敛由客户侧 acli_sync 第二阶段
 * 持票据拉取平台 manifest 完成（S4c）。页面不接触 SSH 口令/私钥，也不连桥。
 *
 * 鉴权走管理端 JWT，由网关 `require_admin` 校验后重签内部令牌。
 */
import { createApiClient } from '@hci/shared'
import { getAccessToken } from '@/utils/auth'

const client = createApiClient('/api/v1', undefined, getAccessToken)
const BASE = '/acli-plugins'

export interface AcliPluginParameter {
  flag: string
  description: string
  required: boolean
  default: string
}

export interface AcliPluginExtraFile {
  path: string
  mode: '0644' | '0755'
  content: string
}

/** 新增插件所需补齐的全部信息（与服务端 TEMPLATE_SPEC 同源）。 */
export interface AcliPluginSpec {
  update_mode: 'third_party' | 'builtin_override'
  plugin_name: string
  plugin_uuid: string
  display_name: string
  title_zh: string
  title_en: string
  description_zh: string
  description_en: string
  vendor_name: string
  change_log: string
  version: string
  acli_version: string
  hci_version: string
  source: number
  status: number
  arch: number
  container: string
  interpreter: 'bash' | 'sh'
  script_filename: string
  script_content: string
  parameters: AcliPluginParameter[]
  extra_files: AcliPluginExtraFile[]
  enable_completion: boolean
  passthrough_help: boolean
  enable_pid_lock: boolean
  timeout_seconds: number
  smoke_args: string
  source_url: string
}

export interface AcliPluginTemplateResponse {
  template: AcliPluginSpec
  manifest_required_fields: string[]
  builtin_plugins: string[]
  forbidden_fields: string[]
}

export interface AcliPluginItem {
  plugin_name: string
  display_name: string
  update_mode: string
  // 平台不接收节点侧回传，因此没有「当前已生效 hash」这一列；
  // 只有期望态（desired_spec_hash + revision）。
  desired_spec_hash: string
  enabled: boolean
  revision: number | null
  spec_json: AcliPluginSpec | null
  validation: { ok: boolean; errors: string[]; warnings: string[] } | null
}

export interface SaveSpecResult {
  plugin_name: string
  revision: number | null
  spec_hash: string
  status: string
}

export async function getTemplate(): Promise<AcliPluginTemplateResponse> {
  const { data } = await client.get(`${BASE}/templates`)
  return data as AcliPluginTemplateResponse
}

export async function listPlugins(): Promise<AcliPluginItem[]> {
  const { data } = await client.get(`${BASE}/plugins`)
  return (data?.plugins ?? []) as AcliPluginItem[]
}

/** 仅保存规格：校验 → 不可变 revision；同一规格重复保存按 spec_hash 幂等。 */
export async function saveSpec(spec: AcliPluginSpec): Promise<SaveSpecResult> {
  const { data } = await client.post(`${BASE}/specs`, { spec })
  return data as SaveSpecResult
}

/** 把新增插件信息模板下载为 JSON，供后续再次新增插件时填写。 */
export function downloadTemplate(template: AcliPluginSpec): void {
  const blob = new Blob([JSON.stringify(template, null, 2)], { type: 'application/json;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = 'acli-plugin-template.json'
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(url)
}
