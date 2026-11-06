import { describe, expect, it } from 'vitest'
import {
  NODE_ARCHIVE_DIR,
  UNVERIFIED_CLEANUP_TIP,
  buildNodeCleanupTip,
  expectedNodeArchivePath,
  trustedNodeArchivePath,
} from '../logCollectCleanup'

const CASE = 'Q2026110600940'
const TRUSTED = `${NODE_ARCHIVE_DIR}/${CASE}.zip`
const OBJECT_KEY = `${CASE}/dea7ccec-e402-43fd-997b-c6f59859c509.zip`

/**
 * 回归（2026-11-06 Q2026110600940）：结果态曾拿 P2 的平台对象 key 拼删除命令。
 * 用户要求两条硬契约：
 * 1. **只给准确路径，任何情况下都不出现 rm / del 等删除命令**；
 * 2. 路径必须逐字命中节点绝对路径，拿不准就明确「不提供路径」。
 */
describe('expectedNodeArchivePath / trustedNodeArchivePath', () => {
  it('按工单号推导绝对路径', () => {
    expect(expectedNodeArchivePath(CASE)).toBe(TRUSTED)
  })

  it('工单号不合法时不推导', () => {
    expect(expectedNodeArchivePath('')).toBeNull()
    expect(expectedNodeArchivePath('Q123')).toBeNull()
    expect(expectedNodeArchivePath(undefined)).toBeNull()
  })

  it('逐字相等才可信', () => {
    expect(trustedNodeArchivePath(CASE, TRUSTED)).toBe(TRUSTED)
  })

  it('拒绝平台对象 key（本次事故的元凶）', () => {
    expect(trustedNodeArchivePath(CASE, OBJECT_KEY)).toBeNull()
  })

  it('拒绝路径穿越、任意绝对路径、别的工单的包', () => {
    expect(trustedNodeArchivePath(CASE, `${NODE_ARCHIVE_DIR}/../../etc/passwd`)).toBeNull()
    expect(trustedNodeArchivePath(CASE, '/etc/passwd')).toBeNull()
    expect(trustedNodeArchivePath(CASE, `${NODE_ARCHIVE_DIR}/Q2026110600341.zip`)).toBeNull()
  })

  it('拒绝带空格、后缀、大小写差异以及空值', () => {
    expect(trustedNodeArchivePath(CASE, ` ${TRUSTED}`)).toBeNull()
    expect(trustedNodeArchivePath(CASE, `${TRUSTED}.bak`)).toBeNull()
    expect(trustedNodeArchivePath(CASE, TRUSTED.toUpperCase())).toBeNull()
    expect(trustedNodeArchivePath(CASE, null)).toBeNull()
  })
})

describe('buildNodeCleanupTip', () => {
  it('有可信绝对路径时只展示路径，不输出任何删除命令', () => {
    const tip = buildNodeCleanupTip(CASE, [TRUSTED])
    expect(tip).toContain(TRUSTED)
    expect(tip).toContain('自行处理')
    expect(tip).not.toMatch(/\brm\b|\bdel\b|\bremove\b/i)
    expect(tip).not.toContain(OBJECT_KEY)
  })

  it('只有平台对象 key 时不提供路径', () => {
    const tip = buildNodeCleanupTip(CASE, [OBJECT_KEY])
    expect(tip).toBe(UNVERIFIED_CLEANUP_TIP)
    expect(tip).toContain('不提供路径')
    expect(tip).not.toContain(NODE_ARCHIVE_DIR)
    expect(tip).not.toMatch(/\brm\b|\bdel\b|\bremove\b/i)
  })

  it('没有任何证据时不提供路径', () => {
    const tip = buildNodeCleanupTip(CASE, [null, undefined, ''])
    expect(tip).toContain('不提供路径')
    expect(tip).not.toContain(NODE_ARCHIVE_DIR)
    expect(tip).not.toMatch(/\brm\b|\bdel\b|\bremove\b/i)
  })

  it('按候选顺序回退：实时对账不可信时采用服务端核过的证据', () => {
    const tip = buildNodeCleanupTip(CASE, [OBJECT_KEY, TRUSTED])
    expect(tip).toContain(TRUSTED)
    expect(tip).not.toMatch(/\brm\b|\bdel\b|\bremove\b/i)
  })

  it('工单号非法时，即使候选看起来像路径也不给路径', () => {
    const tip = buildNodeCleanupTip('Q123', [`${NODE_ARCHIVE_DIR}/Q123.zip`])
    expect(tip).toContain('不提供路径')
    expect(tip).not.toContain(NODE_ARCHIVE_DIR)
  })

  it('路径独占一行、不带参数，且所有输入都不产生删除命令', () => {
    const cases = [
      [TRUSTED],
      [OBJECT_KEY],
      [null],
      [`${NODE_ARCHIVE_DIR}/../../etc/passwd`],
      ['/etc/passwd'],
    ]
    for (const candidates of cases) {
      const tip = buildNodeCleanupTip(CASE, candidates)
      expect(tip).not.toMatch(/\brm\b|\bdel\b|\bremove\b/i)
      const lines = tip.split('\n')
      const pathLine = lines.find((line) => line === TRUSTED)
      if (candidates[0] === TRUSTED) {
        expect(pathLine).toBe(TRUSTED)
      } else {
        expect(pathLine).toBeUndefined()
      }
    }
  })
})
