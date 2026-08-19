// 工具产出变量目录解析的纯逻辑单测。
import { describe, expect, it } from 'vitest'

import {
  buildProduceVariableCatalog,
  extractProduceVariablesFromSchema,
  findProduceVariable,
  parseProduceVariableDraftsFromSchema,
} from '../produceVariables'

describe('parseProduceVariableDraftsFromSchema', () => {
  it('保留编辑中的空草稿，避免双向同步清除新增行', () => {
    expect(parseProduceVariableDraftsFromSchema({
      properties: {
        produces: {
          default: [
            { name: 'HOST', path: 'host' },
            { name: '', path: '' },
          ],
        },
      },
    })).toEqual([
      { name: 'HOST', path: 'host' },
      { name: '', path: '' },
    ])
  })

  it('跳过无效条目并兼容旧版顶层 produces', () => {
    expect(parseProduceVariableDraftsFromSchema({
      produces: [{ name: '', path: '' }, null, 'invalid'],
    })).toEqual([{ name: '', path: '' }])
  })
})

describe('extractProduceVariablesFromSchema', () => {
  it('从标准 Schema 的 produces.default 读取变量并保留路径', () => {
    expect(extractProduceVariablesFromSchema({
      properties: {
        produces: {
          default: [
            { name: 'HOST', path: 'host' },
            { name: 'END', path: 'end' },
          ],
        },
      },
    })).toEqual([
      { name: 'HOST', path: 'host' },
      { name: 'END', path: 'end' },
    ])
  })

  it('兼容旧版顶层 produces，忽略空名称并按名称去重', () => {
    expect(extractProduceVariablesFromSchema({
      produces: [
        { name: ' HOST ', path: 'host' },
        { name: '', path: 'ignored' },
        { name: 'HOST', path: 'other-host' },
        null,
      ],
    })).toEqual([{ name: 'HOST', path: 'host' }])
  })

  it('无效或缺失目录返回空数组', () => {
    expect(extractProduceVariablesFromSchema(null)).toEqual([])
    expect(extractProduceVariablesFromSchema({ properties: { produces: { default: {} } } })).toEqual([])
  })
})

describe('buildProduceVariableCatalog', () => {
  it('只暴露启用的 QKV 工具变量，按工具名隔离', () => {
    const catalog = buildProduceVariableCatalog([
      {
        tool_name: 'qkv_task',
        category: 'qkv',
        is_active: true,
        parameters_schema: { properties: { produces: { default: [{ name: 'VM', path: 'vm' }] } } },
      },
      {
        tool_name: 'qkv_alert',
        category: 'qkv',
        is_active: false,
        parameters_schema: { properties: { produces: { default: [{ name: 'ALERT_TYPE', path: 'alert_type' }] } } },
      },
      {
        tool_name: 'qfk_system',
        category: 'qfk',
        is_active: true,
        parameters_schema: { properties: { produces: { default: [{ name: 'SHOULD_NOT_APPEAR', path: 'x' }] } } },
      },
    ])

    expect(catalog).toEqual({ qkv_task: [{ name: 'VM', path: 'vm' }] })
  })
})

describe('findProduceVariable', () => {
  it('按当前 QKV 工具精确返回变量与 JSON 路径的绑定', () => {
    const catalog = {
      qkv_task: [{ name: 'ERRCODE_TRACING', path: 'errcode_tracing' }],
      qkv_alert: [{ name: 'ERRCODE_TRACING', path: 'alert_error_code' }],
    }

    expect(findProduceVariable(catalog, 'qkv_task', 'ERRCODE_TRACING')).toEqual({
      name: 'ERRCODE_TRACING',
      path: 'errcode_tracing',
    })
    expect(findProduceVariable(catalog, 'qkv_alert', 'MISSING')).toBeUndefined()
  })
})

// ── alias / effectiveProduceKey 相关测试 ────────────────────────────────────
import { effectiveProduceKey, parseProduceVariableDraftsFromSchema as parseDrafts } from '../produceVariables'

describe('effectiveProduceKey', () => {
  it('alias 非空时返回 alias', () => {
    expect(effectiveProduceKey({ name: 'END', alias: 'END1' })).toBe('END1')
    expect(effectiveProduceKey({ name: 'END', alias: '  END2  ' })).toBe('END2')
  })

  it('alias 为空或 undefined 时回退到 name', () => {
    expect(effectiveProduceKey({ name: 'END', alias: '' })).toBe('END')
    expect(effectiveProduceKey({ name: 'HOST' })).toBe('HOST')
  })
})

describe('parseProduceVariableDraftsFromSchema 透传 alias', () => {
  it('alias 字段存在时正确透传', () => {
    const result = parseDrafts({
      properties: {
        produces: {
          default: [
            { name: 'END', path: 'end', alias: 'END1' },
            { name: 'END', path: 'end' },
          ],
        },
      },
    })
    expect(result[0]).toEqual({ name: 'END', path: 'end', alias: 'END1' })
    // alias 字段缺失时为 undefined，保持向下兼容
    expect(result[1].alias).toBeUndefined()
  })
})
