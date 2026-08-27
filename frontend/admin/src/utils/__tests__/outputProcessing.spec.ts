import { describe, expect, it } from 'vitest'
import {
  formatAssertSummary,
  formatDeriveExtractSummary,
  formatOutputProcessingFullText,
} from '../outputProcessing'

describe('outputProcessing utils', () => {
  describe('formatAssertSummary', () => {
    it('格式化 threshold 阈值判断', () => {
      expect(formatAssertSummary({
        mode: 'assert',
        input: '{{PROCESS}}',
        match: { type: 'threshold', operator: '>', value: 90 },
      })).toBe('> 90')

      expect(formatAssertSummary({
        mode: 'assert',
        input: '{{PROCESS}}',
        match: { type: 'threshold', operator: '<=', value: 10, aggregation: 'max' },
      })).toBe('<= 10 (最大值)')
    })

    it('格式化 keyword 关键字判断', () => {
      expect(formatAssertSummary({
        mode: 'assert',
        input: '{{OUTPUT}}',
        match: { type: 'keyword', pattern: ['error', 'fail'], mode: 'or' },
      })).toBe('包含 [error, fail] (或)')

      expect(formatAssertSummary({
        mode: 'assert',
        input: '{{OUTPUT}}',
        match: { type: 'keyword', pattern: 'fatal', mode: 'and' },
      })).toBe('包含 [fatal]')
    })

    it('格式化 regex 正则判断', () => {
      expect(formatAssertSummary({
        mode: 'assert',
        input: '{{OUTPUT}}',
        match: { type: 'regex', pattern: '^ERROR.*code=500' },
      })).toBe('正则 /^ERROR.*code=500/')
    })

    it('格式化 state 状态判断', () => {
      expect(formatAssertSummary({
        mode: 'assert',
        input: '{{STATUS}}',
        match: { type: 'state', pattern: 'running' },
      })).toBe('状态 == "running"')
    })

    it('格式化 delta 差值判断', () => {
      expect(formatAssertSummary({
        mode: 'assert',
        input: '{{COUNT}}',
        match: { type: 'delta', operator: '>', value: 5, minimum_samples: 3 },
      })).toBe('差值 > 5 (样本≥3)')
    })

    it('格式化 trend 趋势判断', () => {
      expect(formatAssertSummary({
        mode: 'assert',
        input: '{{LOAD}}',
        match: { type: 'trend', direction: 'increasing', value: 0, minimum_samples: 4 },
      })).toBe('趋势 上升 > 0 (样本≥4)')
    })

    it('格式化 exists 存在性判断', () => {
      expect(formatAssertSummary({
        mode: 'assert',
        input: '{{RECORD}}',
        match: { type: 'exists', expected: true },
      })).toBe('应存在')

      expect(formatAssertSummary({
        mode: 'assert',
        input: '{{RECORD}}',
        match: { type: 'exists', expected: false },
      })).toBe('应不存在')
    })

    it('处理异常或缺失数据', () => {
      expect(formatAssertSummary(undefined)).toBe('—')
      expect(formatAssertSummary({})).toBe('—')
    })
  })

  describe('formatDeriveExtractSummary', () => {
    it('格式化 feature 特征提取', () => {
      expect(formatDeriveExtractSummary({
        mode: 'derive',
        input: '{{DESCRIPTION}}',
        extract: { type: 'feature', feature: 'vm_name' },
      })).toBe('特征「虚拟机名称」')
    })

    it('格式化 split 分隔符提取', () => {
      expect(formatDeriveExtractSummary({
        mode: 'derive',
        input: '{{ERRCODE}}',
        extract: { type: 'split', separator: '/' },
      })).toBe('分隔符「/」')
    })

    it('格式化 AI 提取', () => {
      expect(formatDeriveExtractSummary({
        mode: 'derive',
        input: '{{DESCRIPTION}}',
        extract: { type: 'feature', ai_processing: { mode: 'extract', instruction: '提取虚拟机名称' } },
      })).toBe('原文取值「提取虚拟机名称」')
    })

    it('处理缺失 extract', () => {
      expect(formatDeriveExtractSummary(undefined)).toBe('—')
      expect(formatDeriveExtractSummary({})).toBe('直接取值')
    })
  })

  describe('formatOutputProcessingFullText', () => {
    it('生成完整断言文本', () => {
      expect(formatOutputProcessingFullText({
        mode: 'assert',
        input: '{{PROCESS}}',
        match: { type: 'threshold', operator: '>', value: 90 },
      })).toBe('判断：{{PROCESS}} > 90')

      expect(formatOutputProcessingFullText({
        mode: 'assert',
        input: '{{PROCESS}}',
        match: { type: 'threshold', operator: '>', value: 90, expected: false },
      })).toBe('判断：{{PROCESS}} > 90 [期望为假]')
    })

    it('生成完整派生文本', () => {
      expect(formatOutputProcessingFullText({
        mode: 'derive',
        input: '{{DESCRIPTION}}',
        name: 'VM_NAME',
        type: 'string',
        extract: { type: 'feature', feature: 'vm_name' },
      })).toBe('提取：{{DESCRIPTION}} → 特征「虚拟机名称」 → VM_NAME (string)')
    })
  })
})
