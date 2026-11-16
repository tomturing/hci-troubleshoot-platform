/**
 * acli 插件管理端 API。
 *
 * 契约边界：页面只提交「插件规格 spec + 已登录 SSH 会话」。
 * 目标节点、SSH 凭据、bridge 地址与部署命令全部由平台服务端托管，
 * 这里不定义、不发送、不接收这些字段（服务端白名单会直接拒绝）。
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

/** custom-ui + terminal_bridge 当前已登录的 SSH 会话（可下发目标）。 */
export interface AcliPluginSession {
  case_id: string
  node_ip: string
  host: string
  key: string
}

export interface AcliPluginItem {
  plugin_name: string
  display_name: string
  update_mode: string
  current_spec_hash: string
  desired_spec_hash: string
  last_deploy_status: string
  last_deploy_at: string | null
  enabled: boolean
  revision: number | null
  spec_json: AcliPluginSpec | null
  validation: { ok: boolean; errors: string[]; warnings: string[] } | null
}

export interface AcliPluginDeployment {
  deploy_id: string
  plugin_name: string
  revision: number
  spec_hash: string
  status: 'pending' | 'claimed' | 'running' | 'succeeded' | 'failed'
  attempt: number
  exec_id: string
  trace_id: string
  verification: Record<string, any>
  bridge_result: Record<string, any>
  error_code: string
  error_message: string
  created_at: string | null
  finished_at: string | null
}

export interface PublishResult {
  deploy_id: string
  spec_hash: string
  status: string
  idempotent: boolean
  env_code: string
}

export async function getTemplate(): Promise<AcliPluginTemplateResponse> {
  const { data } = await client.get(`${BASE}/templates`)
  return data as AcliPluginTemplateResponse
}

export async function listSessions(): Promise<AcliPluginSession[]> {
  const { data } = await client.get(`${BASE}/sessions`)
  return (data?.sessions ?? []) as AcliPluginSession[]
}

export async function listPlugins(): Promise<AcliPluginItem[]> {
  const { data } = await client.get(`${BASE}/plugins`)
  return (data?.plugins ?? []) as AcliPluginItem[]
}

/** 保存配置并自动下发；返回 deploy_id 用于页面轮询状态。 */
export async function publishPlugin(spec: AcliPluginSpec, session: { caseId: string; nodeIp: string }): Promise<PublishResult> {
  const { data } = await client.post(`${BASE}/plugins`, {
    spec,
    case_id: session.caseId,
    node_ip: session.nodeIp,
  })
  return data as PublishResult
}

export async function listDeployments(pluginName: string, limit = 50): Promise<AcliPluginDeployment[]> {
  const { data } = await client.get(
    `${BASE}/plugins/${encodeURIComponent(pluginName)}/deployments?limit=${limit}`,
  )
  return (data?.deployments ?? []) as AcliPluginDeployment[]
}

export async function getDeployment(deployId: string): Promise<AcliPluginDeployment> {
  const { data } = await client.get(`${BASE}/deployments/${encodeURIComponent(deployId)}`)
  return data?.deployment as AcliPluginDeployment
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
