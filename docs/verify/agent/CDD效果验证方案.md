---
status: active
category: verify
audience: developer
last_updated: 2026-08-20
version: v2.0
owner: team
---

# CDD 效果验证方案

## 1. 验证目标

| 目标 | 验收标准 |
|---|---|
| 候选完整性 | 分类全部 `published + executable` KBD 进入 SignalPlan |
| 仿真等价性 | real/sim candidate IDs 完全相同，只允许 provider 不同 |
| 调度确定性 | 输入顺序变化不改变 plan ID 和调度顺序 |
| 证据安全 | `ERROR/UNKNOWN/BLOCKED` 不支持也不排除候选 |
| 结论安全 | 只有唯一支持且其余全部排除才能 `DEFINITIVE` |
| 采集效率 | 相同 acquisition 去重，优先高判别力、必要覆盖和依赖解锁 |
| Bundle 完整性 | 仿真分类内每个可能执行的 RouteKey 都有明确 fixture |

不再使用“top-15 在 8 步内缩到两篇”“选择最高频工具”或“连续三次失败退出”作为 CDD 验收指标。

## 2. 自动化测试

```bash
uv run pytest \
  backend/agent-service/tests/unit/test_investigation_agent.py \
  backend/agent-service/tests/unit/test_cdd_scheduler.py \
  backend/agent-service/tests/unit/test_kbd_differential.py -q
```

关键断言：

| 测试 | 覆盖能力 |
|---|---|
| `test_real_and_sim_modes_keep_identical_category_candidates` | TestRun target/revision 不得筛选候选 |
| `test_discriminating_shared_acquisition_wins_over_plain_coverage` | 评分选择判别 acquisition，不按频次 |
| `test_instruction_text_does_not_split_identical_runtime_acquisition` | execution identity 去重 |
| `test_producer_unlock_value_schedules_before_consumer` | producer/consumer 依赖解锁 |
| `test_candidate_input_order_does_not_change_plan_or_schedule` | 调度确定性 |
| `test_unknown_error_and_blocked_never_support_or_reject` | 执行失败不伪造反证 |
| `test_supported_plus_unresolved_is_partial_not_definitive` | 未决候选阻止 S4 |
| `test_supported_plus_rejected_is_definitive` | 唯一支持且全集闭合才确认 |
| `test_kbd27123_lsof_pid_then_ps_uses_canonical_argv_and_precise_process_identity` | 27123 变量链与命令契约 |

## 3. KBD27123 与 KBD30880 分类级仿真

### 3.1 Fixture 前置条件

KBD27123 场景 Bundle 至少覆盖：

```text
task get -k 启动虚拟机 -s failed -l 1
system lsof
system ps -p {{PID}} -o cmd=
system cat /sf/cfg/gpu_info.ini
```

前三类 observation 应支持 27123；`gpu_info.ini` observation 应由 30880 自身 Matcher 评价为反证。Runtime 不得直接返回“KBD30880 不命中”。

### 3.2 预期状态

```text
27123: required evidence satisfied -> SUPPORTED
30880: must evidence contradicted   -> REJECTED
Conclusion Gate                     -> DEFINITIVE
```

删除 `cat` fixture 后，预期必须退化为：

```text
30880 -> ERROR/INCONCLUSIVE
Gate  -> PARTIAL
```

如果仍输出 `DEFINITIVE`，说明候选被泄漏答案过滤或 ERROR 被错误当作反证。

## 4. 对抗性用例

1. 把 TestRun target 从 27123 改为同分类其他 KBD，candidate IDs 不得变化。
2. 让 TestRun revision 落后当前 KBD revision，Agent 候选不得变化；控制面应在创建前报告 Bundle stale。
3. 对非目标 RouteKey 返回 exit 127，候选必须未决而非排除。
4. 构造两篇信号完全相同的 KBD，结果必须 `PARTIAL`，不能依据 target 标签二选一。
5. 交换 API 返回 KBD 顺序，plan ID、调度顺序和结论必须一致。
6. 删除 producer 输出变量，下游必须 `BLOCKED`，不能用默认值补猜。

## 5. 现场证据

每次验收保存：

- `trace_id/case_id/conversation_id/test_run_id`；
- category snapshot ID 和完整 candidate support IDs；
- Bundle digest、category snapshot digest 和 RouteKey 覆盖清单；
- 每次 `exec_id -> fixture_id -> evaluation_id`；
- 每篇 KBD 的 SignalOutcome 和 CandidateState；
- Conclusion Gate 的 supported/rejected/inconclusive/not_executable 集合。

没有上述证据时，不能仅凭“目标 KBD 三条命令 exit code 0”宣称 Agent 端到端诊断通过。
