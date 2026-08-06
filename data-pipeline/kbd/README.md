# KBD 知识生产管道使用手册

本文是 Support 案例转 KBD Proposal 的权威操作手册。它以当前代码为准，覆盖抓取、入库、截图识别、分类、关键信号抽取、日志信号审计、专家交接、重跑和故障处理。

上层目录和 `scripts` 的责任边界见 [Data Pipeline 总览](../README.md)。

## 1. 目标与非目标

KBD Pipeline 的目标不是把人类案例原文简单搬进数据库，而是建立一条可追溯的知识生产链：

1. 保留 Support 原始案例和图片，确保能追溯来源。
2. 将案例转换为稳定的章节化知识，而不是依赖页面样式。
3. 从截图中提取可观察事实，并把模型推断与事实分开。
4. 生成分类和 `signals_json` Proposal，让 Agent 能获取、执行和判定案例中的隐性知识。
5. 在专家复核前自动发现运行时不可执行的日志信号。

它不负责：

- 代替专家确认故障语义和解决方案；
- 自动批准或发布 KBD；
- 在客户环境执行 qkv/qfk；
- 用审计通过证明案例一定适用于客户现场。

## 2. 当前六阶段 DAG

```text
                    ┌→ vision ────────┐
fetch → import ─────┤                  ├→ extract-signals → audit-log-signals
                    └→ classify ──────┘
```

实际拓扑顺序是：

```text
FETCH → IMPORT → {VISION ∥ CLASSIFY} → EXTRACT_SIGNALS → AUDIT_LOG_SIGNALS
```

`VISION` 和 `CLASSIFY` 在 Import 成功后并行：两者没有技术依赖，分类使用结构化的
`title/problem_description/alert_info`，不从展示用 `content_md` 反向解析。业务上截图是
信号抽取的重要事实源，所以 `EXTRACT_SIGNALS` 对 **Vision 成功** 和 **分类成功** 都是硬依赖；
识图失败、部分识图或需复核的 KBD 不会降级进入 Signal 抽取，而会明确显示为前置阻断。

`pipeline --stages` 会自动补齐前置依赖。例如 `--stages audit-log-signals` 会展开成完整六阶段链路；如果只想审计数据库现状且绝不触发前置生产，请使用独立的 `audit-log-signals` 子命令。

| Stage | 输入 | 主要输出 | 是否写状态 |
|---|---|---|---|
| 1 `fetch` | support_id、Portal Cookie | `cache/{id}/raw.json`、原图 | 写本地缓存 |
| 2 `import` | raw.json、原图 | `kbd_entry`、`kbd_image`，状态为 draft | 写 KB Service/DB |
| 3 `vision` | `kbd_image`、图片上下文 | `images_json[].evidence/desc`、重建 `content_md` | 写 KB Service/DB |
| 4 `classify` | IMPORT 写入的结构化案例文本 | `ai_category_*` | 写 DB/API |
| 5 `extract-signals` | Vision 成功、已分类 KBD、截图 Evidence、文本 | Signal v2 `signals_json` Proposal | 写 KB Service/DB |
| 6 `audit-log-signals` | `signals_json` | qfk_log 只读审计报告 | 不写数据库 |

## 3. 代码与数据责任

| 路径 | 职责 |
|---|---|
| `run.py` | 统一 CLI、参数解析、单阶段调度 |
| `pipeline.py` | 六阶段 DAG、批量编排、进度统计 |
| `fetcher.py` | Portal API 调用、原始响应和图片缓存 |
| `converter.py` | HTML 到章节化语义字段、图片占位符和上下文 |
| `importer.py` | 调用 `/api/kbd/ingest` 原子写入 KBD 与图片 |
| `image_proc.py` | 提交/轮询 Vision 任务，不在本地重复实现 LLM |
| `classifier.py` | 批量分类调度 |
| `extract_signals.py` | 提交/轮询关键信号抽取任务 |
| `log_signal_audit.py` | qfk_log Schema/Catalog/parser/predicate 只读审计领域逻辑 |
| `config.py` | `.env` 和环境变量配置 |
| `cache/` | 按 support_id 保存可追溯原始材料，不是权威发布数据 |
| `logs/` | run 日志和 progress 可观测性文件，不是真相之源 |

数据库是阶段完成状态的真相之源。`progress_*.json` 用于观察运行过程，不应被当作业务锁或发布状态。

日志信号审计已完整迁入本目录：领域规则只能在 `log_signal_audit.py` 维护，CLI 只能通过 `kbd.run audit-log-signals` 调用。`scripts/verify` 不再保留同名脚本，避免形成双入口和规则漂移。

## 4. 环境准备

### 4.1 安装依赖

在仓库根目录执行：

```bash
uv sync
```

### 4.2 配置

```bash
cp data-pipeline/kbd/.env.example data-pipeline/kbd/.env
```

至少确认以下变量：

| 变量 | 哪些阶段需要 | 说明 |
|---|---|---|
| `SANGFOR_COOKIE` | fetch | Support Portal 登录 Cookie；过期后需更新 |
| `SANGFOR_API_BASE` | fetch | 默认 `https://support.sangfor.com.cn` |
| `DATABASE_URL` | import 后各阶段、DB 审计 | asyncpg 可连接的 PostgreSQL DSN |
| `KB_SERVICE_URL` | import、vision、extract | KB Service 地址 |
| `INTERNAL_API_TOKEN` | import、vision、classify、extract | 与 KB Service 一致的内部 Token |
| `EXCEL_FILE` | `--excel` | 第一列为案例 ID 的 Excel 路径 |
| `KBD_CACHE_DIR` | fetch/import | 默认 `data-pipeline/kbd/cache` |
| `KBD_LOGS_DIR` | 所有 CLI 命令 | 默认 `data-pipeline/kbd/logs` |
| `VISION_CONCURRENCY` | vision | Vision 并发，受模型限流约束 |
| `CLASSIFY_CONCURRENCY` | classify | Pipeline 分类 API 调用并发，默认 2 |
| `EXTRACT_CONCURRENCY` | extract | Signal 抽取并发，默认 3 |

`.env` 含 Cookie、Token 和数据库地址，禁止提交 Git。不要在工单、文档或测试输出中粘贴真实值。

### 4.3 统一命令前缀

本文命令都从仓库根目录执行：

```bash
uv run python -m data-pipeline.kbd.run <command>
```

标准入口在包加载时自动定位同一 checkout 的 `backend/shared`；包含 Stage 6 的命令会在执行任何生产阶段前预检这份共享契约。这样不会在 fetch/import/Vision/LLM 已执行后，才因 `No module named shared` 失败。

`PYTHONPATH=data-pipeline:backend uv run python -m kbd.run <command>` 保留为兼容旧自动化的入口，但新的手工操作、文档和故障排查均以本节标准入口为准。

先做健康检查：

```bash
uv run python -m data-pipeline.kbd.run config
uv run python -m data-pipeline.kbd.run --help
```

`config` 会遮蔽 Cookie、Token 和数据库敏感值。

## 5. 快速开始

对单条案例运行完整生产闭环：

```bash
uv run python -m data-pipeline.kbd.run pipeline --ids 37150
```

人工批量操作可使用中文向导：

```bash
uv run python -m data-pipeline.kbd.run wizard
```

向导只在交互终端执行，并要求输入“是”确认；自动化/CI 请继续使用 `pipeline --json`。每次运行同时生成
人类可读文本日志、`progress_<run_id>.json` 和可检索的 `kbd_<run_id>.jsonl`；后者包含 trace、KBD、阶段、
Job 与错误码关联字段。

批量指定 ID：

```bash
uv run python -m data-pipeline.kbd.run pipeline \
  --ids 37150,39436,41818
```

从文本文件读取，每行一个 support_id：

```bash
uv run python -m data-pipeline.kbd.run pipeline \
  --id-file /path/to/kbd_ids.txt
```

从配置的 Excel 读取并先试跑 10 条：

```bash
uv run python -m data-pipeline.kbd.run pipeline \
  --excel \
  --limit 10
```

完整 Pipeline 默认包含 Stage 5 抽取和 Stage 6 日志契约审计。完成不等于发布；下一步是在 Admin UI 对原文、截图、分类、关键信号、排查步骤和解决方案做专家复核。

## 6. 输入选择

大多数生产子命令支持三个互斥来源：

```text
--ids 37150,41818
--id-file /path/to/ids.txt
--excel [--limit N]
```

未提供来源时命令会明确报错，不会默认全量操作。

`audit-log-signals` 额外支持：

```text
--all             数据库全量只读审计
--file FILE       审计已导出的 JSON 数组
--stdin           从标准输入读取 JSON 数组
```

## 7. 六阶段详细语义

### 7.1 Stage 1：fetch

```bash
uv run python -m data-pipeline.kbd.run fetch --ids 37150
```

产物：

```text
data-pipeline/kbd/cache/37150/
├── raw.json
├── img_0.<ext>
├── img_1.<ext>
└── 下载/锁相关标记
```

默认存在有效 `raw.json` 时跳过。需要重新获取源案例时显式使用：

```bash
uv run python -m data-pipeline.kbd.run fetch \
  --ids 37150 \
  --force
```

`--force` 只影响抓取缓存，不会自动覆盖数据库内容。

### 7.2 Stage 2：import

```bash
uv run python -m data-pipeline.kbd.run import --ids 37150
```

处理内容：

1. 从 raw.json 提取问题描述、告警信息、有效排查步骤、根因、解决方案、操作影响、临时方案和建议总结。
2. 去除页面装饰样式，保留段落、列表、表格键值和 `![img:N]` 语义位置。
3. 为图片生成 `section/context_before/context_after`。
4. 经 KB Service 同一事务写入 `kbd_entry` 与 `kbd_image`。
5. 由后端 `rebuild_content_md()` 统一渲染展示/Agent Markdown。

Pipeline 不再依赖 `.desc.txt`，也不在本地独立拼装最终 `content_md`。

覆盖 draft：

```bash
uv run python -m data-pipeline.kbd.run import \
  --ids 37150 \
  --override
```

覆盖 published 风险更高，只有明确需要重建且已备份/理解专家修改影响时才使用：

```bash
uv run python -m data-pipeline.kbd.run import \
  --ids 37150 \
  --override \
  --override-status all
```

### 7.3 Stage 3：vision

```bash
uv run python -m data-pipeline.kbd.run vision --ids 37150
```

Vision 从数据库 `kbd_image` 读取原图，结合章节和前后文，调用 KB Service 异步任务并轮询。结果写入 `images_json`，核心原则是分离：

- Observed Facts：OCR 文字、可见控件、状态、错误码、时间等；
- Inferences：截图类型和语义解释等模型判断；
- Quality/Provenance：质量、需复核原因、图片哈希和模型来源。

一张截图可对应 `qkv_alert`、`qkv_task`、`qkv_dialog` 等信号候选，但截图分类本身不是可执行信号；Stage 5 还必须结合文本、Evidence 和工具能力生成 Schema 合法的 Proposal。

仅重试自动可恢复的失败图片：

```bash
uv run python -m data-pipeline.kbd.run vision \
  --ids 37150 \
  --failed-only
```

`failed-only` 会重试空描述、缺 Evidence、`failed/partial/low_quality/needs_review` 等可重试状态。已成功抽取观察事实但仍需人判断的图片，不应被无限重跑来碰运气。

### 7.4 Stage 4：classify

```bash
uv run python -m data-pipeline.kbd.run classify --ids 37150
```

仅处理 draft 且尚无 AI 分类的条目，写入：

- `ai_category_id`
- `ai_category_conf`
- `ai_category_reason`

这些字段是 LLM Proposal。专家可在 Admin UI 修改最终分类；发布和 Agent 使用以审核后的稳定内容为准。

### 7.5 Stage 5：extract-signals

关键信号抽取现在是 `kbd.run` 的一等子命令：

```bash
uv run python -m data-pipeline.kbd.run extract-signals \
  --ids 37150,41818
```

兼容别名：

```bash
uv run python -m data-pipeline.kbd.run extract --ids 37150
```

独立命令只处理同时满足以下条件的条目：

1. `status = draft`；
2. 已有人工分类或 AI 分类；
3. `signals_json` 为 `NULL` 或空数组。

这三个前置条件避免离开分类上下文抽取，也避免批处理静默覆盖专家已编辑的稳定 Proposal。需要重新生成已有信号时，应在 Admin 审核语境中明确操作和比较差异，而不是通过默认批处理覆盖。

工作方式：

```text
data-pipeline
  POST /api/admin/kbd/{kbd_entry_id}/extract-signals
  ← 202 + job_id
  GET  /api/admin/kbd/{kbd_entry_id}/extract-signals/status?job_id=...
  ← done/failed

kb-service
  读取文本 + 图片 Evidence + 分类 + 工具 Schema
  → LLM 生成候选
  → Schema/能力门禁
  → 写 signals_json + rejected_candidates + verification_contract
```

结果统计：

| 字段 | 含义 |
|---|---|
| `done` | 产生至少一个活动可执行信号 |
| `needs_review` | LLM 任务完成，但没有活动可执行 Proposal；不能冒充完成 |
| `failed` | API、任务或处理失败 |
| `skipped` | support_id 不存在等批处理跳过 |
| `skipped_by_precondition` | 非 draft、未分类或已有 signals_json，被 CLI 保护性跳过 |

也可以在 Pipeline 中显式运行到抽取：

```bash
uv run python -m data-pipeline.kbd.run pipeline \
  --ids 37150 \
  --stages extract-signals
```

注意：Pipeline DAG 会自动补齐 Stage 1–4；独立 `extract-signals` 不补跑前置阶段，只处理数据库中已经准备好的条目。

### 7.6 Stage 6：audit-log-signals

这是日志关键信号的生产后质量检查，直接复用 Agent 运行时使用的：

- qfk_log acquire 参数 Schema；
- HCI 日志源 Catalog；
- 文件名/路径规范化；
- parser 与 predicate 支持关系；
- producer 的有界返回约束。

它不会写数据库、修复 Proposal 或发布 KBD。

审计指定数据库案例：

```bash
uv run python -m data-pipeline.kbd.run audit-log-signals \
  --ids 37150,41818
```

数据库全量审计并把完整报告归档：

```bash
uv run python -m data-pipeline.kbd.run audit-log-signals \
  --all \
  --output /tmp/kbd-log-signal-audit.json
```

审计文件输入：

```bash
uv run python -m data-pipeline.kbd.run audit-log-signals \
  --file /tmp/kbd-signals.json \
  --output /tmp/kbd-log-signal-audit.json
```

审计 stdin：

```bash
kubectl exec -n hci-dev <postgres-pod> -- psql -U <user> -d <db> -Atc \
  "SELECT jsonb_agg(jsonb_build_object('support_id', support_id, 'signals_json', signals_json)) FROM kbd_entry" \
  | uv run python -m data-pipeline.kbd.run audit-log-signals --stdin
```

输入必须是 JSON 数组：

```json
[
  {
    "support_id": "37150",
    "signals_json": {
      "schema_version": 2,
      "signals": []
    }
  }
]
```

审计状态：

| 状态 | 精确含义 | 后续动作 |
|---|---|---|
| `PASS_LOG_CONTRACT` | 活动 qfk_log 能通过当前构建契约 | 继续做语义和现场可复现性复核 |
| `BLOCKED_ACTIVE_SIGNAL` | 活动 Proposal 有不可执行日志信号 | 发布前修复或明确 Capability Gap |
| `NEEDS_EXPERT_REVIEW` | 活动日志可构建，但有被门禁拒绝的日志候选 | 判断是漏信号还是外部/未支持数据源 |
| `NO_ACTIVE_LOG_SIGNAL` | 当前无活动 qfk_log | 不代表原案例没有日志语义，需结合案例复核 |

常见 issue code：

| code | 含义 |
|---|---|
| `MISSING_FILE` | qfk_log 未指定可解析文件 |
| `INVALID_TIME_OR_PATH` | 时间窗口或路径不符合当前契约 |
| `UNSUPPORTED_PREDICATE` | 日志 parser 不支持该 matcher 类型 |
| `UNBOUNDED_PRODUCER` | 变量产出缺少关键字/request_id 边界，可能回传整文件 |
| `CAPABILITY_GAP` | 数据源不属于当前 qfk_log 能力，例如外部 BMC 日志 |
| `REJECTED_LOG_CANDIDATE` | 抽取候选被生产门禁拒绝，需专家判断 |

默认情况下，发现 Proposal 问题仍返回 0，因为报告本身是专家复核清单。用于 CI 时显式开启严格门禁：

```bash
uv run python -m data-pipeline.kbd.run audit-log-signals \
  --file /tmp/kbd-signals.json \
  --fail-on-blocked
```

只有存在 `BLOCKED_ACTIVE_SIGNAL` 才返回 1；JSON 解析失败、文件不可读等审计器自身错误会自然返回非零。

## 8. Pipeline 的部分运行、续跑与覆盖

指定阶段：

```bash
uv run python -m data-pipeline.kbd.run pipeline \
  --ids 37150 \
  --stages fetch,import,vision
```

支持名称 `fetch,import,vision,classify,extract-signals,audit-log-signals`，也兼容数字 `1` 到 `6`。

从数据库现状续跑：

```bash
uv run python -m data-pipeline.kbd.run pipeline \
  --ids 37150 \
  --resume
```

`--resume` 以数据库状态为阶段完成依据；progress 文件只是运行记录。`--resume-run-id` 用于沿用日志 run_id，不应改变业务正确性。

仅处理自动识别出的抓取/Vision 失败案例：

```bash
uv run python -m data-pipeline.kbd.run pipeline \
  --excel \
  --failed-only
```

强制重新抓取并覆盖允许状态的导入记录：

```bash
uv run python -m data-pipeline.kbd.run pipeline \
  --ids 37150 \
  --force-fetch \
  --override
```

不要把 `--force-fetch`、`--override` 和“重新抽取已有专家信号”混为一谈。前两者只控制源缓存和 Import；Stage 5 仍保护非空 `signals_json`。

## 9. 数据模型和发布边界

### 9.1 章节字段

`kbd_entry` 保存问题描述、告警、排查步骤、根因、解决方案、操作影响、临时方案和建议总结。章节字段是可编辑知识源，`content_md` 是后端统一生成的 projection，不应由多个生产端分别维护。

### 9.2 images_json 与 kbd_image

- `kbd_image` 保存原始图片字节，用于复核和重新识图；
- `images_json` 保存图片序号、章节、上下文、兼容描述和结构化 Evidence；
- `![img:N]` 在章节字段中保留图片语义位置；
- 后端渲染时对未验证推断做隔离，不应把模型猜测伪装成截图事实。

### 9.3 signals_json

`signals_json` 是 Agent 可执行/判定知识的权威结构，典型内容包括：

- 活动 `signals`；
- 被门禁拒绝但保留供专家判断的 `rejected_candidates`；
- 整案例的 `verification_contract`；
- Schema/提取版本和 provenance。

它不是案例原文的摘要。每个信号必须回答：如何获取、如何解析、如何判定、需要哪些变量、产生哪些变量，以及能力不支持时如何显式暴露。

### 9.4 状态和 Agent 可见性

典型生命周期：

```text
draft → published → archived
  └──→ rejected
```

自动 Pipeline 生产 draft Proposal；专家修改并验证；发布后 Agent 才消费稳定版本。日志审计通过不能自动触发 published。

分类、全量/单图识别和关键信号抽取产生的每次 AI 结果，均由 kb-service 追加到同一
`kbd_revision` Proposal 历史；Pipeline 不得绕过服务端直接更新分类结果。专家对分类、
图片 Evidence、章节或 Signal 的保存继续形成同一 payload schema 的 Expert Revision，
并显式绑定所审核的 Proposal。`evaluation-export` 据此输出 Proposal→Expert 字段级 Diff，
用于按错误类型持续改进分类 Prompt、Vision Prompt、Signal Prompt、Schema 和工具能力；
中间工作稿及未满足事实边界的数据不能直接当作 Expert Gold 或训练目标。

## 10. 专家复核交接清单

自动链路完成后，专家至少确认：

1. 原始案例标题、问题描述和适用版本是否准确。
2. 每张截图类型、OCR、错误码、时间、主机/VM/任务/告警身份是否正确。
3. AI 分类是否落在正确产品和故障域。
4. 关键信号是否覆盖案例成立条件、排除条件、根因证据和解决后验证。
5. qkv/qfk 的参数能否在真实 HCI 环境获得，变量依赖是否闭合。
6. qfk_log 文件、HOST/VM/END/request_id、parser、predicate 是否正确。
7. 排查动作的风险、权限、作用对象和预期返回是否清楚。
8. 解决方案是否可执行，是否遗漏影响、回滚或临时性说明。
9. 在审核页编辑后是否即时保存，并用当前工具能力验证。
10. 发布版本是否以专家修改结果为准，同时保留 LLM Proposal 供后续评估。

## 11. 日志、结果与可观测性

每次非 `config` CLI 调用会生成：

```text
data-pipeline/kbd/logs/kbd_YYYYMMDD_HHMMSS.log
```

Pipeline 还生成：

```text
data-pipeline/kbd/logs/progress_YYYYMMDD_HHMMSS.json
```

日志包含 run_id 和 trace_id，可用于串联 data-pipeline 与 KB Service。查看最新日志：

```bash
ls -1t data-pipeline/kbd/logs/kbd_*.log | head -1
```

不要用 progress 文件手工改业务状态，也不要把日志中的“HTTP 成功”解释成“有可执行信号”。Stage 5 的 `needs_review` 专门区分这两者。

## 12. 常见问题

### 12.1 `No module named kbd`

从仓库根目录使用：

```bash
uv run python -m data-pipeline.kbd.run --help
```

### 12.2 `No module named shared`

标准入口会自动补上当前 checkout 的 `backend`。如果仍报错，说明 checkout 缺少或损坏 `backend/shared`，先执行 `uv sync`，再确认在仓库根目录存在 `backend/shared/schemas/`；不要通过跳过 Stage 6 或重跑前五阶段绕过这个错误。

### 12.3 Portal 401/返回登录页

更新 `data-pipeline/kbd/.env` 中的 `SANGFOR_COOKIE`。Cookie 是短期凭证，不要把用户提供的真实 Header 归档到仓库。

### 12.4 没有已准备好可导入的案例

确认 `cache/{support_id}/raw.json` 存在且有效；先单独运行 fetch，查看同一 run_id 日志。

### 12.5 Vision 429 或超时

降低 `VISION_CONCURRENCY`，等待配额恢复后使用 `vision --failed-only`。不要靠无界重试提高“成功率”。

### 12.6 `extract-signals` 显示没有可抽取案例

检查三项：是否 draft、是否已分类、`signals_json` 是否为空。这通常是保护性跳过，而不是程序故障。

### 12.7 抽取成功但 `done=0`

如果 `needs_review>0`，说明 LLM 流程完成但没有通过门禁的活动信号。查看 `rejected_candidates` 和 Admin 审核页，不要把空壳当完成。

### 12.8 `NO_ACTIVE_LOG_SIGNAL` 是否代表没有问题

不代表。它只说明当前 Proposal 没有活动 qfk_log，可能是案例本来不依赖日志，也可能是信号覆盖不足，必须结合原案例判断。

### 12.9 审计通过是否可以自动发布

不可以。`PASS_LOG_CONTRACT` 只证明当前日志信号能按运行时契约构建，不证明文件选对、关键字选对、阈值合理、故障已复现或解决方案正确。

## 13. 测试与变更要求

运行 KBD 单元测试：

```bash
PYTHONPATH=data-pipeline:backend uv run pytest -q \
  tests/unit/kbd
```

静态检查：

```bash
PYTHONPATH=data-pipeline:backend uv run ruff check \
  data-pipeline/kbd \
  tests/unit/kbd
git diff --check
```

验证 README 中的统一入口：

```bash
uv run python -m data-pipeline.kbd.run --help
uv run python -m data-pipeline.kbd.run pipeline --help
uv run python -m data-pipeline.kbd.run extract-signals --help
uv run python -m data-pipeline.kbd.run audit-log-signals --help
```

修改任一 Stage 时，测试必须覆盖输入、前置条件、成功、失败、幂等/保护性跳过和输出统计。修改 Signal 或 qfk_log 契约时，生产端审计与 Agent 运行时必须复用同一 Schema/Catalog，禁止各自维护一份近似规则。

## 14. 相关设计文档

- [知识库总体设计](../../docs/solution/knowledge-base/知识库设计.md)
- [关键信号字段级别抽取](../../docs/solution/agent/02-架构设计/关键信号字段级分别抽取.md)
- [qfk_log 统一日志采集、解析与判定设计](../../docs/solution/agent/02-架构设计/qfk_log统一日志采集解析与判定设计.md)
- [HCI 底层目录、日志、容器与 aCLI 知识基线](../../docs/solution/agent/02-架构设计/HCI底层目录日志容器与aCLI知识基线.md)

最后更新：2026-07-30。
