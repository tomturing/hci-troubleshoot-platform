/**
 * commandRisk.ts
 * acli 命令风险级别静态推断
 *
 * 设计原则（来自命令自动执行设计文档）：
 * - 未知命令默认 caution（宁可多拦截，不可漏放）
 * - 明确只读命令前缀 → readonly
 * - 明确破坏性命令前缀 → danger
 * - 其余 → caution
 */

export type RiskLevel = 'none' | 'readonly' | 'caution' | 'danger'

/**
 * 只读命令前缀白名单
 * 这些命令只查询状态，不改变系统状态，可安全重复执行
 */
const READONLY_PREFIXES: string[] = [
  // acli 查询类
  'acli platform info',
  'acli alert list',
  'acli alert get',
  'acli task list',
  'acli task get',
  'acli vm list',
  'acli vm get',
  'acli vm status',
  'acli node list',
  'acli node get',
  'acli node health',
  'acli storage list',
  'acli storage disk list',
  'acli storage pool list',
  'acli network list',
  'acli network ping',
  'acli job list',
  'acli job get',
  'acli info',
  'acli --formatter json alert',
  'acli --formatter json task',
  'acli --formatter json vm',
  'acli --formatter json node',
  'acli --formatter json storage',
  'acli --formatter json network',
  'acli --formatter json job',
  // Linux 只读命令
  'cat ',
  'ls ',
  'less ',
  'more ',
  'head ',
  'tail ',
  'ps ',
  'ps aux',
  'ps -ef',
  'df ',
  'du ',
  'free ',
  'uname ',
  'hostname',
  'uptime',
  'date',
  'id ',
  'whoami',
  'env',
  'echo ',
  'grep ',
  'awk ',
  'sed ',
  'sort ',
  'uniq ',
  'wc ',
  'find ',
  'which ',
  'type ',
  'top -b',
  'vmstat',
  'iostat',
  'netstat',
  'ss ',
  'ip addr',
  'ip route',
  'ifconfig',
  'ping ',
  'traceroute',
  'nslookup',
  'dig ',
  'curl ',
  'wget ',
  'journalctl',
  'systemctl status',
  'systemctl list',
  'dmesg',
  'lsblk',
  'lspci',
  'lsmod',
  'lscpu',
  'cat /proc',
  'cat /sys',
  'cat /etc',
]

/**
 * 危险命令前缀（不可逆操作，必须强制确认）
 */
const DANGER_PREFIXES: string[] = [
  // acli 破坏性操作
  'acli vm migrate',
  'acli vm delete',
  'acli vm reboot',
  'acli vm force',
  'acli vm power off',
  'acli vm poweroff',
  'acli node reboot',
  'acli node evacuate',
  'acli node delete',
  'acli node maintenance',
  'acli storage delete',
  'acli storage format',
  'acli network delete',
  'acli job cancel',
  'acli job abort',
  // 危险 Linux 命令
  'rm ',
  'rm -',
  'dd ',
  'mkfs',
  'fdisk',
  'parted',
  'wipefs',
  'shred',
  'truncate',
  'mv /',
  'chmod 777',
  'chmod -R',
  'chown -R',
  '> /dev/',
  'kill -9',
  'killall',
  'reboot',
  'shutdown',
  'halt',
  'poweroff',
  'init 0',
  'init 6',
  'systemctl stop',
  'systemctl disable',
  'systemctl kill',
  'iptables -F',
  'iptables --flush',
]

/**
 * 谨慎命令前缀（有副作用但通常可恢复）
 * safe-only 模式下拦截，aggressive 模式下自动执行
 */
const CAUTION_PREFIXES: string[] = [
  'acli vm snapshot',
  'acli vm clone',
  'acli storage snapshot',
  'systemctl restart',
  'systemctl start',
  'service ',
  'cp ',
  'mv ',
  'ln ',
  'mount ',
  'umount',
  'chmod ',
  'chown ',
  'useradd',
  'groupadd',
  'passwd',
]

/**
 * 推断命令的风险级别
 * @param command 命令字符串
 * @returns RiskLevel
 */
export function inferRiskLevel(command: string): RiskLevel {
  const cmd = command.trim()
  const cmdLower = cmd.toLowerCase()

  // danger 优先（精确前缀匹配，防止 readonly 命令名中包含 danger 前缀关键字）
  if (DANGER_PREFIXES.some((p) => cmdLower.startsWith(p.toLowerCase()))) {
    return 'danger'
  }

  // caution（有副作用但通常可恢复）
  if (CAUTION_PREFIXES.some((p) => cmdLower.startsWith(p.toLowerCase()))) {
    return 'caution'
  }

  // readonly（明确只读白名单）
  if (READONLY_PREFIXES.some((p) => cmdLower.startsWith(p.toLowerCase()))) {
    return 'readonly'
  }

  // 未知命令默认 caution（宁可多拦截）
  return 'caution'
}

/**
 * 判断命令在指定模式下是否可以自动执行（不需要任何确认）
 * @param riskLevel 命令风险级别
 * @param mode 自动执行模式
 * @returns true = 可直接执行；false = 需要用户介入
 */
export function canAutoExecute(
  riskLevel: RiskLevel,
  mode: 'off' | 'safe-only' | 'aggressive',
): boolean {
  if (mode === 'off') return false
  if (riskLevel === 'danger') return false // danger 永远不自动执行
  if (mode === 'safe-only') return riskLevel === 'none' || riskLevel === 'readonly'
  if (mode === 'aggressive') return riskLevel !== 'danger'
  return false
}

/**
 * 判断命令是否需要强制确认弹框（无论何种模式）
 */
export function requiresForceConfirm(riskLevel: RiskLevel): boolean {
  return riskLevel === 'danger'
}
