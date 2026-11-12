"""日志收集失败归因（error_message → error_code 的确定性映射）。

## 背景（2026-11-12 现场）

staging 近 30 天 118 个失败任务里 **76 个（64.4%）error_code 为空**，只能靠
`error_message` 文本猜原因。逐条溯源后确认：空码不是某一处漏写，而是
**归因责任没有唯一收口**——bridge、customer 前端、case-service 各自在
「写失败」时顺带补码，任何一条路径漏掉就永久留白。

第一性原理：**失败必须可归因是平台的责任，不是调用方的礼貌。**
因此在保存边界（`LogCollectService.update_task`）做确定性补全：
拿到终态失败却没有码时，按 `error_message` 做**有序模式匹配**落一个规范码，
匹配不到时落 `LC-PLATFORM-011`（未归因），而不是留空。

## 约束

1. 只做「补空」，绝不覆盖已有码：上层包装码是更强的信息（谁失败谁出码）；
2. 匹配顺序从**具体到泛化**：`客户机磁盘空间不足` 必须先于 `磁盘空间不足`，
   否则会把「清临时目录即可」误判成「清节点 /sf/data/local」；
3. 返回的码必须在 `log_collect_error_catalog.json` 中定义——
   本模块的每条规则都由单测与目录强校验，禁止臆造码值。
"""

from __future__ import annotations

from ..schemas.log_collect_errors import load_error_catalog

# 未归因兜底码：失败事实已确认，但没有可识别的根因文本。
# 它本身就是一个信号——出现量上升说明某个上游在失败时没带原因，需要补码。
UNATTRIBUTED_CODE = "LC-PLATFORM-011"

# 与 bridge 的通道断开（前端 BRIDGE_CHANNEL_LOST_MESSAGE）：
# 节点操作根本没送达，属于平台侧通道问题，重连后可重试。
BRIDGE_CHANNEL_LOST_CODE = "LC-PLATFORM-010"

# 有序规则表：(子串, 错误码)。**顺序即优先级**，新增规则必须想清楚会不会
# 抢走下面更具体的语义（例如「上传会话大小不一致」必须先于「大小不一致」）。
_RULES: tuple[tuple[str, str], ...] = (
    # ── 场景清单分发（SPC）：必须先于 ENV/EXEC 的泛化规则 ─────────────
    # 「节点 SSH 认证失败」若排在 ENV 的「SSH 认证失败」之后，会被抢成 ENV-007，
    # 用户就会去查节点环境而不是场景清单扇出；所以 SPC 整体放最前。
    ("场景清单目录缺失", "LC-SPC-001"),
    ("场景清单目录不唯一", "LC-SPC-002"),
    ("场景清单备份回读不一致", "LC-SPC-003"),
    ("场景清单备份失败", "LC-SPC-003"),
    ("场景清单写入后校验不一致", "LC-SPC-004"),
    ("场景清单写入被拒绝", "LC-SPC-004"),
    ("场景清单写入失败", "LC-SPC-004"),
    ("场景清单文件缺失", "LC-SPC-009"),
    ("场景清单回滚失败", "LC-SPC-005"),
    ("节点 SSH 认证失败", "LC-SPC-007"),
    ("节点命令执行超时", "LC-SPC-008"),
    ("节点 SSH 连接失败", "LC-SPC-006"),
    ("场景清单写入未通过", "LC-SPC-010"),
    # 2026-11-12 之前的历史文案：只保留归因，不再产生。
    ("场景未能写入全部目标节点", "LC-SPC-010"),
    ("收集专项", "LC-PLUGIN-016"),
    ("节点上没有所选专项", "LC-PLUGIN-016"),
    ("未建立采集任务", "LC-PLUGIN-015"),
    # ── 平台存储（本层自身产生的故障，最具体）────────────────────────
    ("存储目录不可写", "LC-PLATFORM-007"),
    ("不可写", "LC-PLATFORM-007"),
    ("存储空间不足", "LC-PLATFORM-008"),
    # ── 平台内部错误：有归因（是平台 500）就不该落「未归因」──────────
    ("判定为僵尸会话已清理", "LC-PLATFORM-013"),
    ("平台收尾失败", "LC-PLATFORM-012"),
    ("INTERNAL_ERROR", "LC-PLATFORM-012"),
    ("服务内部错误", "LC-PLATFORM-012"),
    ("status code 5", "LC-PLATFORM-012"),
    # ── 与 bridge 的通道断开：节点操作未送达 ─────────────────────────
    ("与 terminal_bridge 的连接已断开", BRIDGE_CHANNEL_LOST_CODE),
    ("bridge 连接已断开", BRIDGE_CHANNEL_LOST_CODE),
    # ── 客户机侧容量（必须早于泛化的「磁盘空间不足」）───────────────
    ("客户机还有其它交付任务", "LC-TRANSFER-009"),
    ("并发容量不足", "LC-TRANSFER-009"),
    ("客户机磁盘空间不足", "LC-TRANSFER-008"),
    ("临时目录空间不足", "LC-TRANSFER-008"),
    ("写入本地文件失败", "LC-TRANSFER-008"),
    ("%TEMP%", "LC-TRANSFER-008"),
    # ── 节点侧前置条件 ──────────────────────────────────────────────
    ("/sf/data/local", "LC-ENV-004"),
    ("磁盘空间不足", "LC-ENV-004"),
    ("可用空间", "LC-ENV-004"),
    ("SSH 会话不存在", "LC-ENV-006"),
    ("ssh_connect", "LC-ENV-006"),
    ("需重新连接节点", "LC-ENV-006"),
    ("重新连接 SSH", "LC-ENV-006"),
    ("交付需要重新认证", "LC-ENV-006"),
    ("SSH 认证失败", "LC-ENV-007"),
    ("节点离线", "LC-ENV-008"),
    ("host offline", "LC-ENV-008"),
    ("日志服务器不可达", "LC-ENV-005"),
    # ── acli 缺失 / 命令层 ─────────────────────────────────────────
    ("acli: command not found", "LC-EXEC-003"),
    ("未知的命令或者命名空间", "LC-EXEC-003"),
    ("plugin: no such file", "LC-EXEC-003"),
    ("命令返回非零", "LC-EXEC-003"),
    ("命令执行超时", "LC-EXEC-002"),
    ("SSH 等待结果失败", "LC-EXEC-005"),
    # ── 安装 ───────────────────────────────────────────────────────
    ("下载安装包失败", "LC-INSTALL-002"),
    ("安装包上传到 HCI 失败", "LC-INSTALL-004"),
    ("acli 安装命令失败", "LC-INSTALL-005"),
    ("安装后版本验证失败", "LC-INSTALL-006"),
    ("落盘校验失败", "LC-INSTALL-007"),
    # ── 插件语义 ───────────────────────────────────────────────────
    ("已有进行中的任务", "LC-PLUGIN-007"),
    ("正在执行", "LC-PLUGIN-007"),
    ("已有采集在跑", "LC-PLUGIN-007"),
    ("节点上已无任务", "LC-PLUGIN-008"),
    ("任务不存在", "LC-PLUGIN-008"),
    ("未找到对应的日志收集任务", "LC-PLUGIN-008"),
    ("采集未完成", "LC-PLUGIN-010"),
    ("日志采集执行失败", "LC-PLUGIN-010"),
    ("取消采集失败", "LC-PLUGIN-011"),
    ("部分主机采集失败", "LC-PLUGIN-012"),
    # ── 制品/交付（「源包仍在写入」必须先于泛化的分片失败）──────────
    ("源包已变化", "LC-TRANSFER-010"),
    ("源包仍在写入", "LC-TRANSFER-010"),
    ("仍未写完", "LC-TRANSFER-010"),
    ("会话已被新代替代", "LC-TRANSFER-010"),
    ("上传会话大小不一致", "LC-TRANSFER-010"),
    ("按新基线重建", "LC-TRANSFER-010"),
    ("压缩包不存在", "LC-TRANSFER-001"),
    ("源包可能已被清理", "LC-TRANSFER-001"),
    ("本地副本不存在", "LC-TRANSFER-001"),
    ("未找到采集产物", "LC-TRANSFER-001"),
    ("校验和不一致", "LC-TRANSFER-002"),
    ("上传中断", "LC-TRANSFER-003"),
    ("分块上传失败", "LC-TRANSFER-003"),
    ("片）", "LC-TRANSFER-003"),
    ("无法确认日志包", "LC-TRANSFER-006"),
    ("归档核验未通过", "LC-TRANSFER-007"),
    ("直传归档核验", "LC-TRANSFER-007"),
    # 代传通用失败文案（relay 兜底文本）：属于分片上传阶段的失败。
    ("归档到日志服务器失败", "LC-TRANSFER-003"),
    # ── 泛化症状词（必须排在所有具体规则之后）────────────────────────
    # 「超时」任何阶段都会出现，是无信息量的症状词。放在前面会把
    # 「超时；归档核验未通过」抢成 LC-EXEC-002（命令执行超时），
    # 把用户引去查命令，而真正该查的是归档核验。只有所有具体规则
    # 都未命中时才允许退回这一条。
    ("超时", "LC-EXEC-002"),
)


def derive_error_code(message: str | None) -> str:
    """把失败文案确定性映射为规范错误码。

    空文案同样返回未归因码：**「没有原因」本身也是一种需要被看见的状态**，
    留空会让面板与聚合 SQL 永远无法统计有多少失败缺归因。

    匹配不到任何规则时返回 :data:`UNATTRIBUTED_CODE`，绝不返回空串——
    调用方（保存边界）依赖这个不变量保证「失败必有码」。
    """
    text = (message or "").strip()
    if text:
        for needle, code in _RULES:
            if needle in text:
                # 目录缺失时 load_error_catalog 会降级为空；此处只做校验不抛错，
                # 避免历史脏数据把一次 PATCH 变成 500。
                if code in load_error_catalog():
                    return code
                continue
    return UNATTRIBUTED_CODE
