#!/usr/bin/env python3
"""一次性修复脚本: BUG-R01, BUG-R05, BUG-R02+R03, BUG-R04, DC-04"""

ROOT = "/mnt/d/aihci/hci-troubleshoot-platform"


def fix_file(path: str, old: str, new: str, label: str) -> bool:
    content = open(path, encoding="utf-8").read()
    if old not in content:
        print(f"[FAIL] {label}: 未找到目标字符串")
        # 打印调试信息
        key = old.split("\n")[0][:50]
        idx = content.find(key)
        print(f"  关键字 '{key}' 位于 char {idx}")
        return False
    new_content = content.replace(old, new, 1)
    open(path, "w", encoding="utf-8").write(new_content)
    print(f"[OK] {label}")
    return True


# ──────────────────────────────────────────────────────────────────────────────
# BUG-R01: SopToolExecutor 幂等性检查缺少 return
# ──────────────────────────────────────────────────────────────────────────────
fix_file(
    f"{ROOT}/backend/agent-service/app/adapters/agents/htp/sop_tools.py",
    old=(
        '        # T-AGT-23: 幂等性检查 - 写操作工具在已完成节点中跳过执行\n'
        '        if tool_name in self.WRITE_OPERATION_TOOLS and self._completed_steps:\n'
        '            logger.info(\n'
        '                event="write_tool_idempotency_check",\n'
        '                tool_name=tool_name,\n'
        '                completed_steps=self._completed_steps,\n'
        '                conversation_id=self._conversation_id,\n'
        '            )\n'
        '\n'
        '        # SOP 导航工具：使用注入的上下文执行'
    ),
    new=(
        '        # T-AGT-23: 幂等性检查 - 写操作工具在 SOP 恢复模式下跳过重复执行\n'
        '        if tool_name in self.WRITE_OPERATION_TOOLS and self._completed_steps:\n'
        '            logger.info(\n'
        '                event="write_tool_idempotency_skip",\n'
        '                tool_name=tool_name,\n'
        '                completed_steps=self._completed_steps,\n'
        '                conversation_id=self._conversation_id,\n'
        '                message="SOP 恢复模式：跳过写操作工具，避免重复执行",\n'
        '            )\n'
        '            return {\n'
        '                "skipped": True,\n'
        '                "reason": (\n'
        '                    f"SOP 恢复模式：工具 {tool_name} 已在先前节点中执行，"\n'
        '                    "跳过重复执行以保证幂等性"\n'
        '                ),\n'
        '                "completed_steps_count": len(self._completed_steps),\n'
        '            }\n'
        '\n'
        '        # SOP 导航工具：使用注入的上下文执行'
    ),
    label="BUG-R01: SopToolExecutor 幂等性检查",
)

# ──────────────────────────────────────────────────────────────────────────────
# BUG-R05: agent_router 降级路径 1 (ops-agent 未启用)
# ──────────────────────────────────────────────────────────────────────────────
router_path = f"{ROOT}/backend/agent-service/app/adapters/agents/agent_router.py"

fix_file(
    router_path,
    old=(
        '                async for event in self._investigation_agent.process(\n'
        '                    session_id=session_id,\n'
        '                    messages=messages,\n'
        '                    category_id=category_id or "",\n'
        '                    diagnostic_stage=diagnostic_stage,\n'
        '                    env_context=env_context,\n'
        '                    assistant_type=settings.OPS_AGENT_FALLBACK_ASSISTANT_TYPE,\n'
        '                    case_id=case_id,\n'
        '                    user_id=user_id,\n'
        '                ):\n'
        '                    yield event\n'
        '            else:\n'
        '                try:\n'
        '                    async for event in self._ops_agent.process('
    ),
    new=(
        '                async for event in self._investigation_agent.process(\n'
        '                    session_id=session_id,\n'
        '                    messages=messages,\n'
        '                    category_id=category_id or "",\n'
        '                    diagnostic_stage=diagnostic_stage,\n'
        '                    env_context=env_context,\n'
        '                    assistant_type=settings.OPS_AGENT_FALLBACK_ASSISTANT_TYPE,\n'
        '                    case_id=case_id,\n'
        '                    user_id=user_id,\n'
        '                    sop_resume_context=sop_resume_context,  # T-AGT-23: SOP 执行恢复上下文\n'
        '                ):\n'
        '                    yield event\n'
        '            else:\n'
        '                try:\n'
        '                    async for event in self._ops_agent.process('
    ),
    label="BUG-R05: agent_router 降级路径 1 (ops-agent 未启用)",
)

# BUG-R05: 降级路径 2 (ops-agent 不可达)
fix_file(
    router_path,
    old=(
        '                    async for event in self._investigation_agent.process(\n'
        '                        session_id=session_id,\n'
        '                        messages=messages,\n'
        '                        category_id=category_id or "",\n'
        '                        diagnostic_stage=diagnostic_stage,\n'
        '                        env_context=env_context,\n'
        '                        assistant_type=settings.OPS_AGENT_FALLBACK_ASSISTANT_TYPE,\n'
        '                        case_id=case_id,\n'
        '                        user_id=user_id,\n'
        '                    ):\n'
        '                        yield event\n'
        '            return\n'
        '\n'
        '        # 2. pai-agent'
    ),
    new=(
        '                    async for event in self._investigation_agent.process(\n'
        '                        session_id=session_id,\n'
        '                        messages=messages,\n'
        '                        category_id=category_id or "",\n'
        '                        diagnostic_stage=diagnostic_stage,\n'
        '                        env_context=env_context,\n'
        '                        assistant_type=settings.OPS_AGENT_FALLBACK_ASSISTANT_TYPE,\n'
        '                        case_id=case_id,\n'
        '                        user_id=user_id,\n'
        '                        sop_resume_context=sop_resume_context,  # T-AGT-23: SOP 执行恢复上下文\n'
        '                    ):\n'
        '                        yield event\n'
        '            return\n'
        '\n'
        '        # 2. pai-agent'
    ),
    label="BUG-R05: agent_router 降级路径 2 (ops-agent 不可达)",
)

# BUG-R05: 降级路径 3 (pai-agent 未启用)
fix_file(
    router_path,
    old=(
        '                async for event in self._investigation_agent.process(\n'
        '                    session_id=session_id,\n'
        '                    messages=messages,\n'
        '                    category_id=category_id or "",\n'
        '                    diagnostic_stage=diagnostic_stage,\n'
        '                    env_context=env_context,\n'
        '                    assistant_type=fallback_type,\n'
        '                    case_id=case_id,\n'
        '                    user_id=user_id,\n'
        '                ):\n'
        '                    yield event\n'
        '            else:\n'
        '                try:\n'
        '                    async for event in self._pai.process('
    ),
    new=(
        '                async for event in self._investigation_agent.process(\n'
        '                    session_id=session_id,\n'
        '                    messages=messages,\n'
        '                    category_id=category_id or "",\n'
        '                    diagnostic_stage=diagnostic_stage,\n'
        '                    env_context=env_context,\n'
        '                    assistant_type=fallback_type,\n'
        '                    case_id=case_id,\n'
        '                    user_id=user_id,\n'
        '                    sop_resume_context=sop_resume_context,  # T-AGT-23: SOP 执行恢复上下文\n'
        '                ):\n'
        '                    yield event\n'
        '            else:\n'
        '                try:\n'
        '                    async for event in self._pai.process('
    ),
    label="BUG-R05: agent_router 降级路径 3 (pai-agent 未启用)",
)

# BUG-R05: 降级路径 4 (pai-agent 不可达)
fix_file(
    router_path,
    old=(
        '                    async for event in self._investigation_agent.process(\n'
        '                        session_id=session_id,\n'
        '                        messages=messages,\n'
        '                        category_id=category_id or "",\n'
        '                        diagnostic_stage=diagnostic_stage,\n'
        '                        env_context=env_context,\n'
        '                        assistant_type=settings.OPS_AGENT_FALLBACK_ASSISTANT_TYPE,\n'
        '                        case_id=case_id,\n'
        '                        user_id=user_id,\n'
        '                    ):\n'
        '                        yield event\n'
        '            return\n'
        '\n'
        '        # 3. HTP Agent'
    ),
    new=(
        '                    async for event in self._investigation_agent.process(\n'
        '                        session_id=session_id,\n'
        '                        messages=messages,\n'
        '                        category_id=category_id or "",\n'
        '                        diagnostic_stage=diagnostic_stage,\n'
        '                        env_context=env_context,\n'
        '                        assistant_type=settings.OPS_AGENT_FALLBACK_ASSISTANT_TYPE,\n'
        '                        case_id=case_id,\n'
        '                        user_id=user_id,\n'
        '                        sop_resume_context=sop_resume_context,  # T-AGT-23: SOP 执行恢复上下文\n'
        '                    ):\n'
        '                        yield event\n'
        '            return\n'
        '\n'
        '        # 3. HTP Agent'
    ),
    label="BUG-R05: agent_router 降级路径 4 (pai-agent 不可达)",
)

# ──────────────────────────────────────────────────────────────────────────────
# BUG-R02 + BUG-R03: re.match → re.fullmatch + try/except re.error
# ──────────────────────────────────────────────────────────────────────────────
fix_file(
    f"{ROOT}/backend/conversation-service/app/routes/sop_execution.py",
    old=(
        '        # 校验值是否符合 pattern\n'
        '        var_value_str = str(var_value) if not isinstance(var_value, str) else var_value\n'
        '        if not re.match(validation_pattern, var_value_str):\n'
        '            errors.append(\n'
        '                f"变量 \'{var_name}\' 值 \'{var_value_str}\' 不符合校验规则 \'{validation_pattern}\'"\n'
        '            )\n'
    ),
    new=(
        '        # 校验值是否符合 pattern（使用 fullmatch 保证完整匹配，BUG-R02）\n'
        '        var_value_str = str(var_value) if not isinstance(var_value, str) else var_value\n'
        '        try:\n'
        '            if not re.fullmatch(validation_pattern, var_value_str):\n'
        '                errors.append(\n'
        '                    f"变量 \'{var_name}\' 值 \'{var_value_str}\' 不符合校验规则 \'{validation_pattern}\'"\n'
        '                )\n'
        '        except re.error as exc:\n'
        '            # BUG-R03: validation_pattern 为无效正则时，记录警告并跳过该变量校验\n'
        '            logger.warning(\n'
        '                event="validate_variables_invalid_pattern",\n'
        '                var_name=var_name,\n'
        '                validation_pattern=validation_pattern,\n'
        '                error=str(exc),\n'
        '            )\n'
    ),
    label="BUG-R02+R03: re.fullmatch + re.error 异常处理",
)

# ──────────────────────────────────────────────────────────────────────────────
# BUG-R04: pai_agent_adapter 忙等待反模式 → 使用 drain task
# ──────────────────────────────────────────────────────────────────────────────
fix_file(
    f"{ROOT}/backend/agent-service/app/adapters/agents/pai/pai_agent_adapter.py",
    old=(
        '                    # 启动文本流任务（后台运行）\n'
        '                    text_task = asyncio.create_task(text_stream_task())\n'
        '\n'
        '                    # 主循环：从合并队列读取并 yield\n'
        '                    # 同时检查事件队列（事件会先写入 tool_event_queue，再转移到 merged_queue）\n'
        '                    while True:\n'
        '                        # 先检查工具事件队列（非阻塞）\n'
        '                        try:\n'
        '                            event = tool_event_queue.get_nowait()\n'
        '                            await merged_queue.put(event)\n'
        '                        except asyncio.QueueEmpty:\n'
        '                            pass\n'
        '\n'
        '                        # 从合并队列读取（阻塞一小段时间）\n'
        '                        try:\n'
        '                            item = await asyncio.wait_for(merged_queue.get(), timeout=0.01)\n'
        '                            if item is None:\n'
        '                                # 文本流结束，退出主循环\n'
        '                                break\n'
        '                            yield item\n'
        '                        except asyncio.TimeoutError:\n'
        '                            # 继续轮询\n'
        '                            continue\n'
        '\n'
        '                    # 等待文本流任务完成\n'
        '                    await text_task\n'
    ),
    new=(
        '                    # 启动文本流任务（后台运行）\n'
        '                    text_task = asyncio.create_task(text_stream_task())\n'
        '\n'
        '                    # 启动工具事件转移任务（BUG-R04: 替换忙等待轮询）\n'
        '                    # 使用 await 阻塞等待，避免 10ms 空转消耗 CPU\n'
        '                    async def tool_event_drain_task():\n'
        '                        """将 tool_event_queue 的事件转入 merged_queue（阻塞等待）"""\n'
        '                        while True:\n'
        '                            event = await tool_event_queue.get()\n'
        '                            await merged_queue.put(event)\n'
        '\n'
        '                    drain_task = asyncio.create_task(tool_event_drain_task())\n'
        '\n'
        '                    # 主循环：从合并队列阻塞读取（无超时，不空转）\n'
        '                    try:\n'
        '                        while True:\n'
        '                            item = await merged_queue.get()\n'
        '                            if item is None:\n'
        '                                # 文本流结束，退出主循环\n'
        '                                break\n'
        '                            yield item\n'
        '                    finally:\n'
        '                        # 停止 drain 任务\n'
        '                        drain_task.cancel()\n'
        '                        try:\n'
        '                            await drain_task\n'
        '                        except asyncio.CancelledError:\n'
        '                            pass\n'
        '\n'
        '                    # 等待文本流任务完成\n'
        '                    await text_task\n'
    ),
    label="BUG-R04: pai_agent_adapter 忙等待 → drain task",
)

# ──────────────────────────────────────────────────────────────────────────────
# DC-04: admin.py validation_pattern 写入时校验合法正则
# ──────────────────────────────────────────────────────────────────────────────
fix_file(
    f"{ROOT}/backend/kb-service/app/routes/admin.py",
    old=(
        '                if field not in allowed_fields:\n'
        '                    raise HTTPException(\n'
        '                        status_code=400,\n'
        '                        detail=f"字段 \'{field}\' 不允许编辑，可编辑字段：{sorted(allowed_fields)}",\n'
        '                    )\n'
        '                current_var[field] = value\n'
        '                updated_count += 1\n'
    ),
    new=(
        '                if field not in allowed_fields:\n'
        '                    raise HTTPException(\n'
        '                        status_code=400,\n'
        '                        detail=f"字段 \'{field}\' 不允许编辑，可编辑字段：{sorted(allowed_fields)}",\n'
        '                    )\n'
        '                # DC-04: validation_pattern 需验证为合法正则，防止写入无效值导致运行时 500\n'
        '                if field == "validation_pattern" and value:\n'
        '                    try:\n'
        '                        re.compile(value)\n'
        '                    except re.error as exc:\n'
        '                        raise HTTPException(\n'
        '                            status_code=400,\n'
        '                            detail=f"变量 \'{var_name}\' 的 validation_pattern \'{value}\' 不是合法正则: {exc}",\n'
        '                        )\n'
        '                current_var[field] = value\n'
        '                updated_count += 1\n'
    ),
    label="DC-04: admin.py validation_pattern 正则合法性校验",
)

print("\n全部修复完成。")
