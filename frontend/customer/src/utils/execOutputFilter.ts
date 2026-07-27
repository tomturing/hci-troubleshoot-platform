export const MAX_RELAYED_OUTPUT_CHARS = 256 * 1024

export interface ExecOutputFilter {
  source: 'stdout' | 'stderr'
  include: string[]
  exclude: string[]
  include_mode: 'all' | 'any'
  case_sensitive: boolean
}

export interface ExecOutputBuffer {
  stdout: string[]
  stderr: string[]
  stdoutRemainder: string
  stderrRemainder: string
  keptChars: number
  overflow: boolean
}

export interface AgentExecResult {
  execId: string
  output: string
  exitCode: number
  stdout?: string
  stderr?: string
}

export interface ExecResultRelayPayload {
  exec_id: string
  output: string
  exit_code: number
  stdout?: string
  stderr?: string
}

export function createExecOutputBuffer(): ExecOutputBuffer {
  return {
    stdout: [],
    stderr: [],
    stdoutRemainder: '',
    stderrRemainder: '',
    keptChars: 0,
    overflow: false,
  }
}

/** 由两个物理流重建兼容字段 output，禁止沿用 bridge 最终帧中的原始聚合输出。 */
export function combineExecOutput(stdout = '', stderr = ''): string {
  if (!stdout) return stderr
  if (!stderr) return stdout
  return stdout.endsWith('\n') ? `${stdout}${stderr}` : `${stdout}\n${stderr}`
}

/**
 * 构造发往 Gateway 的最小安全结果。
 *
 * output 是历史兼容字段，stdout/stderr 是真实物理流；只要物理流存在，output
 * 必须由物理流重建。否则旧 bridge 最终帧中的数十 MB 原文会作为第三份副本进入
 * HTTP，即使 stdout/stderr 已完成筛选，也仍可能使 Gateway OOM。
 */
export function buildSafeExecResultPayload(
  execId: string,
  output: string,
  exitCode: number,
  status?: string,
  stdout?: string,
  stderr?: string,
): ExecResultRelayPayload {
  const hasPhysicalStreams = stdout !== undefined || stderr !== undefined
  const canonicalOutput = hasPhysicalStreams ? combineExecOutput(stdout, stderr) : output
  const relayedOutput = status ? `${status}: ${canonicalOutput}` : canonicalOutput
  if (relayedOutput.length > MAX_RELAYED_OUTPUT_CHARS) {
    const error = 'QFK_EDGE_OUTPUT_LIMIT: 回传结果超过 256 KiB，已在浏览器边界拒绝'
    return {
      exec_id: execId,
      output: error,
      exit_code: -1,
      stderr: error,
    }
  }
  return {
    exec_id: execId,
    output: relayedOutput,
    exit_code: exitCode,
    stdout: stdout || undefined,
    stderr: stderr || undefined,
  }
}

export function lineMatchesExecFilter(line: string, filter: ExecOutputFilter): boolean {
  const candidate = filter.case_sensitive ? line : line.toLocaleLowerCase()
  const includes = filter.case_sensitive
    ? filter.include
    : filter.include.map(value => value.toLocaleLowerCase())
  const excludes = filter.case_sensitive
    ? filter.exclude
    : filter.exclude.map(value => value.toLocaleLowerCase())
  const includeOk = includes.length === 0 || (
    filter.include_mode === 'any'
      ? includes.some(value => candidate.includes(value))
      : includes.every(value => candidate.includes(value))
  )
  return includeOk && !excludes.some(value => candidate.includes(value))
}

/**
 * 对 terminal_bridge 流式分块做安全的字面量逐行筛选。
 *
 * remainder 保证关键字所在的逻辑行即使跨 WebSocket chunk 也不会漏匹配；
 * stdout/stderr 共用一个总预算，超过 256 KiB 后 Fail Closed。
 */
export function appendExecOutput(
  buffer: ExecOutputBuffer,
  filters: ExecOutputFilter[],
  source: 'stdout' | 'stderr',
  chunk: string,
  flush = false,
  maxChars = MAX_RELAYED_OUTPUT_CHARS,
): void {
  const sourceFilters = filters.filter(filter => filter.source === source)
  if (sourceFilters.length === 0) {
    if (!buffer.overflow && buffer.keptChars + chunk.length <= maxChars) {
      buffer[source].push(chunk)
      buffer.keptChars += chunk.length
    } else if (chunk) {
      buffer.overflow = true
    }
    return
  }

  const remainderKey = source === 'stdout' ? 'stdoutRemainder' : 'stderrRemainder'
  const input = buffer[remainderKey] + chunk
  const lines = input.split('\n')
  if (flush) {
    buffer[remainderKey] = ''
  } else {
    buffer[remainderKey] = lines.pop() || ''
  }

  for (let index = 0; index < lines.length; index += 1) {
    const isUnterminatedFinalLine = flush && index === lines.length - 1 && !input.endsWith('\n')
    const line = lines[index] + (isUnterminatedFinalLine ? '' : '\n')
    if (!sourceFilters.some(filter => lineMatchesExecFilter(line, filter))) continue
    if (buffer.keptChars + line.length > maxChars) {
      buffer.overflow = true
      continue
    }
    buffer[source].push(line)
    buffer.keptChars += line.length
  }
}

/**
 * 合并 bridge 最终帧。旧 bridge 会在流式 chunk 后再次携带完整大输出；只要某个
 * source 配置了 filter，就必须用已筛选缓冲覆盖最终帧，禁止原始大字符串进入 HTTP。
 */
export function finalizeExecOutput(
  buffer: ExecOutputBuffer,
  filters: ExecOutputFilter[],
  result: AgentExecResult,
): AgentExecResult {
  if (buffer.overflow || filters.some(filter => filter.source === 'stdout') || result.stdout === undefined) {
    result.stdout = buffer.stdout.join('')
  }
  if (buffer.overflow || filters.some(filter => filter.source === 'stderr') || result.stderr === undefined) {
    result.stderr = buffer.stderr.join('')
  }
  if (buffer.overflow) {
    result.exitCode = -1
    result.stderr = `${result.stderr || ''}\nQFK_EDGE_OUTPUT_LIMIT: 安全筛选后的结果仍超过 256 KiB，请收紧行筛选条件`.trim()
  }
  // output 是旧协议的聚合兼容字段，也必须同步覆盖。只覆盖 stdout/stderr 会让
  // 旧 bridge 的完整原始输出作为第三份副本进入 POST /exec-result。
  result.output = combineExecOutput(result.stdout, result.stderr)
  return result
}
