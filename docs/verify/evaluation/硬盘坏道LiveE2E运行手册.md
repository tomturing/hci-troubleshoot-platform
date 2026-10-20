# 硬盘坏道 Live E2E Smoke 运行手册（L3）

## 这是什么

贯穿**真实代码路径**的会话控制链路端到端 smoke：`gateway 下游 case-service → conversation-service → agent-service → (真实/桩)LLM → SOP 变量交互`。用于在发布前一次性验证「硬盘坏道」工单（Q2026101511258）历史上反复挤牙膏出现的跨层缝隙是否仍被守住。

对应避坑条目：`docs/verify/pitfalls/debugging.md` 的 **V-022**（根因）与 **V-021 / V-017 / V-020**（各历史缝隙）。

## 诚实边界（务必先读）

- **测试只能证伪，不能证明「100% 无 bug」**。本 smoke 收敛的是「跨层契约缝隙回归」，不是「功能全对」。
- 断言**只打在结构不变量 + 守卫事件**上，绝不断言逐字文本——真实 LLM 输出非确定性，比文本必产生假阳/假阴。
- 本 L3 是**后端会话链路级**，不跑真实浏览器渲染。#1149 那类前端态机/渲染缝隙由 jsdom 组件测（`MessageBubble.variable-submit.spec.ts`）守；若要真浏览器需另起 L2 playwright（本能力不含）。
- **发布前 gate，不进每 PR CI**（依赖 docker + 全栈 compose + LLM，慢且非确定性）。其**纯逻辑**（解析/判定/断言/对抗桩）脱网单测进 CI。
- 端到端**实跑必须在具备 docker+全栈的宿主机**执行；受限沙箱内只能跑纯逻辑单测。

## 组成

| 路径 | 职责 |
|------|------|
| `evaluation/live_e2e/asserts.py` | 纯逻辑：SSE 解析、交互卡归一化、user_questions 落地性、结论无溯源数值、5 条跨层不变量汇总 |
| `evaluation/live_e2e/driver.py` | httpx 异步驱动：建案→建会话→流式 SSE→变量交互提交（复刻 #1149「先落库再续跑」时序）→终态 |
| `evaluation/live_e2e/smoke.py` | CLI 入口：加载场景、`--attempts` 多数表决、dump 原始事件流 |
| `evaluation/live_e2e/scenarios/disk_badtrack.json` | 场景 spec（工单文本 + 期望 + 变量应答脚本 + 声明变量集） |
| `scripts/hci-sim/disk_badtrack_llm_stub.py` | 确定性对抗 LLM 桩（OpenAI 兼容），复刻历史坏输出逼守卫触发 |
| `evaluation/live_e2e/docker-compose.live-e2e.yml` | 端口暴露覆盖（把 case/conversation/agent 发布到宿主机，仅供本 smoke） |
| `scripts/verify/live_e2e_disk_badtrack.sh` | 全栈编排 + teardown |
| `make live-e2e-disk-badtrack` | 编排入口 target |

## 前置条件

- 宿主机可 `docker compose`，仓库根有 `.env`（含 `INTERNAL_API_TOKEN`、真实 LLM 凭据）。
- 已 `uv sync`（`.venv` 就绪，含 `httpx`）。

## 运行

```bash
# 默认：truncated 变体 + 确定性对抗桩 + 3 次多数表决
make live-e2e-disk-badtrack

# 显式参数
make live-e2e-disk-badtrack VARIANT=truncated MODE=stub ATTEMPTS=3

# 跑真实 LLM（不加桩）观察守卫是否在真实输出下约束
make live-e2e-disk-badtrack VARIANT=positive MODE=real ATTEMPTS=5

# 跑完不拆栈，便于手工进服务排查
make live-e2e-disk-badtrack LEAVE_RUNNING=1
```

直接调驱动（不重起栈，仅当服务已在跑时）：

```bash
PYTHONPATH=backend .venv/bin/python -m evaluation.live_e2e.smoke \
  --scenario evaluation/live_e2e/scenarios/disk_badtrack.json \
  --mode stub --attempts 3 \
  --case-base http://localhost:8001 --conv-base http://localhost:8002 --agent-base http://localhost:8005 \
  --internal-token "$INTERNAL_API_TOKEN"
```

## 参数

- `VARIANT`：`positive`（asan_disks 合法、disk_dev 可自动提取）/ `truncated`（asan_disks 截断成非法 JSON→disk_dev 转人工弹框，复现原始失败路径）。
- `MODE`：`stub`（用对抗桩确定性注入历史坏输出，断言守卫**必须**拦住，`expect_grounding_filter=True`）/ `real`（真实 LLM，仅观测不被强注入）。
- `ATTEMPTS`：多数表决次数，判绿阈值 = `ceil(N*2/3)`。
- `LEAVE_RUNNING`：`1` 保留容器栈不 teardown。

## 五条不变量（每条对应一个历史挤牙膏 bug）

| 不变量 | 守的缝隙 |
|--------|---------|
| `S0_category_confirmed` | V-020：确认分类命中期望叶子（不得 `intent_parse_failed`） |
| `S1_sop_hit` | 命中 sop_document id=4 根 |
| `VarSubmit_persisted` | #1149 后端真值：提交的变量必须落 `sop_execution.context_variables` |
| `UserQuestions_grounded` | #1151/V-021：最终提问不含重复索取已就绪变量、不含未声明 token |
| `Guard_user_questions_filter_fired` | #1151 守卫确曾触发（`agent_hallucination_detected_total{hallucination_type="ungrounded_user_question"}` 计数增量） |

## 『破坏缝隙能验红』演练

验收标准要求：人为破坏某一缝隙时 smoke **能红**。两条路径：

1. **脱网 CI 级（始终可跑）**：`tests/unit/test_live_e2e_asserts.py::test_evaluate_invariants_red_when_filter_bypassed` 构造「脏 user_questions 直抵输出 + 守卫未触发」，断言 `UserQuestions_grounded` 与 `Guard_user_questions_filter_fired` 双双判红——证明判据真的在守而非空跑。
2. **宿主机全栈级**：临时注释掉 `react_engine.py` 里 `filter_ungrounded_user_questions` 的丢弃分支后跑 `make live-e2e-disk-badtrack MODE=stub`，smoke 必须判红（stub 注入的坏提问未被过滤即命中 `UserQuestions_grounded`）。验红后**务必还原**。

## 产物与排障

- 产物归档在 `.local/live_e2e/<时间戳>/`：每轮 `events-*.jsonl`（原始 SSE 事件，可回放）、`observation-*.json`（结构化观测）、`logs/*.log`（case/conversation/agent/stub 容器日志切片）。
- 失败先定位是哪条缝隙：看 `observation-*.json` 的 `failed` 不变量名 → 对应上表 → 再去 `events-*.jsonl` 与该服务日志找根因。
- teardown 由脚本 `trap` 保证（`docker rm -f` 桩 + `compose down`），无常驻容器/端口泄漏；`LEAVE_RUNNING=1` 时须手动 `docker compose -f deploy/docker/docker-compose.yml down` 并 `docker rm -f hci-disk-stub`。

## 进 CI 的部分

纯逻辑单测（无需 docker/LLM）：

```bash
PYTHONPATH=backend uv run --python 3.12 --frozen pytest \
  tests/unit/test_disk_badtrack_llm_stub.py \
  tests/unit/test_live_e2e_asserts.py \
  tests/unit/test_live_e2e_driver.py -q
```
