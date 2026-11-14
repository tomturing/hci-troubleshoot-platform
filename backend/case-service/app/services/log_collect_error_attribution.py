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
from .log_collect_attribution_rules_generated import ATTRIBUTION_RULES

# 未归因兜底码：失败事实已确认，但没有可识别的根因文本。
# 它本身就是一个信号——出现量上升说明某个上游在失败时没带原因，需要补码。
UNATTRIBUTED_CODE = "LC-PLATFORM-011"

# 与 bridge 的通道断开（前端 BRIDGE_CHANNEL_LOST_MESSAGE）：
# 节点操作根本没送达，属于平台侧通道问题，重连后可重试。
BRIDGE_CHANNEL_LOST_CODE = "LC-PLATFORM-010"

# 有序规则表：(子串, 错误码)。**顺序即优先级**，新增规则必须想清楚会不会
# 抢走下面更具体的语义（例如「上传会话大小不一致」必须先于「大小不一致」）。
_RULES = ATTRIBUTION_RULES


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
