# KBD 知识生产管道使用手册

本文是 Support 案例转 KBD Proposal 的权威操作手册。它以当前代码为准，覆盖环境准备、抓取、入库、截图识别、分类、关键信号抽取、全量 Signal 审查、专家交接、重跑和故障处理。

> 目录名在 Linux/macOS 上区分大小写，实际路径是 `data-pipeline/kbd/`（小写 `kbd`）。本文所有命令均从仓库根目录执行。

上层目录和 `scripts` 的责任边界见 [Data Pipeline 总览](../README.md)。

## 现行 CLI：统一任务管理模型（2026-08-09）

旧的 `pipeline` 入口以及 `--force-fetch`、`--force`、`--override`、
`--override-status`、`--failed-only`、`--resume-run-id` 已删除，不再兼容。当前唯一生产
任务入口是 `task`；`cli` 是同一任务入口的交互式前端，六个 Stage 名称是便捷别名：

```bash
uv run python -m data-pipeline.kbd.run task --ids 29351 --stages vision
```

所有阶段共享同一组参数：`--excel` / `--ids` / `--id-file` / `--run-id`（任务范围）、
`--stages`（默认 `all`）、`--resume`、`--failed`、`--rework[=STATUS_LIST]`。
三种显式模式严格互斥：无模式参数选择“未执行 + 失败”，`--resume` 仅未执行，`--failed`
仅失败，`--rework` 重做用户显式指定的阶段且默认只处理 draft，支持 `draft,published,rejected,archived` 多选。自动补齐的前置阶段只有在未成功、会阻断目标阶段时才补做；已成功的前置阶段不会重做。`--run-id` 只读取历史执行的不可变
manifest，代表任务范围；本次运行仍生成独立 `execution_id`。指定阶段会按 DAG 自动补齐未满足
的前置依赖，执行前日志会打印请求阶段、补齐阶段和每阶段选中任务数。published 重做只能
创建 maintenance working revision，不能原地覆盖生效版本；Vision 还按图片 `seq` 记录子任务状态。

## 1. 目标与非目标

KBD Pipeline 的目标不是把人类案例原文简单搬进数据库，而是建立一条可追溯的知识生产链：

1. 保留 Support 原始案例和图片，确保能追溯来源。
2. 将案例转换为稳定的章节化知识，而不是依赖页面样式。
3. 从截图中提取可观察事实，并把模型推断与事实分开。
4. 生成分类和 `signals_json` Proposal，让 Agent 能获取、执行和判定案例中的隐性知识。
5. 在专家复核和发布前，使用 Agent 最终执行所用的 Shared Resolution Runtime 审查全部 Signal。

它不负责：

- 代替专家确认故障语义和解决方案；
- 自动批准或发布 KBD；
- 在客户环境执行 qkv/qfk；
- 用 Signal Review 通过证明案例一定适用于客户现场。

## 2. 当前六阶段 DAG

```text
                    ┌→ vision ────────┐
fetch → import ─────┤                  ├→ extract-signals → review-signals
                    └→ classify ──────┘
```

实际拓扑顺序是：

```text
FETCH → IMPORT → {CLASSIFY ∥ VISION} → EXTRACT_SIGNALS → REVIEW_SIGNALS
```

`VISION` 和 `CLASSIFY` 在 Import 成功后并行：两者没有技术依赖，分类使用结构化的
`title/problem_description/alert_info`，不从展示用 `content_md` 反向解析。业务上截图是
信号抽取的重要事实源，所以 `EXTRACT_SIGNALS` 对 **Vision 成功** 和 **分类成功** 都是硬依赖；
识图失败、部分识图或需复核的 KBD 不会降级进入 Signal 抽取，而会明确显示为前置阻断。

`task --stages` 会自动补齐前置依赖。例如 `--stages review-signals` 会展开成完整六阶段链路；如果只想审查外部 JSON 且绝不创建 KBD 任务，请使用独立的 `review-input` 子命令。

| Stage | 输入 | 主要输出 | 是否写状态 |
|---|---|---|---|
| 1 `fetch` | support_id、Portal Cookie | `cache/{id}/raw.json`、原图 | 写本地缓存 |
| 2 `import` | raw.json、原图 | `kbd_entry`、`kbd_image`，状态为 draft | 写 KB Service/DB |
| 3 `classify` | IMPORT 写入的结构化案例文本 | `ai_category_*` | 写 DB/API |
| 4 `vision` | `kbd_image`、图片上下文 | `images_json[].evidence/desc`、重建 `content_md` | 写 KB Service/DB |
| 5 `extract-signals` | Vision 成功、已分类 KBD、截图 Evidence、文本 | Signal v2 `signals_json` Proposal | 写 KB Service/DB |
| 6 `review-signals` | `signals_json` | 全部 Signal 的 Shared Resolution Runtime 审查报告 | 不写数据库 |

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
| `signal_review.py` | 全部 Signal 的 Shared Resolution Runtime 审查适配与报告 |
| `config.py` | `.env` 和环境变量配置 |
| `cache/` | 按 support_id 保存可追溯原始材料，不是权威发布数据 |
| `logs/` | run 日志和 progress 可观测性文件，不是真相之源 |

数据库是阶段完成状态的真相之源。`progress_*.json` 用于观察运行过程，不应被当作业务锁或发布状态。

全量 Signal 审查统一调用 `backend/shared/resolution/review.py`，其底层直接使用 PR704 Shared Resolution Runtime。Pipeline 只追加自己的 rejected-candidate 报告，不复制 Resolver 规则。CLI 唯一命令是 `kbd.run review-signals`，不保留旧入口。

## 4. 环境准备

### 4.0 执行前检查清单

第一次运行或切换环境时，按以下顺序检查，避免在已经调用 LLM 后才发现环境问题：

1. 当前目录是仓库根目录：`pwd` 应指向 `hci-troubleshoot-platform`。
2. `backend/shared/schemas/` 存在；Stage 6 会依赖这份共享运行时契约。
3. Python 依赖可解析：先执行 `uv sync`，再执行 `config` 和 `--help`。
4. `.env` 中的 Cookie、数据库 DSN、KB Service 地址和内部 Token 均属于同一个环境。
5. 先用 1 条 KBD 做冒烟，再扩大到 5 条或更大的批量。

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
| `DATABASE_URL` | import 后各阶段、DB Signal Review | asyncpg 可连接的 PostgreSQL DSN |
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

### 4.4 当前版本的验证边界

部署验证已经确认 ArgoCD、kb-service 健康检查、数据库迁移和服务端 Vision Job 可以工作；但 5 条 KBD 的完整链路验证发现，Pipeline 使用 `asyncpg` 读取 PostgreSQL `jsonb` 时没有统一反序列化，可能把服务端已经完成的 Vision Job 误判为失败，继而阻断 `extract-signals`。因此在该缺陷修复并回归验证前：

- 不要把完整 `pipeline` 的失败简单解释为所有图片识别失败；
- 不要为了绕过阻断而直接批量执行 `extract-signals`；
- 可以使用 `fetch`、`import`、`vision`、`classify` 和只读 `review-signals` 做分阶段排障；
- 修复后应重新执行“1 条冒烟 → 5 条验证 → 正常批量”的成功路径。

## 5. 快速开始

对单条案例运行完整生产闭环：

```bash
uv run python -m data-pipeline.kbd.run task --ids 37150
```

人工批量操作可使用交互式 CLI：

```bash
uv run python -m data-pipeline.kbd.run cli
```

CLI 只在交互终端执行，交互顺序为：任务范围（手动 ID、历史 run-id、ID 文件、Excel，默认手动 ID）→阶段→执行模式→重做状态（仅 rework）→limit→任务计划预览→最终确认。它只收集统一 `task` 参数，不再询问 force、override 或 failed-only。

四种模式为：默认“未执行 + 失败”、`--resume`“仅未执行”、`--failed`“仅失败”、`--rework`“重做用户指定阶段”。重做状态支持 `draft`、`published`、`rejected`、`archived`，可多选；published 只能进入 maintenance working revision。选择具体阶段时，DAG 仍会解析前置阶段，但 rework 只将确实阻断目标阶段的未成功前置任务加入计划，并在 CLI 计划和日志中明确列出。历史 run-id 从 `logs/task-manifests/` 最近任务列表选择，也可以手动输入。执行后的日志、JSONL 和 KBD 完成摘要与非交互入口完全相同。

批量指定 ID：

```bash
uv run python -m data-pipeline.kbd.run task \
  --ids 37150,39436,41818
```

从文本文件读取，每行一个 support_id：

```bash
uv run python -m data-pipeline.kbd.run task \
  --id-file /path/to/kbd_ids.txt
```

从配置的 Excel 读取并先试跑 10 条：

```bash
uv run python -m data-pipeline.kbd.run task \
  --excel \
  --limit 10
```

完整 Pipeline 默认包含 Stage 5 抽取和 Stage 6 全量 Signal Review。完成不等于发布；下一步是在 Admin UI 对原文、截图、分类、关键信号、排查步骤和解决方案做专家复核。

### 5.1 推荐人工 SOP：一条案例到待审核 Proposal

适合知识运营、支持专家或研发人工处理单条/少量案例。需要重建历史内容时使用统一的 `--rework` 模式，不再使用已删除的 `--force-fetch` 或 `--override`。

```bash
# 1. 检查当前环境，输出会遮蔽敏感配置
uv run python -m data-pipeline.kbd.run config

# 2. 对 1 条案例执行完整 DAG
uv run python -m data-pipeline.kbd.run task --ids 37150

# 3. 根据终端打印的 run_id 查看进度与可检索排障日志
ls -1t data-pipeline/kbd/logs/progress_*.json | head -1
ls -1t data-pipeline/kbd/logs/kbd_*.jsonl | head -1

# 4. 完成后进入 Admin UI 做专家审核；不要由 CLI 自动发布
```

终端摘要必须同时满足以下条件，才能把该条目交给专家审核：

| 检查项 | 期望结果 | 不满足时的动作 |
|---|---|---|
| Import | `created/overridden/skipped`，且无 error | 修复源数据或导入权限后重跑 |
| Vision | KBD 状态 `done`，没有 `failed/needs_review` | 修复图片、Provider 或识别质量后重试 Vision |
| Classify | 已存在 AI 分类或人工分类 | 核对分类失败原因，必要时在审核页补充 |
| Extract | `done`，存在活动可执行 Signal | 查看 `needs_review` / `rejected_candidates`，不得把空结果当成功 |
| Signal Review | 无 `BLOCKED_SIGNAL_REVIEW` | 修复 Resolver、命令/日志 Catalog、参数或明确 Capability Gap |

`PASS_SIGNAL_REVIEW` 仅表示活动 Signal 通过 Shared Resolution Runtime 的静态审查；`NEEDS_SIGNAL_REVIEW` 表示仍需现场 probe 或存在被拒候选。两者都不表示故障语义、根因或解决方案已被专家确认。

### 5.2 推荐批量 SOP：先小批量，再逐步放量

```bash
# 5 条验证：推荐的第一批量级
uv run python -m data-pipeline.kbd.run task \
  --id-file /path/to/kbd_ids.txt \
  --json

# Excel 来源先限制 10 条；观察 Provider、失败率和总耗时后再扩大
uv run python -m data-pipeline.kbd.run task \
  --excel \
  --limit 10 \
  --json
```

批处理结束后，按 `run_id` 汇总，而不要只看命令行最后一条 HTTP 日志：

```text
完成 KBD 数 / 总 KBD 数
各 Stage 的 failed、needs_review、blocked_by_dependency
Vision 的图片数、KBD 状态数和耗时
失败 KBD 的 support_id、job_id、error_code、是否可重试
Stage 6 的 BLOCKED_SIGNAL_REVIEW 与 NEEDS_SIGNAL_REVIEW
```

CI/自动化应使用 `--json`，并以 `pipeline.success`、`completed_ids`、`failed_steps` 和退出码判断是否通过；不得仅以命令是否发出 HTTP 请求判断成功。

### 5.3 覆盖、重跑和发布的安全边界

| 需求 | 推荐命令 | 风险与约束 |
|---|---|---|
| 正常补齐未完成和失败 | `task --ids 37150` | 默认模式；按每个 Stage 独立选择 |
| 只跑未完成 | `task --ids 37150 --resume` | 不重试已失败任务 |
| 只重试失败 | `task --ids 37150 --failed` | 不选择未执行任务 |
| 重做 draft | `task --ids 37150 --rework` | 明确允许已成功任务再次调用 |
| 重做多个状态 | `task --ids 37150 --rework=draft,published,rejected,archived` | published 使用 maintenance working revision |
| Vision 技术失败重试 | `task --ids 37150 --stages vision --failed` | 先确认 Provider/网络恢复，不做无界重试 |
| 从历史任务范围执行 | `task --run-id 20260809_103000 --failed` | run-id 读取不可变 manifest |
| 重新抽取已有 Signal | 不使用默认批处理覆盖 | 应在 Admin 审核语境中比较 Proposal 与专家修改 |
| 发布 KBD | Admin UI 专家审核后发布 | CLI 和 Signal Review 都不能自动发布 |

## 6. 输入选择

大多数生产子命令支持三个互斥来源：

```text
--ids 37150,41818
--id-file /path/to/ids.txt
--excel [--limit N]
```

未提供来源时命令会明确报错，不会默认全量操作。

独立的 `review-input` 支持：

```text
--file FILE       审查已导出的 JSON 数组
--stdin           从标准输入读取 JSON 数组
--output FILE     写出审查报告
--fail-on-blocked BLOCKED 时返回 1
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

默认存在有效 `raw.json` 时跳过。需要重新获取源案例时使用统一重做模式：

```bash
uv run python -m data-pipeline.kbd.run fetch \
  --ids 37150 \
  --rework
```

`fetch --rework` 会重新抓取源案例；它只重做 Fetch Stage（抓取阶段），不会自动覆盖数据库中的 KBD。

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

重做 draft：

```bash
uv run python -m data-pipeline.kbd.run task \
  --ids 37150 \
  --rework
```

重做 published 必须进入 maintenance working revision，并重新经过专家审查：

```bash
uv run python -m data-pipeline.kbd.run task \
  --ids 37150 \
  --rework=draft,published
```

### 7.3 Stage 3：classify

```bash
uv run python -m data-pipeline.kbd.run classify --ids 37150
```

仅处理 draft 且尚无 AI 分类的条目，写入：

- `ai_category_id`
- `ai_category_conf`
- `ai_category_reason`

这些字段是 LLM Proposal。专家可在 Admin UI 修改最终分类；发布和 Agent 使用以审核后的稳定内容为准。

### 7.4 Stage 4：vision

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
  --failed
```

`--failed` 会按 Vision 任务账本选择失败图片/案例。已成功抽取观察事实但仍需人判断的图片，不应被无限重跑来碰运气。

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
uv run python -m data-pipeline.kbd.run task \
  --ids 37150 \
  --stages extract-signals
```

注意：Pipeline DAG 会自动补齐 Stage 1–4；独立 `extract-signals` 不补跑前置阶段，只处理数据库中已经准备好的条目。

### 7.6 Stage 6：review-signals

这是全部关键信号的生产后质量检查，直接复用 Agent 执行时使用的 Shared Resolution Runtime：

- qfk/qkv acquire 参数 Schema；
- 各领域 Resolver 和命令/日志 Catalog；
- 文件名、路径和命令规范化；
- parser、predicate 与只读能力约束；
- producer 的有界返回约束。

它不会写数据库、修复 Proposal 或发布 KBD。

审查指定数据库案例：

```bash
uv run python -m data-pipeline.kbd.run review-signals \
  --ids 37150,41818
```

数据库全量审查并把完整报告归档：

```bash
uv run python -m data-pipeline.kbd.run review-signals \
  --all \
  --output /tmp/kbd-signal-review.json
```

审查文件输入：

```bash
uv run python -m data-pipeline.kbd.run review-signals \
  --file /tmp/kbd-signals.json \
  --output /tmp/kbd-signal-review.json
```

审查 stdin：

```bash
kubectl exec -n hci-dev <postgres-pod> -- psql -U <user> -d <db> -Atc \
  "SELECT jsonb_agg(jsonb_build_object('support_id', support_id, 'signals_json', signals_json)) FROM kbd_entry" \
  | uv run python -m data-pipeline.kbd.run review-signals --stdin
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

Signal Review 状态：

| 状态 | 精确含义 | 后续动作 |
|---|---|---|
| `PASS_SIGNAL_REVIEW` | 全部活动 Signal 通过 Shared Resolution Runtime 静态审查 | 继续做语义和现场可复现性复核 |
| `BLOCKED_SIGNAL_REVIEW` | 活动 Proposal 有无法编译/解析的 Signal | 发布前修复或明确 Capability Gap |
| `NEEDS_SIGNAL_REVIEW` | 存在 needs_probe 警告或被门禁拒绝的候选 | 判断是漏信号、现场待探测还是未支持数据源 |
| `NO_ACTIVE_SIGNAL` | 当前无活动 Signal | 不代表原案例没有信号语义，需结合案例复核 |

常见 issue code：

| code | 含义 |
|---|---|
| `LOG_FILE_REQUIRED` / `LOG_CATALOG_REJECTED` | qfk_log 文件、路径或日志源不符合 Runtime Catalog |
| `SYSTEM_COMMAND_UNKNOWN` | qfk_system/domain 命令不在当前 aCLI Catalog |
| `SIGNAL_ACQUIRE_ARGS_INVALID` | acquire 参数不符合共享 Signal 契约 |
| `SIGNAL_RUNTIME_NOT_VERIFIED` | Agent 执行前要求 verified，但当前仍需现场探测 |
| `REJECTED_SIGNAL_CANDIDATE` | 抽取候选被生产门禁拒绝，需专家判断 |

默认情况下，发现 Proposal 问题仍返回 0，因为报告本身是专家复核清单。用于 CI 时显式开启严格门禁：

```bash
uv run python -m data-pipeline.kbd.run review-signals \
  --file /tmp/kbd-signals.json \
  --fail-on-blocked
```

只有存在 `BLOCKED_SIGNAL_REVIEW` 才返回 1；JSON 解析失败、文件不可读等审查器自身错误会自然返回非零。

## 8. task 的部分运行、续跑与重做

指定阶段：

```bash
uv run python -m data-pipeline.kbd.run task \
  --ids 37150 \
  --stages fetch,import,vision
```

支持名称 `fetch,import,vision,classify,extract-signals,review-signals` 和数字 `1` 到 `6`。

从数据库现状续跑：

```bash
uv run python -m data-pipeline.kbd.run task \
  --ids 37150 \
  --resume
```

`--resume` 以任务账本状态为选择依据；progress 文件只是运行记录。历史任务范围使用 `--run-id`，它读取不可变 manifest，不会沿用旧日志文件。

仅处理自动识别出的抓取/Vision 失败案例：

```bash
uv run python -m data-pipeline.kbd.run task \
  --excel \
  --failed
```

强制重新抓取并覆盖允许状态的导入记录：

```bash
uv run python -m data-pipeline.kbd.run task \
  --ids 37150 \
  --rework
```

不要把 `--rework` 与默认补齐/失败重试混为一谈。`--rework` 明确表示用户指定阶段即使成功也再次执行；自动补齐的前置阶段只有在未成功且会阻断目标阶段时才重做。状态范围可指定 `draft,published,rejected,archived`。

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

自动 Pipeline 生产 draft Proposal；专家修改并复核；发布后 Agent 才消费稳定版本。Signal Review 通过不能自动触发 published。

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

以及适合 `rg`、脚本和归档的结构化事件流：

```text
data-pipeline/kbd/logs/kbd_YYYYMMDD_HHMMSS.jsonl
```

Pipeline 还生成：

```text
data-pipeline/kbd/logs/progress_YYYYMMDD_HHMMSS.json
```

日志包含 run_id 和 trace_id，可用于串联 data-pipeline 与 KB Service。查看最新日志：

```bash
ls -1t data-pipeline/kbd/logs/kbd_*.log | head -1
```

终端摘要中的阶段状态有明确语义：`完成` 表示本阶段确实选中了任务并得到结果，
`无需执行：已有结果` 表示 KBD 仍在本次范围内且已满足本阶段条件；`无需执行：当前模式无需处理`
表示被当前生命周期模式过滤；`未安排` 表示
没有可执行候选（通常是前置阶段没有提供输入）。这两者都不能理解为“执行了但结果为 0”。摘要还会显示候选数、
选中数和前置阻断数；`done/failed/skipped` 只表示业务结果，不再承担“是否执行”的语义。

失败时不要只看最后一行。CLI 会同时打印可复制的检索命令：

```bash
# 查看该次运行的所有 ERROR/CRITICAL（JSONL 每行都是一条结构化记录）
rg -n '"level": "(ERROR|CRITICAL)"' data-pipeline/kbd/logs/kbd_<run_id>.jsonl

# 查看某个案例的全部事件（包含 job_id、error_code、error_detail、retryable）
rg -n '"support_id": "27123"' data-pipeline/kbd/logs/kbd_<run_id>.jsonl

# 查看同一 run 的完整文本日志和 DEBUG 堆栈
less -N data-pipeline/kbd/logs/kbd_<run_id>.log
```

每条文本/JSONL 记录都带 `run_id` 和 `trace_id`；调用 kb-service 的事件还带
`support_id`、`stage`、`job_id`。因此服务端日志可直接用同一个 trace_id 查询，
无需猜测应该去哪个文件。异步 Vision/Signal 任务失败时，CLI 优先展示服务端返回的
具体原因和下一步建议；只有服务端没有返回原因时才会出现“未分类异常”。

完整的字段、级别和状态语义契约见 [LOGGING.md](LOGGING.md)。

不要用 progress 文件手工改业务状态，也不要把日志中的“HTTP 成功”解释成“有可执行信号”。Stage 5 的 `needs_review` 专门区分这两者。

### 11.1 终端颜色与无颜色环境

交互终端默认启用 ANSI 颜色，便于快速区分：

| 颜色 | 含义 |
|---|---|
| 绿色 | 完成、成功、通过、ready |
| 黄色 | 需复核、跳过、重试、warning |
| 红色 | 错误、异常、超时、失败、前置阻断 |
| 青色整行 | Stage 阶段分隔线 |

阶段标题使用整行分隔格式，例如：

```text
=================================  Stage 1: 数据抓取  ==================================
```

日志文件和 JSONL 始终保持无 ANSI 控制码，适合归档、检索和机器处理。终端颜色遵循标准 `NO_COLOR` 约定，也可以显式控制：

```bash
# 强制开启（适用于某些不会正确报告 TTY 的终端；若环境已设置 NO_COLOR，先移除它）
env -u NO_COLOR KBD_COLOR=always uv run python -m data-pipeline.kbd.run cli

# 关闭颜色（适用于 CI、重定向和纯文本终端）
NO_COLOR=1 uv run python -m data-pipeline.kbd.run task --ids 37150 --json
```

### 11.2 终端排版采用的最佳实践与失败根因

本 CLI 遵循 Unix/POSIX 命令行、结构化日志和 Unicode 终端排版的通用范式：

| 范式 | 本项目的落地规则 |
|---|---|
| POSIX/Unix CLI | 多选项用编号；yes/no 使用 `y/yes/n/no`；回车采用明确默认值；`--help`、退出码和 `--json` 面向自动化保持稳定 |
| 固定宽度 Banner | 每个 Stage 与 `KBD 流水线完成摘要` 共用同一个总显示宽度（当前为 96 个显示列），标题居中，剩余列由两侧 `=` 平分；标题变长不会把右端推歪 |
| Unicode 显示宽度 | 中文、全角字符按两列计算，不能直接使用 Python `len()`；表格布局先计算可视宽度，再生成空格和边界 |
| 表格边界优先 | 摘要使用明确的 `│` 列边界和 `┌─┬─┐` 分隔线；列宽由表头和全部数据的最大可视宽度共同决定 |
| ANSI 后着色 | 先完成纯文本的宽度计算和补齐，再给状态字段包裹颜色码；ANSI 控制序列不能参与布局计算 |
| 人机输出分离 | 终端输出服务于人类快速定位；文本日志和 JSONL 不写颜色码，JSON 字段和退出码服务于检索、CI 与告警 |

之前多次“看似补齐但仍然错位”的根因不是 `=` 数量少，而是布局算法缺少不变量：

1. 阶段标题两侧使用固定数量的 `=`，没有约束所有标题的总显示宽度；标题文字长度变化后，右端必然漂移。
2. 中英文混排按照字符数而非终端显示列数补空格；中文通常占两列，导致表头和数据列逐渐错开。
3. 摘要使用无边界的连续字符串，操作者无法快速确认列的真实起止位置；某一行状态变长后更难排障。
4. 如果先给文本加 ANSI 颜色再计算宽度，控制序列会被错误地当成可见字符，颜色一开一关就出现不同的错位。
5. 分隔线长度和实际表格列宽分别硬编码，新增列或调整列宽时两者容易失去同步。

因此当前实现把“固定总宽度、显示宽度计算、边界生成、最后着色”作为可测试的不变量，而不是依赖人工目测调整空格数量。宽度常量集中在 `terminal_layout.py`，Stage Banner 和摘要表格不能各自维护一套宽度。

## 12. 常见问题

### 12.1 `uv sync` 或 `uv run` 报 `uv.lock` malformed / inconsistent wheel

典型错误：

```text
Failed to parse `uv.lock`
The entry for package `asyncssh` (2.20.0) has wheel
`asyncssh-2.22.0-py3-none-any.whl` with inconsistent version (2.22.0)
```

这不是 KBD CLI、数据库或 KB Service 的报错，而是 `uv` 在解析锁文件阶段就停止了，Python 模块尚未启动。原因是 `uv.lock` 的同一个 `[[package]]` 条目出现了版本不一致：

```toml
name = "asyncssh"
version = "2.20.0"
sdist = "asyncssh-2.22.0.tar.gz"
wheels = ["asyncssh-2.22.0-py3-none-any.whl"]
```

包元数据版本是 `2.20.0`，但下载产物文件名和校验信息是 `2.22.0`。`uv` 为防止使用错误或篡改的依赖包，会拒绝解析整个锁文件，所以以下两个命令会同时失败：

```bash
uv sync
uv run python -m data-pipeline.kbd.run --help
```

安全处理方式：

1. 先确认是否存在本地未提交的 `uv.lock` 修改：

   ```bash
   git diff -- uv.lock
   ```

2. 如果修改属于个人临时变更，先保存补丁或与变更者确认，再恢复到仓库版本；不要直接覆盖其他人的未提交工作。
3. 选择修复路径：

   - 如果该改动并非有意降级依赖：恢复为仓库中已验证的完整锁文件。
   - 如果确实要把 `asyncssh` 降级到 `2.20.0`：不能只修改 `version` 一行，必须在保存旧锁文件后重新解析并生成整份锁文件，使 sdist、wheel、hash 和 transitive dependencies 同步变化。

   损坏的锁文件本身可能使 `uv lock` 也无法启动；此时先按团队的 Git/备份流程恢复一份可解析的锁文件，或在确认备份已完成后移走损坏锁文件，再执行：

   ```bash
   uv lock
   uv lock --check
   uv sync
   ```

4. 修复或重新生成后必须确认 `asyncssh` 的 `version`、sdist 文件名和 wheel 文件名一致，并运行 KBD 帮助命令：

   ```bash
   uv run python -m data-pipeline.kbd.run --help
   ```

不要使用错误提示中的 `UV_SKIP_WHEEL_FILENAME_CHECK=1` 作为日常修复。它只是跳过保护性校验，可能把错误锁文件带入环境；只有在依赖供应方明确确认文件名检查是误报、且有临时隔离方案时才可短期使用。

### 12.2 `No module named kbd`

从仓库根目录使用：

```bash
uv run python -m data-pipeline.kbd.run --help
```

### 12.3 `No module named shared`

标准入口会自动补上当前 checkout 的 `backend`。如果仍报错，说明 checkout 缺少或损坏 `backend/shared`，先执行 `uv sync`，再确认在仓库根目录存在 `backend/shared/schemas/`；不要通过跳过 Stage 6 或重跑前五阶段绕过这个错误。

### 12.4 Portal 401/返回登录页

更新 `data-pipeline/kbd/.env` 中的 `SANGFOR_COOKIE`。Cookie 是短期凭证，不要把用户提供的真实 Header 归档到仓库。

### 12.5 没有已准备好可导入的案例

确认 `cache/{support_id}/raw.json` 存在且有效；先单独运行 fetch，查看同一 run_id 日志。

### 12.6 Vision 429 或超时

降低 `VISION_CONCURRENCY`，等待配额恢复后使用 `vision --failed`。不要靠无界重试提高“成功率”。

### 12.7 `extract-signals` 显示没有可抽取案例

检查三项：是否 draft、是否已分类、`signals_json` 是否为空。这通常是保护性跳过，而不是程序故障。

### 12.8 抽取成功但 `done=0`

如果 `needs_review>0`，说明 LLM 流程完成但没有通过门禁的活动信号。查看 `rejected_candidates` 和 Admin 审核页，不要把空壳当完成。

### 12.9 `NO_ACTIVE_SIGNAL` 是否代表没有问题

不代表。它只说明当前 Proposal 没有活动 Signal，可能是案例本来不依赖后端采集，也可能是抽取覆盖不足，必须结合原案例判断。

### 12.10 Signal Review 通过是否可以自动发布

不可以。`PASS_SIGNAL_REVIEW` 只证明当前 Signal 能按 Shared Resolution Runtime 静态编译；不证明现场文件/命令存在、关键字选对、阈值合理、故障已复现或解决方案正确。Agent 执行阶段仍需 `require_verified=True` 的现场门禁。

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
uv run python -m data-pipeline.kbd.run task --help
uv run python -m data-pipeline.kbd.run cli --help
uv run python -m data-pipeline.kbd.run extract-signals --help
uv run python -m data-pipeline.kbd.run review-signals --help
```

修改任一 Stage 时，测试必须覆盖输入、前置条件、成功、失败、幂等/保护性跳过和输出统计。修改 Signal、qfk 或 qkv 契约时，四个审查入口与 Agent 执行必须复用同一 Shared Resolution Runtime、Schema 和 Catalog，禁止各自维护一份近似规则。

## 14. 相关设计文档

- [知识库总体设计](../../docs/solution/knowledge-base/知识库设计.md)
- [关键信号字段级别抽取](../../docs/solution/agent/02-架构设计/关键信号字段级分别抽取.md)
- [qfk_log 统一日志采集、解析与判定设计](../../docs/solution/agent/02-架构设计/qfk_log统一日志采集解析与判定设计.md)
- [HCI 底层目录、日志、容器与 aCLI 知识基线](../../docs/solution/agent/02-架构设计/HCI底层目录日志容器与aCLI知识基线.md)

最后更新：2026-08-06。
