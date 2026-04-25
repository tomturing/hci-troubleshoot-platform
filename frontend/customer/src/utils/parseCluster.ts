import { stripAnsi } from '@/api/terminal'

/**
 * 解析集群命令（acli cluster.list_ext）输出，提取结构化字段。
 *
 * 支持格式：
 * - key=value 行（acli 主要格式）：如 name=my-cluster
 * - 版本号行：如 6.10.0_R2 → hci_version
 * - 内核行：如 Linux host-xxx ... → kernel
 * - build 时间行：如 build 2025-09-01 ... → build_time
 *
 * 跳过：DONE marker、命令回显、Shell 提示符、update 日志、section 标题
 */
export function parseClusterOutput(output: string): Record<string, unknown> {
  const cleaned = stripAnsi(output)
  const result: Record<string, unknown> = {}
  const lines = cleaned.split('\n')

  for (const rawLine of lines) {
    const line = rawLine.trim()
    if (!line) continue

    // 1. 跳过 DONE marker 行（__HCI_DONE_xxx__:0 格式）
    if (line.startsWith('__HCI_DONE_')) continue

    // 2. 跳过命令回显行（包含完整命令字符串）
    if (line.includes('printf') || line.includes('__HCI_DONE_') || line.startsWith('acli ')) continue

    // 3. 跳过 Shell 提示符行（Sangfor:xxx # 格式）
    if (/^Sangfor:/.test(line)) continue

    // 4. 跳过历史记录行（update 日志，格式为 "YYYY-MM-DD HH:MM:SS  update | ..."）
    if (/^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\s+update/.test(line)) continue

    // 5. 跳过 section 标题行 [xxx]
    if (/^\[.+\]$/.test(line)) continue

    // 6. 解析 key=value 行（acli 的主要格式）
    const eqIdx = line.indexOf('=')
    if (eqIdx > 0) {
      const key = line.slice(0, eqIdx).trim()
      const value = line.slice(eqIdx + 1).trim()
      // key 必须是合法标识符（字母、数字、下划线），排除误匹配
      if (/^[a-zA-Z][a-zA-Z0-9_]*$/.test(key)) {
        result[key] = value
        continue
      }
    }

    // 7. 提取特殊自由文本行：版本号行（如 "6.10.0_R2"）
    if (/^\d+\.\d+\.\d+/.test(line) && !result['hci_version']) {
      result['hci_version'] = line.trim()
      continue
    }

    // 8. 提取内核信息行（Linux xxx ...）
    if (line.startsWith('Linux ') && !result['kernel']) {
      result['kernel'] = line.trim()
      continue
    }

    // 9. 提取 build 时间行（build YYYY-MM-DD ...）
    if (line.startsWith('build ') && !result['build_time']) {
      result['build_time'] = line.trim()
      continue
    }
  }

  return result
}
