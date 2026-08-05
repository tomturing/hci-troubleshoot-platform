import { describe, expect, it } from 'vitest'

import {
  appendExecOutput,
  buildSafeExecResultPayload,
  createExecOutputBuffer,
  finalizeExecOutput,
  lineMatchesExecFilter,
  type ExecOutputFilter,
} from '../execOutputFilter'

const allFilter: ExecOutputFilter = {
  source: 'stdout',
  include: ['4359974862144', 'qcow2'],
  exclude: ['grep'],
  include_mode: 'all',
  case_sensitive: true,
}

describe('execOutputFilter', () => {
  it('按 all/any、exclude 和大小写规则匹配字面量', () => {
    expect(lineMatchesExecFilter('qemu /4359974862144/disk.qcow2', allFilter)).toBe(true)
    expect(lineMatchesExecFilter('grep 4359974862144 qcow2', allFilter)).toBe(false)
    expect(lineMatchesExecFilter('4359974862144', allFilter)).toBe(false)
    expect(lineMatchesExecFilter('SERVER-IMG', {
      ...allFilter,
      include: ['server-img', 'other'],
      exclude: [],
      include_mode: 'any',
      case_sensitive: false,
    })).toBe(true)
    expect(lineMatchesExecFilter('检测到IP 发生冲突 测试数据', {
      ...allFilter,
      include: ['检测到IP', '冲突'],
      exclude: ['测试数据', '模拟冲突'],
      exclude_mode: 'all',
    })).toBe(true)
    expect(lineMatchesExecFilter('检测到IP 发生冲突 测试数据 模拟冲突', {
      ...allFilter,
      include: ['检测到IP', '冲突'],
      exclude: ['测试数据', '模拟冲突'],
      exclude_mode: 'all',
    })).toBe(false)
  })

  it('关键字跨 WebSocket chunk 时仍保留完整逻辑行', () => {
    const buffer = createExecOutputBuffer()
    appendExecOutput(buffer, [allFilter], 'stdout', 'noise\nqemu /4359974')
    appendExecOutput(buffer, [allFilter], 'stdout', '862144/disk.qcow2\nother\n')
    appendExecOutput(buffer, [allFilter], 'stdout', '', true)

    expect(buffer.stdout.join('')).toBe('qemu /4359974862144/disk.qcow2\n')
    expect(buffer.stdoutRemainder).toBe('')
  })

  it('flush 保留没有换行符的最后一行且不制造额外空行', () => {
    const buffer = createExecOutputBuffer()
    appendExecOutput(buffer, [{ ...allFilter, include: ['PID'], exclude: [] }], 'stdout', 'PID 9527')
    appendExecOutput(buffer, [{ ...allFilter, include: ['PID'], exclude: [] }], 'stdout', '', true)
    expect(buffer.stdout.join('')).toBe('PID 9527')
  })

  it('stdout/stderr 共用上限，超限后 Fail Closed', () => {
    const buffer = createExecOutputBuffer()
    appendExecOutput(buffer, [], 'stdout', '12345', false, 8)
    appendExecOutput(buffer, [], 'stderr', '6789', false, 8)

    expect(buffer.stdout.join('')).toBe('12345')
    expect(buffer.stderr.join('')).toBe('')
    expect(buffer.overflow).toBe(true)
  })

  it('旧 bridge 最终帧携带完整大输出时用筛选缓冲覆盖', () => {
    const buffer = createExecOutputBuffer()
    appendExecOutput(buffer, [allFilter], 'stdout', 'qemu /4359974862144/disk.qcow2\n')
    const result = finalizeExecOutput(buffer, [allFilter], {
      execId: 'exec-old-bridge',
      output: '',
      exitCode: 0,
      stdout: 'unfiltered'.repeat(1024 * 1024),
      stderr: '',
    })

    expect(result.stdout).toBe('qemu /4359974862144/disk.qcow2\n')
    expect(result.output).toBe('qemu /4359974862144/disk.qcow2\n')
    expect(result.output).not.toContain('unfiltered')
    expect(result.exitCode).toBe(0)
  })

  it('旧 bridge 没有发送分块时也不采用最终帧中的未筛选输出', () => {
    const result = finalizeExecOutput(createExecOutputBuffer(), [allFilter], {
      execId: 'exec-old-bridge-final-only',
      output: '',
      exitCode: 0,
      stdout: 'unfiltered'.repeat(1024 * 1024),
      stderr: '',
    })

    expect(result.stdout).toBe('')
    expect(result.output).toBe('')
    expect(result.exitCode).toBe(0)
  })

  it('回传 payload 以筛选后的物理流覆盖旧 bridge 的 40 MB 聚合输出', () => {
    const payload = buildSafeExecResultPayload(
      'exec-lsof-40mb',
      'unfiltered'.repeat(4 * 1024 * 1024),
      0,
      undefined,
      'qemu 9527 /images/4359974862144/disk.qcow2\n',
      '',
    )

    expect(payload.output).toBe('qemu 9527 /images/4359974862144/disk.qcow2\n')
    expect(JSON.stringify(payload).length).toBeLessThan(1024)
  })

  it('无物理流的超大兼容输出在 HTTP 边界 Fail Closed', () => {
    const payload = buildSafeExecResultPayload(
      'exec-unfiltered',
      'x'.repeat(256 * 1024 + 1),
      0,
    )

    expect(payload.exit_code).toBe(-1)
    expect(payload.output).toContain('QFK_EDGE_OUTPUT_LIMIT')
    expect(payload.output.length).toBeLessThan(256)
  })

  it('状态前缀也计入 256 KiB 回传预算', () => {
    const payload = buildSafeExecResultPayload(
      'exec-status-overflow',
      'x'.repeat(256 * 1024),
      0,
      'success',
    )

    expect(payload.exit_code).toBe(-1)
    expect(payload.output).toContain('QFK_EDGE_OUTPUT_LIMIT')
  })
})
