import { describe, expect, it } from 'vitest'

import {
  EXTRA_PATH_MAX_COUNT,
  extraPathsLimitHint,
  normalizeExtraPaths,
  parseExtraPathInput,
  validateExtraPath,
} from '../logCollectExtraPaths'

describe('validateExtraPath', () => {
  it('接受目录与文件形态的绝对路径', () => {
    expect(validateExtraPath('/sf/data/local/app/').ok).toBe(true)
    expect(validateExtraPath('/sf/ls.txt').ok).toBe(true)
    expect(validateExtraPath('/sf/data/local/app-2026_11.09').ok).toBe(true)
  })

  it('拒绝相对路径与空值', () => {
    expect(validateExtraPath('').ok).toBe(false)
    expect(validateExtraPath('   ').ok).toBe(false)
    expect(validateExtraPath('sf/data').reason).toContain('绝对路径')
  })

  it('拒绝路径穿越', () => {
    expect(validateExtraPath('/sf/../etc/shadow').reason).toContain('..')
  })

  it('拒绝逗号：bridge 会把它拆成两条路径', () => {
    expect(validateExtraPath('/sf/a,/sf/b').reason).toContain('逗号')
  })

  it('拒绝空格与 shell 元字符：插件给 cp 拼命令时没有加引号', () => {
    for (const bad of ['/sf/a b', '/sf/a;id', '/sf/a&&id', '/sf/$(id)', '/sf/a|b', "/sf/a'b"]) {
      expect(validateExtraPath(bad).ok).toBe(false)
    }
  })

  it('拒绝白名单外字符（与 bridge 的 logCollectPathRe 同源）', () => {
    expect(validateExtraPath('/sf/中文目录').ok).toBe(false)
    expect(validateExtraPath('/sf/a(b)').ok).toBe(false)
    expect(validateExtraPath('/sf/a*').ok).toBe(false)
  })

  it('拒绝超长路径', () => {
    expect(validateExtraPath(`/sf/${'a'.repeat(600)}`).ok).toBe(false)
  })
})

describe('parseExtraPathInput', () => {
  it('支持逗号与换行两种分隔', () => {
    const result = parseExtraPathInput('/sf/a,/sf/b\n/sf/c')
    expect(result.accepted).toEqual(['/sf/a', '/sf/b', '/sf/c'])
    expect(result.rejected).toEqual([])
  })

  it('去重时保留已有项，避免重复追加', () => {
    const result = parseExtraPathInput('/sf/a,/sf/a,/sf/b', ['/sf/a'])
    expect(result.accepted).toEqual(['/sf/b'])
  })

  it('非法项单独收集，不阻断整批合法项', () => {
    const result = parseExtraPathInput('/sf/a,relative,/sf/b;id')
    expect(result.accepted).toEqual(['/sf/a'])
    expect(result.rejected).toHaveLength(2)
    expect(result.rejected[0].value).toBe('relative')
  })

  it('数量上限生效且报告截断条数', () => {
    const input = Array.from({ length: EXTRA_PATH_MAX_COUNT + 3 }, (_, i) => `/sf/p${i}`).join(',')
    const result = parseExtraPathInput(input)
    expect(result.accepted).toHaveLength(EXTRA_PATH_MAX_COUNT)
    expect(result.overflow).toBe(3)
  })
})

describe('normalizeExtraPaths', () => {
  it('去空、去重并保持顺序', () => {
    expect(normalizeExtraPaths(['/sf/b', '', ' ', '/sf/a', '/sf/b'])).toEqual(['/sf/b', '/sf/a'])
  })

  it('空输入返回空数组', () => {
    expect(normalizeExtraPaths(null)).toEqual([])
    expect(normalizeExtraPaths(undefined)).toEqual([])
    expect(normalizeExtraPaths([])).toEqual([])
  })
})

describe('extraPathsLimitHint', () => {
  it('达上限时给出可执行的原因，而不是只置灰', () => {
    expect(extraPathsLimitHint(EXTRA_PATH_MAX_COUNT)).toContain('已达上限')
    expect(extraPathsLimitHint(1)).toContain('当前 1 条')
  })
})
