import type { CollectionPlan } from '@hci/shared'

export const OFFLINE_DIAGNOSIS_PATH = '/offline-diagnosis'

/** 构造独立离线诊断页面地址。 */
export function buildOfflineDiagnosisUrl(caseId: string): string {
  const query = new URLSearchParams({ case_id: caseId.trim() })
  return `${OFFLINE_DIAGNOSIS_PATH}?${query.toString()}`
}

/** 跳转到独立离线诊断页面。 */
export function navigateToOfflineDiagnosis(caseId: string): void {
  window.location.assign(buildOfflineDiagnosisUrl(caseId))
}

/** 根据浏览器路径选择 Customer UI（客户界面）入口。 */
export function isOfflineDiagnosisPath(pathname: string): boolean {
  const normalized = pathname.length > 1 ? pathname.replace(/\/+$/, '') : pathname
  return normalized === OFFLINE_DIAGNOSIS_PATH
}

/** 提取采集计划中所有已激活的明确执行节点。 */
export function getPlanTargetNodes(plan: CollectionPlan | null): string[] {
  if (!plan) return []

  const nodes = new Set<string>()
  for (const item of plan.items) {
    if (item.activation_state !== 'active') continue
    const sourceNode = typeof item.target.source_node === 'string' ? item.target.source_node.trim() : ''
    const nodeId =
      item.target.type === 'node' && typeof item.target.id === 'string' ? item.target.id.trim() : ''
    if (sourceNode) nodes.add(sourceNode)
    if (nodeId) nodes.add(nodeId)
  }
  return [...nodes].sort()
}

/** 判断采集计划是否仍包含待执行时指定的 source_node（来源节点）。 */
export function hasUnresolvedPlanTarget(plan: CollectionPlan | null): boolean {
  return Boolean(
    plan?.items.some(
      (item) =>
        item.activation_state === 'active' &&
        item.target.type === 'variable' &&
        item.target.id === 'source_node',
    ),
  )
}

/** 优先使用用户填写节点；仅有一个计划节点时自动选中。 */
export function resolveArtifactTarget(plan: CollectionPlan | null, preferredTarget: string): string {
  const preferred = preferredTarget.trim()
  if (preferred) return preferred
  const nodes = getPlanTargetNodes(plan)
  return nodes.length === 1 ? nodes[0] : ''
}

/** 将采集项目标转换为客户可读文本。 */
export function formatPlanTarget(target: Record<string, unknown>): string {
  if (target.type === 'diagnosis_session') return '会话级（每个制品包含）'
  if (typeof target.source_node === 'string' && target.source_node.trim()) {
    return target.source_node.trim()
  }
  if (target.type === 'node' && typeof target.id === 'string' && target.id.trim()) {
    return target.id.trim()
  }
  if (target.type === 'variable' && target.id === 'source_node') {
    return '生成制品时指定执行节点'
  }
  if (typeof target.name === 'string' && target.name.trim()) return target.name.trim()
  if (typeof target.id === 'string' && target.id.trim()) return target.id.trim()
  return '当前故障对象'
}
