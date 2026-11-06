"""日志收集：节点侧清理路径的唯一可信来源。

背景（2026-11-06，Q2026110600940）：
P2 代传完成后，`complete` 端点会把 `task.archive_path` 写成**平台对象 key**
（形如 `Q2026110600940/<task-id>.zip`）。结果态的清理提示曾直接拼
删除命令，于是给用户一条指向对象存储 key 的相对路径——
它既删不掉节点上的包，又可能在节点工作目录里误删同名文件。

**为什么必须白名单而不是"看起来像路径就用"**：
删除命令是破坏性操作，用户对平台内部字段（对象 key / 任务表列）没有辨识能力。
唯一能证明"这个绝对路径是节点上的那个包"的证据，是 bridge 把节点
`log_collect.progress` 报告原样写进 `bridge_execution_logs` 的记录。
因此本模块只认一种值：`/sf/data/local/acli/tmp/data/log_collect/<case_id>.zip`，
且必须由调用方先确认桥日志里确实出现过；不合格一律返回 None，
由前端改成"不提供路径"。平台只告诉用户准确路径，**不生成 rm / del 等删除命令**，
是否处理、怎么处理由用户自行确认，绝不降级成猜测值。
"""

from __future__ import annotations

import re

# acli 采集产物的固定目录（现场实测：所有工单的 progress 报告都在这一目录下）。
NODE_ARCHIVE_DIR = "/sf/data/local/acli/tmp/data/log_collect"
# 工单号格式：Q + 13 位数字。过严会让个别历史单拿不到路径，但这正是
# "宁可不给命令，也不给一条可能删错文件的命令" 的代价。
_CASE_ID_RE = re.compile(r"^Q\d{13}$")


def expected_node_archive_path(case_id: str) -> str | None:
    """按工单号推导节点包的绝对路径；工单号不合法时返回 None。

    推导结果本身**不是**可直接执行的证据，只是待 SQL 核对的目标值。
    """
    if not isinstance(case_id, str) or not _CASE_ID_RE.fullmatch(case_id):
        return None
    return f"{NODE_ARCHIVE_DIR}/{case_id}.zip"


def trusted_node_archive_path(case_id: str, candidate: str | None) -> str | None:
    """校验候选路径是否可作为清理命令的参数，不合法返回 None。

    只接受与目标值逐字节相等的路径：这样既能挡住平台对象 key（相对路径）、
    路径穿越（`..`）、任意绝对路径，也能挡住"上一单/别的工单"的包。
    """
    expected = expected_node_archive_path(case_id)
    if expected is None or not isinstance(candidate, str):
        return None
    return candidate if candidate == expected else None
