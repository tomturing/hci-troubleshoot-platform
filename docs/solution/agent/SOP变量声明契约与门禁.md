---
status: active
category: solution
audience: developer
last_updated: 2026-10-26
owner: team
---

# SOP 变量声明契约与门禁

> 起因：Q2026102600003「手动填入之后，还是提示需要手动填」。同一现象已修过两次
> （#1149 前端已提交态、#1151 后端 `user_questions` 幻觉过滤），仍复发。

## 1. 复盘的结论

| 项 | 结论 |
|---|---|
| 直接原因 | 用户提交的值写入了 `context_variables["任务原始数据"]`，而该键**不在 SOP schema 中**；真正的 `vmid` / `task_logs` 从未被写入 |
| 上游原因 | SOP#3 变量表「依赖」列写的是自然语言 `任务原始数据`；22 个章节级变量因 4 列表头无法表达采集工具，解析后全部是 `tool_call + acquisition_tool=NULL` |
| 放大原因 | 运行时对未声明变量**降级为自由输入**（下发卡片、写入野键），且依赖悬空时用 `next_tool_call` 主动引导模型去请求那个不存在的变量 |
| 为什么修两次没用 | #1149/#1151 修的是"变量已写入但 UI / 模型说谎"，本次是"变量从未写对"。两次补丁都在下游，天然治不到 |

**根因是契约缺失，不是某个 SOP 写错。** `variable_schema` 长期是自由 JSONB，
三条写入路径里只有两条有校验，且校验器名为 `validate_variable_schema_dependencies`
却从不校验 `depends_on`。

## 2. 契约层（唯一事实源）

`backend/shared/models/sop_variable.py`

```python
SopVariableSpec   # 单变量：命名/类型/策略/字段矩阵
SopVariableSchema # 集合：依赖存在性/DAG 无环/重名/拓扑序
validate_variable_schema(raw, mode="save"|"publish"|"runtime") -> ValidationReport
```

关键约束：

- 变量名必须是 `^[a-z][a-z0-9_]{0,63}$`（中文描述只能进 `description`）
- `depends_on ⊆ 已声明变量名`，且无环
- 策略必须被别名表识别（`is_known_strategy`），禁止"未知值静默退化成 `user_input`"
- 策略 ↔ 字段矩阵：

| 策略 | 必需字段 | 禁止字段 |
|---|---|---|
| `tool_call` / `agent_pass` | `acquisition_tool` | — |
| `skill_call` | `acquisition_tool` + `output_path` | — |
| `derived` / `path_template` | `expression` | — |
| `json_extract` | `expression` + `depends_on` | — |
| `sop_default` | `default_value` | — |
| `user_input` / `user_confirm` | `description`（publish 态） | `depends_on` / `output_path` / `acquisition_tool` |

- `deprecated` 变量豁免矩阵与依赖校验（不逼着"修一份死配置"），但 publish 态禁止被活跃变量依赖

## 3. 三道闸

| 闸口 | 位置 | 失败行为 |
|---|---|---|
| save | `POST /sop/{id}/variable-schema` | 422 `variable_schema_contract_violation` |
| publish | `POST /sop/{id}/approve` | 422（额外禁止：废弃变量被依赖、必需变量无取值路径） |
| runtime | `sop_request_variable` | 结构化 error + `config_error=true` + ERROR 日志 |

运行时行为变更（核心）：

```text
请求未声明变量  -> sop_variable_not_defined（含可用变量名），不再下发输入卡片
依赖指向未声明  -> sop_schema_invalid_dependency，不再返回 next_tool_call
SOP 无变量声明  -> sop_variable_schema_missing
path_template   -> 由变量池渲染（/sf/log/{task_day}/sfvt_qemu_{vmid}.log）
```

## 4. 变量包（防止复制粘贴再次漂移）

- 表 `sop_variable_pack`（`pack_name` / `version` / `variables`）
- `sop_document.applied_packs` 记录引用，用于漂移检测
- 服务 `backend/kb-service/app/services/variable_pack.py`
  - `expand_pack_variables`：包变量注入 + SOP 自有定义优先 + 冲突显式返回
  - `apply_pack`：合并后**必须通过契约校验**才允许落库
  - `detect_drift`：包升级后未同步的 SOP 会被点名（供 CI / 巡检）
- 包**不是运行时依赖**：展开后仍写回 `variable_schema`，agent-service 只读扁平数组

首批种子：`hci_common_vt_diag`（22 个虚拟机排障通用变量，见 052 迁移）。

## 4.1 提交后续跑闭环（"填了还在问"的最后一环）

变量提交后**后端不会自动续跑**：`/interactive-response` 只负责写变量池，
该会话上一轮的 SSE 连接已结束，后端无法主动推送。历史实现有两个断裂点：

| 断裂点 | 现象 | 修复 |
|---|---|---|
| `chatStore.sendMessage` 在 `isStreaming` 时直接 `return false` | 用户提交瞬间流未收尾 → 续跑消息被静默吞掉，界面停住 | 前端 `resumeAfterStreamIdle`：轮询等流结束（上限 60s）后**真正重发** |
| 路由只返回 `{"ok": true}` | 调用方把 HTTP 200 当成"已继续" | 返回 `should_resume`，明确区分"已写库"与"已继续" |
| `add_message` 漏传 `trace_id` | TypeError 被 `except` 吞成 WARNING，用户提交记录从未落库 | 补 `trace_id=request_id`，并改为 ERROR 结构化日志 |

## 4.2 卡片可读性（命名自由的代价）

变量名受标识符约束，`qemu_log_path` 直接展示给用户可读性差。契约支持
`display_name`，前端按 `display_name → description 首句 → variable_name` 渲染，
迁移 053 为存量变量批量补齐（截断规则：全角/半角括号、分号、句号取首段）。

注意：这只影响展示，不改变任何命名或提交语义。

## 5. 存量整改（051/053）

| SOP | 整改前 | 整改后 |
|---|---|---|
| #2 磁盘寿命到期 | 22 处 `tool_call` 缺工具 + 2 处悬空依赖 + `is_sys_disk` 用未实现的 `llm_inference` | save/publish 均 0 error |
| #3 虚拟机开机失败 | 22 处 `tool_call` 缺工具 + `task_logs` 整行列错位 | save/publish 均 0 error |
| #4 硬盘坏道 | 已被 040 整改，0 违规 | 不动（合规样板） |
| #5 内存条ECC | 3 处 `skill_call` 缺 `output_path` + 1 处 `json_extract` 缺依赖 | save/publish 均 0 error |
| #1 虚拟机开机失败排查流程（draft） | 缺 `task_day` 声明 + 24 处未声明采集工具 | 补齐 `task_day`（derived）后与 #2/#3 同批整改，现 0 error |

整改规则分三类，不做"看起来能过"的兜底：

1. **路径类** → `path_template`（由 `task_day` / `vmid` 推导）
2. **命令类** → `tool_call:acli_exec`，命令取自各 SOP 正文「前置检查」
3. **无采集定义、只能人工判断** → `user_input`，并补全"用途 + 取值线索"

## 6. 验收

```bash
# 真实数据驱动的运行时闭环（不需要起全套服务）
python scripts/verify/sop_variable_runtime_e2e.py --schema-file /tmp/sop3_schema.json

# 任意环境的契约巡检（退出码非 0 即阻断）
DATABASE_URL=... python scripts/verify/sop_variable_contract_check.py --status published

# 契约与合并规则
PYTHONPATH=backend python -m pytest backend/shared/tests/test_sop_variable_spec.py -q
cd backend/kb-service && PYTHONPATH=.:../.. python -m pytest tests/test_variable_pack.py tests/test_sop_publish_dependencies.py -q

# 运行时闸门
cd backend/agent-service && PYTHONPATH=.:../.. python -m pytest tests/unit/test_sop_request_variable_fail_loud.py -q

# 迁移幂等 + 悬空依赖归零（回滚式 dry-run）
sed 's/^COMMIT;$/ROLLBACK;/' database/data-migrations/051_sop_variable_contract_alignment.sql | psql ...
```

必须同时满足：

- [ ] 存量扫描 `depends_on ⊄ declared` = 0 行
- [ ] 整改后 SOP#2/#3/#5 在 save/publish 双模式 0 error
- [ ] 请求未声明变量返回 error 且**不产生** `interactive_request` 消息
- [ ] `path_template` 变量不再出现人工输入卡片
- [ ] `context_variables` 的键 100% ∈ schema 声明集
- [ ] 变量提交后**确实发生第二轮推理**（`sendMessage` 被调用第二次），而不是只有一句提示
- [ ] 卡片标题显示可读文案，不直接暴露 `qemu_log_path` 这类技术名

## 7. 已知边界

- 路径类变量依赖 `task_day`/`vmid` 已声明。SOP#1 原本缺 `task_day`，已按 SOP#3 的既有约定
  补为 `derived: split(task_date, '-').2`；后续新增 SOP 若用到 `{task_day}` 需自行声明。
- 10 个"人工确认类"变量（`host_ip` / `pci_address` / `vm_disk_path` 等）**没有采集命令可依**，
  SOP 正文只把它们当命令参数使用。当前以 `user_input` + 明确 `description` 落地，
  若后续要自动化，需由 SOP 作者补充真实采集命令后再改为 `tool_call`。
- `llm_inference` / `agent_pass` 在引擎中**尚未实现**，契约已禁止其作为自动策略使用；
  若将来实现，需同步加入 `RUNTIME_SUPPORTED_STRATEGIES` 白名单。
