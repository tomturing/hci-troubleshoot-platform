import { describe, expect, it } from 'vitest'
import { logCollectErrorHint } from '../logCollectErrorHints'

describe('logCollectErrorHint', () => {
  // 桥侧新增的每个 SPC 码都必须有可执行的检查与处理步骤，
  // 否则诊断卡会退化成"只有标题没有指引"。
  it.each([
    'LC-SPC-001',
    'LC-SPC-002',
    'LC-SPC-003',
    'LC-SPC-004',
    'LC-SPC-005',
    'LC-SPC-006',
    'LC-SPC-007',
    'LC-SPC-008',
    'LC-SPC-009',
    'LC-SPC-010',
    'LC-PLUGIN-015',
    'LC-PLUGIN-016',
    'LC-TRANSFER-011',
  ])('%s 有标题、检查步骤与处理步骤', (code) => {
    const hint = logCollectErrorHint(code)
    expect(hint?.title).toBeTruthy()
    expect(hint?.check.length).toBeGreaterThan(0)
    expect(hint?.fix.length).toBeGreaterThan(0)
  })

  it('未知码返回 null，由界面指向任务详情而不是编造原因', () => {
    expect(logCollectErrorHint('LC-UNKNOWN-999')).toBeNull()
    expect(logCollectErrorHint('')).toBeNull()
    expect(logCollectErrorHint(undefined)).toBeNull()
  })

  it('大小写与空白可归一', () => {
    expect(logCollectErrorHint('  lc-spc-007 ')?.title).toBe('目标节点 SSH 认证失败')
  })

  // 旧文案把 bridge 墙钟写成“本次采集开始”，节点慢 6 分钟就会误导用户去查网络。
  // 新文案只陈述节点本地时间与时效，处置第一步是校准节点时间。
  it('LC-TRANSFER-011 的提示只陈述节点本地时间与交付时效', () => {
    const hint = logCollectErrorHint('LC-TRANSFER-011')
    expect(hint?.title).toBe('节点产物超出交付时效')
    const text = [...(hint?.check ?? []), ...(hint?.fix ?? [])].join(' ')
    expect(text).toContain('校准节点时间')
    expect(text).not.toContain('早于本次采集开始')
  })
})