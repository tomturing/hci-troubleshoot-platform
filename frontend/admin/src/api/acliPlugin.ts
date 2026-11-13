/** acli 插件管理 API — 挂在网关 /api/v1/tools/{path} 泛向代理下。 */

const BASE = '/api/v1/tools/acli-plugins'

export interface AcliPlugin {
  id?: number
  plugin_name: string
  plugin_uuid?: string
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
  interpreter: string
  script_filename: string
  script_content: string
  parameters: any[]
  extra_files: any[]
  enable_completion: boolean
  passthrough_help: boolean
  enable_pid_lock: boolean
  timeout_seconds: number
  smoke_args: string
  device_host: string
  device_port: number
  device_username: string
  node_ip: string
  source_url: string
  spec_hash?: string
  last_deploy_id?: string
  last_deploy_status?: string
  last_deploy_at?: string | null
  last_deploy_detail?: any
  is_active?: boolean
  created_at?: string
  updated_at?: string
}

export interface DescriptorFile {
  path: string
  mode: string
  content: string
  sha256: string
}

export interface DeployDescriptor {
  descriptor_version: string
  deploy_id: string
  plugin_name: string
  version: string
  spec_hash: string
  device_host: string
  node_ip: string
  files: DescriptorFile[]
  manifest: any
  wrapper: string
  register_item: any
  completion_line: string
  verify_commands: string[]
  deploy_script: string
  deploy_command: string
  deploy_script_path: string
}

export interface DeploymentReceipt {
  deploy_id: string
  spec_hash: string
  version: string
  device_host: string
  node_ip: string
  exec_id: string
  trace_id: string
  status: string
  exit_code: number | null
  stdout: string
  stderr: string
  verification: any
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) },
  })
  const text = await resp.text()
  let data: any = {}
  try {
    data = text ? JSON.parse(text) : {}
  } catch {
    data = { detail: text }
  }
  if (!resp.ok) {
    throw new Error(data?.detail || `请求失败: ${resp.status} ${resp.statusText}`)
  }
  return data as T
}

export async function listAcliPlugins(): Promise<AcliPlugin[]> {
  const data = await request<{ items: AcliPlugin[] }>(BASE)
  return data.items || []
}

export async function getAcliPluginTemplates(): Promise<any> {
  return request<any>(`${BASE}/templates`)
}

export async function createAcliPlugin(payload: AcliPlugin): Promise<any> {
  return request<any>(BASE, { method: 'POST', body: JSON.stringify(payload) })
}

export async function updateAcliPlugin(id: number, payload: AcliPlugin): Promise<any> {
  return request<any>(`${BASE}/${id}`, { method: 'PUT', body: JSON.stringify(payload) })
}

export async function deleteAcliPlugin(id: number): Promise<any> {
  return request<any>(`${BASE}/${id}`, { method: 'DELETE' })
}

export async function buildAcliPluginDescriptor(id: number): Promise<DeployDescriptor> {
  const data = await request<{ descriptor: DeployDescriptor }>(`${BASE}/${id}/descriptor`, {
    method: 'POST',
    body: JSON.stringify({}),
  })
  return data.descriptor
}

export async function listAcliPluginDeployments(id: number): Promise<any[]> {
  const data = await request<{ items: any[] }>(`${BASE}/${id}/deployments`)
  return data.items || []
}

export async function recordAcliPluginDeployment(id: number, payload: DeploymentReceipt): Promise<any> {
  return request<any>(`${BASE}/${id}/deployments`, { method: 'POST', body: JSON.stringify(payload) })
}
