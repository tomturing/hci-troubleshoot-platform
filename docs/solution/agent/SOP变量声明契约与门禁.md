---
status: active
category: solution
audience: developer
last_updated: 2026-10-27
owner: team
---

# SOP 变量声明契约与门禁

> 起因：Q2026102600003「手动填入之后，还是提示需要手动填」。同一现象已修过两次
> （#1149 前端已提交态、#1151 后端 `user_questions` 幻觉过滤），仍复发。

## 变更历史

| 日期 | 版本 | 变更内容 | 关联事件文档 |
|------|------|---------|------------|
| 2026-10-26 | v1.0 | 初版：变量声明契约、三道闸、变量包与 051~053 数据整改（#1270） | — |
| 2026-10-27 | v1.1 | 补 `acquisition_args_template` 类型契约：054 数据收敛 + shared/engine/main 三层守卫 + CI 巡检与单测；补充 E2E 真采集命令与两处必踩坑（#1291） | — |

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
- `tool_call` 的 `acquisition_args_template` / `acquisition_args` **必须是对象**（错误码
  `SOP_VAR_ARGS_TEMPLATE_NOT_OBJECT`）。该值渲染后会被当作工具参数**原样**传给执行器
  （`CompositeToolExecutor.execute` 签名要求 `args: dict`）；写成裸字符串（典型是把
  `output_path` 的值 `"stdout"` 串到了模板列）会在运行时抛
  `AttributeError: 'str' object has no attribute 'get'`，被"失败即接管"吞成人工弹框，
  表现为同一变量反复弹卡、SOP 永不推进，而日志里只剩那句报错、根因不可见。
  只能人工提供的变量应当直接声明 `user_input`，不要留一个跑不通的模板。

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
参数模板非对象  -> 传参前拦下并记 contract_error（不再把 str 当 args 传下去）
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

## 5.1 054：参数模板类型契约收敛

051 在归位日志类变量时写反了 `COALESCE` 优先级：

```sql
COALESCE(item.v -> 'acquisition_args_template', item.v -> 'expression')
```

`->` 取到的是列错位遗留的脏值 `"stdout"`（本属于 `output_path`），
真正的 `{command, reason}` 模板在 `expression` 里却没被优先采用；
且该段 `UPDATE` 的判据是 `acquisition_strategy <> 'tool_call'`，
对"策略已对、模板仍错"的存量行**完全漏判**，于是 `"stdout"` 一路留到了运行时。

054 以各 SOP 正文 `prerequisites` 里的前置检查命令为唯一事实源逐条归位：

| SOP | 变量 | 归位后的 `command` |
|---|---|---|
| #3 | `task_logs` | `acli --formatter json task get -k "启动虚拟机" -s 'failed'` |
| #2 | `alert_logs` | `acli --formatter json alert get -k "磁盘"` |
| #5 | `alert_logs` | `acli --formatter json alert get -k "内存"` |
| #2 | `task_logs` | 已 `deprecated`、正文无对应命令 → 仅清除非法模板，不再采集 |

迁移末尾带收敛断言（`DO $$ ... RAISE EXCEPTION`）：任何活跃 `tool_call` 变量的模板
若仍是非对象标量，迁移当场失败，而不是留到运行时降级。回滚快照表 `_backup_20261026_054`。

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

真 E2E（SOP#3 虚拟机开机失败，含 bridge 真采集）：

```bash
# 1) 为仿真实例签发 lease，生成 driver 需要的 connection.json
python evaluation/live_e2e/fixtures/disk_badtrack/make_connection.py \
  --namespace <sim-ns> --pod <sim-pod> --svc <sim-svc> \
  --manifest /etc/hci-sim/bundles/<vm-scenario>-fixture-manifest.json \
  --support-id <id> --out /tmp/vm_connection.json

# 2) 驱动真实会话（--sim 才会起 headless bridge relay，把 SSE 上的
#    agent_exec_command 真下发到 hci-sim 并回贴 /exec-result）
python scripts/verify/vm_start_failure_e2e.py \
  --case-base http://<case>:8001 --conv-base http://<conv>:8002 --agent-base http://<agent>:8005 \
  --internal-token "$INTERNAL_API_TOKEN" --max-turns 6 \
  --sim --bridge-url ws://<bridge>:9999 --bridge-origin http://<bridge>:9999 \
  --connection /tmp/vm_connection.json --dump-dir /tmp/e2e-dump
```

两个必踩的坑：

- **不启用 `--sim` 会话永远到不了终态**：`on_exec_command` 为 `None` 时命令无人执行，
  agent-service 只能等 `AGENT_EXEC_RESULT_TIMEOUT_SECONDS`（默认 150s）后重试，SSE 流不结束；
  而 `run_scenario` 只在正常返回时写 `--dump-dir`，脚本被杀后连事件证据都拿不到。
- **`--bridge-origin` 必须给且与 Host 同源**：bridge 用 `x/net/websocket` 的 `Handler`，
  其默认 `checkOrigin` 对**缺失 Origin** 的握手直接 403（日志只有 `bridge.WebSocket_请求 origin=`），
  部署侧 `HCI_BRIDGE_ALLOWED_ORIGINS=same-origin`，故 Origin 的 host:port 需等于请求 Host。

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
- **hci-sim 的 fixture 命令路由是 argv 精确匹配**，SOP 正文命令与 fixture 注册命令
  差一个字面量就落空。实测 `kbd-27123`（虚拟机开机失败场景）只注册了
  `acli --formatter json task get -k 启动虚拟机 -s failed -l 1`（带 `-l 1`），
  而 SOP 正文的命令不带 `-l 1` → `exit=127`；`ps axuf | grep ...` 则被
  `policy_denied: 禁止 shell 操作符 '|'` 拒绝。因此当前 E2E 能证明
  "命令真下发 → 真执行 → 结果回贴" 与变量契约，但要跑出 SOP 的终态结论，
  需先为 vm 场景补齐 fixture 路由（不要在 SOP 侧加 `-l 1` 迁就 fixture：
  正文明确要求多任务时用 `user_confirm` 让用户挑，限制条数会改变业务语义）。
- 直连绑定 SOP 成功后 `sop_document_hit_id` 直接取绑定值。原实现只靠
  `_scan_terminal_signals` 从终态事件反推，链路提前结束（命令全失败、缺应答停跑）
  时终态事件不产生，会把"已确定绑定的 SOP"误判成 `hit=None`（假阴性）。
