// 根因信号前端纯逻辑单测（对应 src/utils/rootSignal.ts）
import { existsSync, readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { describe, expect, it } from 'vitest'
import {
  buildBackfillPayload,
  conflictDisplay,
  describeBackfillResult,
  distinctivenessLevel,
  extractBlockReason,
  formatAcquire,
  matchKeywords,
  MIN_ROOT_CAUSE_LEN,
} from '../rootSignal'
import type { RootSignal } from '../rootSignal'

function signal(overrides: Record<string, any> = {}): RootSignal {
  return {
    schema_version: 1,
    role: 'root_cause',
    match_type: 'executable',
    signal_text: '/run/lock 分区被占满导致任务失败',
    acquire: { tool: 'qfk_system', args: { command: 'df', command_args: ['-h', '/run/lock'], host: '{{HOST}}' } },
    match: { type: 'keyword', mode: 'all', expected: ['100%'] },
    distinctiveness: { category_conflicts: 0, global_conflicts: 0 },
    ...overrides,
  }
}

describe('distinctivenessLevel 冲突判级', () => {
  it('0/0 判为全库唯一（根因信号的核心价值）', () => {
    expect(distinctivenessLevel(signal())).toBe('unique')
  })

  it('仅同类冲突判为 category_only，跨类冲突优先判 ambiguous', () => {
    expect(distinctivenessLevel(signal({ distinctiveness: { category_conflicts: 2, global_conflicts: 0 } })))
      .toBe('category_only')
    expect(distinctivenessLevel(signal({ distinctiveness: { category_conflicts: 2, global_conflicts: 1 } })))
      .toBe('ambiguous')
  })

  it('未抽取（无信号或无 distinctiveness）判为 unextracted', () => {
    expect(distinctivenessLevel(null)).toBe('unextracted')
    expect(distinctivenessLevel({ match_type: 'text' })).toBe('unextracted')
  })

  it('判级颜色与释义随级别切换，ambiguous 必须最醒目', () => {
    expect(conflictDisplay('unique').type).toBe('success')
    expect(conflictDisplay('category_only').type).toBe('warning')
    expect(conflictDisplay('ambiguous').type).toBe('danger')
    expect(conflictDisplay('unextracted').label).toBe('尚未抽取')
  })
})

describe('锚点渲染辅助', () => {
  it('executable 摘要包含工具、命令与参数', () => {
    const text = formatAcquire(signal())
    expect(text).toContain('qfk_system')
    expect(text).toContain('df -h /run/lock')
    expect(text).toContain('{{HOST}}')
  })

  it('text 型无 acquire 时摘要为空串（不得渲染成 undefined）', () => {
    expect(formatAcquire(signal({ match_type: 'text', acquire: null }))).toBe('')
  })

  it('关键词只保留非空字符串项', () => {
    expect(matchKeywords(signal({ match: { expected: ['/etc/shadow', '', ' 旧密码哈希 '] } })))
      .toEqual(['/etc/shadow', '旧密码哈希'])
    expect(matchKeywords(signal({ match: { expected: 'oops-not-array' } }))).toEqual([])
  })
})

/**
 * 从当前工作目录向上定位仓库根（含 backend/ 的那一层）。
 * 不用 import.meta.url：jsdom 环境下它不是 file: 协议，fileURLToPath 会直接报错。
 */
function repoRoot(): string {
  let dir = process.cwd()
  for (let i = 0; i < 8; i += 1) {
    if (existsSync(join(dir, 'backend'))) return dir
    const parent = dirname(dir)
    if (parent === dir) break
    dir = parent
  }
  throw new Error('未定位到仓库根，无法校验跨语言契约')
}

describe('抽取前置体检', () => {
  it('根因为空 / 过短给出可执行的原因', () => {
    expect(extractBlockReason('')).toBe('该 KBD 未填写根因，无法抽取根因信号')
    expect(extractBlockReason('配置不正确')).toContain('请先补全')
  })

  it('达标根因返回空串（放行）', () => {
    expect(extractBlockReason('df 显示 /run/lock 分区使用率 100%，占满导致虚拟机任务失败')).toBe('')
  })

  it('前端阈值必须与后端 MIN_ROOT_CAUSE_LEN 一致（跨语言契约不漂移）', () => {
    const source = readFileSync(
      join(repoRoot(), 'backend/kb-service/app/routes/extract_root_signal.py'),
      'utf-8',
    )
    const matched = source.match(/MIN_ROOT_CAUSE_LEN\s*=\s*(\d+)/)
    expect(matched).not.toBeNull()
    expect(Number(matched?.[1])).toBe(MIN_ROOT_CAUSE_LEN)
  })
})

describe('全库回刷请求体', () => {
  it('合法表单产出 statuses/only_missing/limit，前缀为空不下发', () => {
    expect(buildBackfillPayload({ statuses: ['draft', 'published'], onlyMissing: true, categoryPrefix: '  ', limit: 100 }))
      .toEqual({ statuses: ['draft', 'published'], only_missing: true, limit: 100 })
  })

  it('分类前缀非空时下发 category_prefix，limit 夹紧到 1..100', () => {
    const payload = buildBackfillPayload({ statuses: ['draft'], onlyMissing: false, categoryPrefix: '虚拟机', limit: 9999 })
    expect(payload).toEqual({ statuses: ['draft'], only_missing: false, limit: 100, category_prefix: '虚拟机' })
    expect(buildBackfillPayload({ statuses: ['draft'], onlyMissing: false, categoryPrefix: '', limit: 0 }))
      .toMatchObject({ limit: 1 })
  })

  it('空状态或越界状态返回中文错误串而非请求体（挡在提交前）', () => {
    expect(buildBackfillPayload({ statuses: [], onlyMissing: true, categoryPrefix: '', limit: 100 })).toContain('至少选择一个')
    expect(buildBackfillPayload({ statuses: ['archived'], onlyMissing: true, categoryPrefix: '', limit: 100 })).toContain('草稿')
  })

  it('回刷结果如实告知余量：remaining>0 不得表述为已完成', () => {
    expect(describeBackfillResult({ submitted: 100, remaining: 37 })).toContain('待刷余量 37')
    expect(describeBackfillResult({ submitted: 100, remaining: 0 })).toContain('全库回刷完成')
    expect(describeBackfillResult({ submitted: 0, remaining: 0 })).toContain('没有符合条件')
  })
})
