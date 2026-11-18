/**
 * acli 插件管理端 API。
 *
 * 契约边界：页面提交「插件规格 spec + 已登录 SSH 会话标识」，服务端返回
 * **部署命令**与规格摘要；命令的**执行**由页面经 `acliPluginBridge` 直接发到桥
 * （与 customer-ui 的 acli_sync / log_collect 同一条链路），执行结果再回报给服务端落库。
 * 页面始终不接触 SSH 口令/私钥，也不新建 SSH 连接。
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

/** 桥（terminal_bridge）上当前已登录的 SSH 会话（可下发目标），由 `acliPluginBridge` 直读。 */
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

/** 服务端返回的部署计划：命令文本 + 摘要，执行权在浏览器侧。 */
export interface AcliPluginDeployPlan {
  exec_id: string
  trace_id: string
  commands: string[]
  timeout_seconds: number
  case_id: string
  node_ip: string
}

export interface PublishResult {
  deploy_id: string
  spec_hash: string
  status: string
  idempotent: boolean
  plan?: AcliPluginDeployPlan
}

export async function getTemplate(): Promise<AcliPluginTemplateResponse> {
  const { data } = await client.get(`${BASE}/templates`)
  return data as AcliPluginTemplateResponse
}

export async function listPlugins(): Promise<AcliPluginItem[]> {
  const { data } = await client.get(`${BASE}/plugins`)
  return (data?.plugins ?? []) as AcliPluginItem[]
}

/** 保存配置并自动下发；返回 deploy_id 用于页面轮询状态。 */
export async function publishPlugin(
  spec: AcliPluginSpec,
  session: { caseId: string; nodeIp: string },
  force = false,
): Promise<PublishResult> {
  const { data } = await client.post(`${BASE}/plugins`, {
    spec,
    case_id: session.caseId,
    node_ip: session.nodeIp,
    force,
  })
  return data as PublishResult
}

export interface AcliPluginReportPayload {
  exec_id: string
  exit_code: number
  stdout: string
  stderr: string
  timed_out: boolean
  duration_ms: number
  bridge_instance_id: string
}

/** 浏览器执行完部署命令后回报结果；服务端解析设备侧 ACP_RESULT 后落终态。 */
export async function reportDeployResult(
  deployId: string,
  payload: AcliPluginReportPayload,
): Promise<AcliPluginDeployment> {
  const { data } = await client.post(
    `${BASE}/deployments/${encodeURIComponent(deployId)}/result`,
    payload,
  )
  return data?.deployment as AcliPluginDeployment
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
