# Data Pipeline：知识生产入口

`data-pipeline` 是 HTP 的离线/批量知识生产层：把外部原始材料转换为可审核、可发布、可由 Agent 消费的知识 Proposal。它不是临时脚本集合，也不负责线上 Agent 执行。

## 责任边界

| 目录 | 负责什么 | 不负责什么 |
|---|---|---|
| `data-pipeline/kbd/` | Support 案例抓取、语义入库、截图识别、分类、关键信号抽取、生产后只读审计 | 专家批准、线上 Agent 执行 |
| `data-pipeline/raw_to_sop/` | Raw Graph JSON 转 SOP Markdown、dry-run、draft 入库 | KBD 案例抓取、SOP 发布 |
| `data-pipeline/raw/` | 待转换的源文件/示例输入 | 可复用业务逻辑 |
| `scripts/verify/` | 与具体领域无关的仓库级 CI/运维校验 | KBD 领域规则、KBD 数据库生产逻辑 |
| `backend/kb-service/` | LLM 任务、Schema 门禁、KBD 持久化和审核 API | 批量源数据编排 |
| `frontend/admin/` | 专家查看、编辑、即时验证和发布 | 自动批量生产 |
| `backend/agent-service/` | 消费已发布知识并执行 qkv/qfk 等能力 | 生成或静默修复 Proposal |

判断逻辑很简单：如果一段代码理解 `signals_json`、KBD 阶段或日志信号质量，它属于 `data-pipeline/kbd`；如果它只是让 CI/运维调用这段能力，可以留在 `scripts`，但只能做薄封装。

因此，日志信号审计的唯一实现是 `kbd/log_signal_audit.py`，唯一 CLI 入口是 `python -m kbd.run audit-log-signals`。仓库不再保留 `scripts/verify` 下的重复入口。

## 生产闭环

KBD 的自动生产链路是：

```text
Support Portal
  → fetch
  → import
  → vision
  → classify
  → extract-signals
  → audit-log-signals
  → Admin 专家复核/修改/验证
  → publish
  → Agent 消费
```

前六步可由 `python -m kbd.run pipeline` 一次完成。专家复核是当前质量兜底，不被伪装成自动阶段；发布内容以专家确认后的稳定版本为准。

## 统一运行约定

所有命令从仓库根目录执行。KBD 的审计逻辑复用 `backend/shared` 中与 Agent 相同的 Schema 和日志 Catalog，因此统一设置：

```bash
PYTHONPATH=data-pipeline:backend uv run python -m kbd.run --help
```

不要使用 `python -m data-pipeline.kbd.run`：目录名含连字符，不是合法 Python 包路径。

安装依赖：

```bash
uv sync
```

KBD 快速试跑：

```bash
cp data-pipeline/kbd/.env.example data-pipeline/kbd/.env
PYTHONPATH=data-pipeline:backend uv run python -m kbd.run config
PYTHONPATH=data-pipeline:backend uv run python -m kbd.run pipeline --ids 37150
```

详细的环境变量、六阶段语义、关键信号抽取、审计报告和故障处理见 [KBD 使用手册](kbd/README.md)。

## Raw Graph JSON 转 SOP

只转换、不入库：

```bash
PYTHONPATH=data-pipeline uv run python -m raw_to_sop \
  --file data-pipeline/raw/内存ECC故障.json \
  --dry-run \
  --stdout
```

以 draft 入库：

```bash
cp data-pipeline/raw_to_sop/.env.example data-pipeline/raw_to_sop/.env
PYTHONPATH=data-pipeline uv run python -m raw_to_sop \
  --file data-pipeline/raw/内存ECC故障.json \
  --category-id "硬件-内存"
```

该管道的产物仍需在 Admin UI 审核后发布。

## 安全与数据原则

- `.env`、Cookie、Token、数据库口令不得提交 Git，也不得写入 README、测试夹具或日志。
- 抓取缓存保留原始案例和图片，仅用于生产追溯；不要把真实缓存批量提交仓库。
- `audit-log-signals` 默认只读，不修改 Proposal。发现问题默认返回 0 并生成专家清单；只有显式使用 `--fail-on-blocked` 才作为 CI 门禁返回 1。
- `pipeline --override --override-status all` 可能覆盖已发布内容，仅在明确理解影响时使用；日常生产优先处理 draft。
- LLM 输出是 Proposal，不是事实。截图推断、分类和关键信号都必须保留可追溯证据并经过发布门禁。

## 开发和验证

KBD 相关最小回归：

```bash
PYTHONPATH=data-pipeline:backend uv run pytest -q tests/unit/kbd
PYTHONPATH=data-pipeline:backend uv run ruff check data-pipeline/kbd tests/unit/kbd
git diff --check
```

新增生产能力时应同时完成：

1. 可导入的领域模块，而不是把规则堆进 `scripts`。
2. `kbd.run` 的可发现 CLI 入口和 `--help`。
3. 输入、输出、幂等、失败与人工边界的单元测试。
4. 本 README 的入口说明和对应子管道的详细手册。

最后更新：2026-07-30。
