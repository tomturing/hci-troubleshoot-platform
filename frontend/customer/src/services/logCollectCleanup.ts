/**
 * 日志收集：节点侧清理**路径提示**的唯一生成器。
 *
 * 背景（2026-11-06，Q2026110600940）：P2 完成后 `task.archive_path` 是平台对象
 * key（`Q.../<task-id>.zip`），旧代码曾拿它拼成删除命令展示给用户，用户不懂
 * 平台内部字段，照着执行可能误删文件。
 *
 * 两条硬约束（用户 2026-11-06 明确要求）：
 * 1. **只给准确路径，绝不输出 rm / del 等删除命令**。是否处理、怎么处理，由用户
 *    自行确认；平台只负责把"到底是哪个文件"讲清楚。
 * 2. **路径必须来自节点侧证据并逐字校验**。拿不准就不给路径，绝不给错路径——
 *    给错路径比不给路径危险得多。
 *
 * 2026-11-16 起输出只保留一种形态：命中白名单的绝对路径提示；无可信路径时返回空串，
 * 界面据此完全不渲染这条提示——已交付任务不需要「平台不提供路径」这类无决策价值的告警。
 */

/** 必须与后端 `log_collect_node_path.NODE_ARCHIVE_DIR` 保持一致。 */
export const NODE_ARCHIVE_DIR = '/sf/data/local/acli/tmp/data/log_collect'
const CASE_ID_RE = /^Q\d{13}$/

/** 按工单号推导节点包路径；工单号不合法时返回 null。 */
export function expectedNodeArchivePath(caseId: string | null | undefined): string | null {
  if (!caseId || !CASE_ID_RE.test(caseId)) return null
  return `${NODE_ARCHIVE_DIR}/${caseId}.zip`
}

/**
 * 校验候选路径是否是可展示给用户的节点绝对路径，不合格返回 null。
 *
 * 只接受与目标值逐字相等：挡住平台对象 key（相对路径）、路径穿越、任意绝对
 * 路径，以及其他工单的包。后端已校验过，这里仍要独立判断——前端是最后一道闸门。
 */
export function trustedNodeArchivePath(
  caseId: string | null | undefined,
  candidate: string | null | undefined,
): string | null {
  const expected = expectedNodeArchivePath(caseId)
  if (!expected || typeof candidate !== 'string') return null
  return candidate === expected ? candidate : null
}

/**
 * 生成清理路径提示。候选按可信度从高到低传入
 * （实时节点对账 > 服务端桥日志核对的字段 > 任务 archive_path）。
 *
 * 2026-11-16 收敛（Q2026111600624）：结果态只显示正确且有价值的内容。
 * 命中白名单才给出路径；无可信路径时返回空串，界面据此完全不渲染这条提示。
 * 原「平台不提供路径」是一条对已交付用户毫无决策价值的告警，已移除。
 */
export function buildNodeCleanupTip(
  caseId: string | null | undefined,
  candidates: Array<string | null | undefined>,
): string {
  const path = candidates
    .map((candidate) => trustedNodeArchivePath(caseId, candidate))
    .find((value): value is string => value !== null)
  if (!path) return ''
  return (
    '平台保留副本供回看；HCI 节点上的采集包路径如下，请确认归属后自行处理：\n' +
    `${path}`
  )
}
