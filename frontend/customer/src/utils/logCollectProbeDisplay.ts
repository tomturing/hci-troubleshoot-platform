/**
 * 日志收集探测卡展示文案（纯函数）。
 *
 * 从面板里抽出，是为了让产品组件与真浏览器验收页调用同一份拼接逻辑，
 * 避免"单测走一套、浏览器里走另一套"的口径漂移。
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
