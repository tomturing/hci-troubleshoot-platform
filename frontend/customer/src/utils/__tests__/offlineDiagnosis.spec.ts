import { describe, expect, it } from 'vitest'
import type { CollectionPlan, CollectionPlanItem } from '@hci/shared'
import {
  buildOfflineDiagnosisUrl,
  formatPlanTarget,
  getPlanTargetNodes,
  hasUnresolvedPlanTarget,
  isOfflineDiagnosisPath,
  resolveArtifactTarget,
} from '@/utils/offlineDiagnosis'

function makePlan(targets: Array<Record<string, unknown>>, inactiveTarget?: Record<string, unknown>): CollectionPlan {
  const items: CollectionPlanItem[] = targets.map(
    (target, index) =>
      ({
        item_id: `item-${index}`,
        sequence: index + 1,
        collector_id: `collector-${index}`,
        collector_revision: 1,
        collector_version: '1.0',
        collector_checksum: `checksum-${index}`,
        display_name: `采集器 ${index}`,
        required_level: 'mandatory',
        activation_state: 'active',
        target,
        time_window: {},
        condition_snapshot: null,
        reason: '测试',
        expected_size_mb: 1,
        timeout_seconds: 30,
        required_permissions: [],
        sensitive_data_types: [],
      }) satisfies CollectionPlanItem,
  )
  if (inactiveTarget) {
    items.push({
      ...items[0],
      item_id: 'inactive-item',
      sequence: items.length + 1,
      activation_state: 'deferred',
      target: inactiveTarget,
    })
  }
  return {
    collection_plan_id: 'plan-1',
    session_id: 'session-1',
    plan_sequence: 1,
    plan_revision: 1,
    profile_name: '测试采集画像',
    profile_revision: 1,
    profile_version: '1.0',
    profile_checksum: 'checksum',
    kbd_ruleset_snapshot: [],
    kbd_ruleset_checksum: 'kbd-checksum',
    product_version: '7.0',
    required_permissions: [],
    sensitive_data_types: [],
    unresolved_variables: [],
    estimated_size_mb: 1,
    estimated_duration_seconds: 30,
    status: 'ready',
    trace_id: 'trace-1',
    created_at: '2026-07-31T00:00:00Z',
    updated_at: '2026-07-31T00:00:00Z',
    items,
  }
}

describe('离线诊断采集器目标节点', () => {
  it('独立页面地址携带工单号且只匹配离线诊断路径', () => {
    expect(buildOfflineDiagnosisUrl(' Q202607310001 ')).toBe(
      '/offline-diagnosis?case_id=Q202607310001',
    )
    expect(isOfflineDiagnosisPath('/offline-diagnosis')).toBe(true)
    expect(isOfflineDiagnosisPath('/offline-diagnosis/')).toBe(true)
    expect(isOfflineDiagnosisPath('/')).toBe(false)
    expect(isOfflineDiagnosisPath('/conversation')).toBe(false)
  })

  it('只提取已激活采集项中的明确节点并去重排序', () => {
    const plan = makePlan(
      [
        { type: 'node', id: 'node-b' },
        { type: 'node', id: 'node-a' },
        { type: 'affected_object', id: 'vm-1', source_node: 'node-b' },
      ],
      { type: 'node', id: 'node-c' },
    )

    expect(getPlanTargetNodes(plan)).toEqual(['node-a', 'node-b'])
  })

  it('识别尚未解析的 source_node，并允许用户补填执行节点', () => {
    const plan = makePlan([{ type: 'variable', id: 'source_node' }])

    expect(hasUnresolvedPlanTarget(plan)).toBe(true)
    expect(resolveArtifactTarget(plan, 'SVR_aCloud_670')).toBe('SVR_aCloud_670')
    expect(formatPlanTarget(plan.items[0].target)).toBe('生成制品时指定执行节点')
  })

  it('计划只有一个明确节点时自动选中', () => {
    const plan = makePlan([{ type: 'node', id: 'node-only' }])

    expect(resolveArtifactTarget(plan, '')).toBe('node-only')
  })
})
