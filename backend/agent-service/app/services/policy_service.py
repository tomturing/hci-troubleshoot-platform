import logging

logger = logging.getLogger("agent.policy")


class PolicyService:
    """服务端安全与执行策略服务"""

    def evaluate_needs_confirm(
        self,
        tool_name: str,
        risk_level: int,
        require_all_confirm: bool = False,
        execution_mode: str = "safe-only",  # off / safe-only / aggressive
    ) -> bool:
        """
        评估当前工具执行是否需要用户确认。

        规则：
        1. 风险等级为 3 (Block) 的工具直接拦截（这在 react_engine.py 中独立处理，但这里做备用判定）。
        2. 风险等级 >= 2 的高危/写操作工具，无论前端是什么模式（哪怕是 aggressive），必须返回 True 强制用户授权确认。
        3. 如果 `require_all_confirm` 为 True（例如处于关键修复阶段），所有工具无论风险等级，一律返回 True 要求确认。
        4. 风险等级 <= 1 且没有要求全部确认的工具，根据 execution_mode 判定：
           - off: 强制需要确认 (returns True)
           - safe-only / aggressive: 允许自动执行，不需要确认 (returns False)
           - 其他情况 (如 direct / react 默认值): 降级为安全模式，需要确认 (returns True)
        """
        logger.info(
            "评估工具执行确认策略: tool_name=%s, risk_level=%d, require_all_confirm=%s, execution_mode=%s",
            tool_name,
            risk_level,
            require_all_confirm,
            execution_mode,
        )

        # 规则 1：全部确认模式开启，强制确认
        if require_all_confirm:
            return True

        # 规则 2：高风险（risk_level >= 2）强制确认，杜绝前端 aggressive 绕过
        if risk_level >= 2:
            return True

        # 规则 3：低风险工具，根据执行模式决策
        if execution_mode == "off":
            return True
        elif execution_mode in ("safe-only", "aggressive"):
            # safe-only / aggressive 均允许自动执行低风险工具
            return False

        # 兼容旧默认值（如 direct / react），默认强制需要确认
        return True
