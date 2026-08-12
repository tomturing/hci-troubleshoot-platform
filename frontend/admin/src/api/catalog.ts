export interface CatalogMeta {
  name: string
  title: string
  description: string
  exists: boolean
  size_bytes: number
  mtime: string
  item_count: number
}

export interface CatalogDetail {
  meta: CatalogMeta
  content_text: string
  content_json: any
}

export interface ValidateResult {
  valid: boolean
  message: string
  error_type?: string
  item_count?: number
}

export interface UpdateResult {
  success: boolean
  message: string
  meta: CatalogMeta
  content_text: string
}

/** 获取可管理的 Catalog 列表 */
export async function getCatalogList(): Promise<CatalogMeta[]> {
  const resp = await fetch('/api/kb/catalogs')
  if (!resp.ok) {
    throw new Error(`获取 Catalog 列表失败: ${resp.status} ${resp.statusText}`)
  }
  const data = await resp.json()
  return data.catalogs || []
}

/** 获取单条 Catalog 的详细内容 */
export async function getCatalogDetail(filename: string): Promise<CatalogDetail> {
  const resp = await fetch(`/api/kb/catalogs/${encodeURIComponent(filename)}`)
  if (!resp.ok) {
    throw new Error(`获取 Catalog [${filename}] 详情失败: ${resp.status} ${resp.statusText}`)
  }
  return await resp.json()
}

/** 校验 Catalog JSON 内容语法与规则 */
export async function validateCatalogContent(filename: string, content: string): Promise<ValidateResult> {
  const resp = await fetch(`/api/kb/catalogs/${encodeURIComponent(filename)}/validate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content }),
  })
  if (!resp.ok) {
    throw new Error(`校验失败: ${resp.status} ${resp.statusText}`)
  }
  return await resp.json()
}

/** 保存并在线更新 Catalog 内容（触发后端热加载） */
export async function updateCatalogContent(filename: string, content: string): Promise<UpdateResult> {
  const resp = await fetch(`/api/kb/catalogs/${encodeURIComponent(filename)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content }),
  })
  if (!resp.ok) {
    const errorData = await resp.json().catch(() => ({}))
    throw new Error(errorData.detail || `保存失败: ${resp.status} ${resp.statusText}`)
  }
  return await resp.json()
}
