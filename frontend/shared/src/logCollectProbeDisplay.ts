/**
 * 日志收集探测信息展示文案（纯函数）。
 *
 * 客户侧探测卡与管理端任务进展共用同一份拼接逻辑，避免同一个事实
 * 在两端被拼成不同口径（"唯一出处"门禁也要求字典闭合）。
 */

/**
 * 「版本架构」：HCI 产品版本 + 节点 CPU 架构。
 *
 * 第一性原理：排障必须同时看到「软件版本」与「硬件指令集」，缺一就会把
 * 版本相关缺陷误判成环境问题。
 * 对抗性审查：环境上下文可能尚未采集（手工指定工单号/采集失败），此时只显示架构；
 * hci_version 的契约类型是 unknown，必须收窄成字符串才不会渲染成 "[object Object]"。
 */
export function formatVersionArchitecture(
  architecture: string | null | undefined,
  hciVersion: unknown,
): string {
  const arch = (architecture || '').trim()
  const version = typeof hciVersion === 'string' ? hciVersion.trim() : ''
  if (version && arch) return `HCI ${version} / ${arch}`
  if (version) return `HCI ${version}`
  return arch
}

/**
 * acli 版本文案：版本号 + 编译时间。
 *
 * 第一性原理：`acli --version` 的两行输出才是完整事实（1.0.1 / build 2026-09-15 21:00:04），
 * 只显示主版本会让"同版本不同构建"无法区分——排障时恰恰要按 build 比对。
 * 对抗性审查：旧后端不回传 build（字段缺省）时只显示版本号，不能显示 "build undefined"；
 * build 字段在契约里是 unknown，同样必须收窄。
 */
export function formatAcliVersion(
  version: string | null | undefined,
  buildTime: unknown,
  installed = true,
): string {
  if (!installed) return '未安装'
  const v = (version || '').trim()
  const build = typeof buildTime === 'string' ? buildTime.trim() : ''
  return build && v ? `${v} (build ${build})` : v
}
