# KBD 知识生产管道使用手册

> **文档定位**：KBD（知识库条目）数据生产的完整操作指南，涵盖从原始数据抓取到最终发布的全流程。

---

## 目录

- [概述](#概述)
- [快速开始](#快速开始)
- [CLI 命令详解](#cli-命令详解)
- [数据生产流程](#数据生产流程)
- [数据模型](#数据模型)
- [常见场景](#常见场景)
- [故障排除](#故障排除)
- [附录](#附录)

---

## 概述

### 什么是 KBD Pipeline？

KBD Pipeline 是 HCI 智能排障平台的知识生产系统，负责：

1. **数据抓取**：从深信服 Portal 抓取原始案例数据
2. **图片语义化**：使用 Vision LLM 识别截图内容
3. **数据转换**：HTML → 结构化 Markdown
4. **数据入库**：写入 PostgreSQL 数据库
5. **AI 分类**：自动分配分类编码

### 核心概念

| 概念 | 说明 |
|------|------|
| **KBD（Knowledge Base Document）** | 历史故障案例，陈述性知识（What-happened） |
| **SOP（Standard Operating Procedure）** | 排障手册，程序性知识（How-to） |
| **support_id** | 深信服案例 ID（幂等键） |
| **category_id** | 分类编码（如 `虚拟机-003`） |

### 架构概览

新架构 DAG（无环，依赖单向，2026-07 重构 P0–P2）：

```
┌────────────────────────────────────────────────────────────────────┐
│                      KBD Pipeline 架构 (新)                          │
├────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  Stage 1: fetch       Stage 2: import       Stage 3: vision         │
│  ┌─────────┐         ┌──────────────┐       ┌──────────────┐        │
│  │ 原始数据 │────────→│ 语义提取+     │──────→│ 图片语义化    │        │
│  │ 抓取    │         │ 原子入库       │       │ (异步 LLM)    │        │
│  └─────────┘         └──────────────┘       └──────────────┘        │
│       ↓                   ↓                         ↓              │
│  cache/26890/        API: /kbd/ingest         POST /reanalyze-images│
│  - raw.json          (同事务原子写             → 202 + job_id       │
│  - img_N.png          kbd_entry + kbd_image)  GET /status?job_id=…  │
│                                                                      │
│                              ↓                                      │
│                        Stage 4: classify                            │
│                        ┌─────────────┐                              │
│                        │  AI 分类    │                              │
│                        │  (LLM)      │                              │
│                        └─────────────┘                              │
│                              ↓                                      │
│                        更新 category_id                              │
└────────────────────────────────────────────────────────────────────┘
```

**新原则（Content–Presentation Separation，CLAUDE.md §9）**：
- data-pipeline 只做语义提取（段落 + 列表 + 表格键值），丢弃装饰样式
- content_md 不在 data-pipeline 生成，**交由后端 `rebuild_content_md()` 统一渲染**（避免 markdownify 缩进污染 + 跨案例样式高一致）
- `images_json.desc` 初始为空，由 VISION 阶段（reanalyze）填充
- 所有 KBD 的 content_md 由同一函数渲染 → 样式高一致

---

## 快速开始

### 环境准备

```bash
# 1. 进入项目根目录
cd /mnt/d/aihci/hci-troubleshoot-platform

# 2. 安装依赖
uv sync

# 3. 配置环境变量
# 创建 data-pipeline/kbd/.env 文件（参考 .env.example）
cp data-pipeline/kbd/.env.example data-pipeline/kbd/.env

# 编辑 .env，填入必要配置
# - SANGFOR_COOKIE: 深信服 Portal Cookie（抓取数据用）
# - DATABASE_URL: PostgreSQL 连接串
# - KB_SERVICE_URL: kb-service API 地址
# - INTERNAL_API_TOKEN: 内部 API Token
```

### 运行完整 Pipeline

```bash
# 运行完整流水线（所有 4 个 Stage）
uv run python -m data-pipeline.kbd.run pipeline --excel

# 或指定案例 ID
uv run python -m data-pipeline.kbd.run pipeline --ids 26890,26891

# 或仅运行特定 Stage
uv run python -m data-pipeline.kbd.run pipeline --excel --stages fetch,vision
```

### 查看帮助

```bash
# 查看所有命令
uv run python -m data-pipeline.kbd.run --help

# 查看子命令帮助
uv run python -m data-pipeline.kbd.run pipeline --help
uv run python -m data-pipeline.kbd.run fetch --help
```

---

## CLI 命令详解

### 命令总览

```
python -m kbd.run <command> [options]

Commands:
  pipeline      运行完整流水线（或指定 stages）
  fetch         Stage 1：抓取 API + 下载图片
  vision        Stage 2：图片语义化（Vision LLM）
  import        Stage 3：HTML→MD 转换 + 调用 API 入库
  classify      Stage 4：AI 分类（调用 kb-service API）
  review-list   列出待审核案例
  config        打印当前配置
  upload-images 上传本地 cache 图片到数据库（新增）
```

### 通用参数

| 参数 | 说明 | 示例 |
|------|------|------|
| `--ids` | 指定案例 ID（逗号分隔） | `--ids 26890,26891` |
| `--excel` | 从 Excel 文件读取案例 ID | `--excel` |
| `--stages` | 指定运行的 Stage（逗号分隔） | `--stages fetch,vision` |
| `--run-id` | 指定运行 ID（用于日志追踪） | `--run-id 20260709_100000` |

### pipeline 子命令

运行完整或部分流水线。

```bash
# 完整流水线（所有 Stage）
uv run python -m data-pipeline.kbd.run pipeline --excel

# 仅运行 Stage 1 和 Stage 2
uv run python -m data-pipeline.kbd.run pipeline --ids 26890 --stages fetch,vision

# 断点续传（跳过已完成的案例）
uv run python -m data-pipeline.kbd.run pipeline --excel --resume

# 仅处理失败的案例
uv run python -m data-pipeline.kbd.run vision --excel --failed-only

# 强制覆盖已存在的记录
uv run python -m data-pipeline.kbd.run import --ids 26890 --override --override-status all
```

**pipeline 参数详解：**

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--resume` | 断点续传，跳过已完成的案例 | `False` |
| `--resume-run-id` | 指定要恢复的 run_id | 自动查找最新 |
| `--failed-only` | 仅处理失败的案例 | `False` |
| `--override` | 强制覆盖已存在的记录 | `False` |
| `--override-status` | 仅覆盖指定状态的记录（`draft`/`published`/`all`） | `draft` |
| `--force-fetch` | 强制重新抓取（即使已有 raw.json） | `False` |

### fetch 子命令

Stage 1：从深信服 Portal 抓取原始数据和下载图片。

```bash
# 抓取指定案例
uv run python -m data-pipeline.kbd.run fetch --ids 26890

# 从 Excel 批量抓取
uv run python -m data-pipeline.kbd.run fetch --excel

# 强制重新抓取（覆盖已有 raw.json）
uv run python -m data-pipeline.kbd.run fetch --ids 26890 --force
```

**输出：**
- `cache/{support_id}/raw.json` - 原始 API 响应
- `cache/{support_id}/img_N.png` - 下载的图片
- `cache/{support_id}/img_N.failed` - 下载失败的标记

### vision 子命令

Stage 2：使用 Vision LLM 识别图片内容。

```bash
# 识图指定案例
uv run python -m data-pipeline.kbd.run vision --ids 26890

# 仅处理识图失败的案例
uv run python -m data-pipeline.kbd.run vision --excel --failed-only
```

**输出：**
- 不再写入本地文件。Vision 描述（desc）由后端 `reanalyze-images` 异步填充到
  `kbd_entry.images_json[].desc`；原始图片二进制已随 IMPORT 原子写入 `kbd_image` 表。

### import 子命令

Stage 3：HTML 转 Markdown 并调用 kb-service API 入库。

```bash
# 导入指定案例
uv run python -m data-pipeline.kbd.run import --ids 26890

# 强制覆盖
uv run python -m data-pipeline.kbd.run import --ids 26890 --override --override-status all
```

**幂等行为：**

| 场景 | 行为 |
|------|------|
| support_id 不存在 | 创建新记录，status=draft |
| support_id 已存在，status=draft | 覆盖（需 `--override`） |
| support_id 已存在，status=published | 拒绝覆盖（需 `--override-status all`） |

### classify 子命令

Stage 4：AI 自动分类。

```bash
# 分类指定案例
uv run python -m data-pipeline.kbd.run classify --ids 26890
```

**输出：**
- 更新 `kbd_entry.ai_category_id`
- 更新 `kbd_entry.ai_category_conf`
- 更新 `kbd_entry.ai_category_reason`

### upload-images 子命令（新增）

将本地 cache 目录的图片上传到 `kbd_image` 表，支持"重新识图"功能。

```bash
# 上传指定案例的图片
uv run python -m data-pipeline.kbd.upload_images_to_db --ids 26890

# 上传所有 cache 目录的图片
uv run python -m data-pipeline.kbd.upload_images_to_db --all
```

**为什么需要上传图片？**
- `reanalyze-images` API 从 `kbd_image` 表读取原始图片
- 本地 cache 目录的图片需先上传到数据库
- 上传后可在 Admin UI 点击"重新识图"按钮

### review-list 子命令

列出待审核的案例。

```bash
uv run python -m data-pipeline.kbd.run review-list
```

### config 子命令

打印当前配置。

```bash
uv run python -m data-pipeline.kbd.run config
```

---

## 数据生产流程

### 完整流程图

```
┌─────────────┐
│ 深信服 Portal │
└──────┬──────┘
       │ fetch (Stage 1)
       ↓
┌─────────────┐
│ cache/ID/   │
│ - raw.json  │
│ - img_N.png │
└──────┬──────┘
       │ vision (Stage 2)
       ↓
┌─────────────┐
│ PostgreSQL  │
│ images_json │
│ .desc (DB)  │
└──────┬──────┘
       │ import (Stage 3)
       ↓
┌─────────────┐
│ PostgreSQL  │
│ - kbd_entry │
│ - kbd_image │
└──────┬──────┘
       │ classify (Stage 4)
       ↓
┌─────────────┐
│ kbd_entry   │
│ .category_id│
└─────────────┘
```

### Stage 详细说明

#### Stage 1: fetch（数据抓取）

**输入：**
- 案例列表（`--ids` 或 `--excel`）

**处理：**
1. 调用深信服 API 获取案例详情
2. 解析 HTML 提取图片 URL
3. 下载图片到 cache 目录

**输出：**
- `cache/{support_id}/raw.json` - 完整 API 响应
- `cache/{support_id}/img_N.png` - 原始图片
- `cache/{support_id}/{support_id}.lock` - 并发锁

**幂等性：**
- `raw.json` 存在则跳过（除非 `--force-fetch`）

#### Stage 2: vision（图片语义化）

**输入：**
- `cache/{support_id}/img_N.png`

**处理：**
1. 读取本地图片文件
2. 压缩（>500KB 自动压缩）
3. 调用 Vision LLM（qwen3.5-plus）
4. 解析输出为结构化字段

**输出：**
- 不再写入本地文件；描述由后端 reanalyze 填充到 `kbd_entry.images_json[].desc`

**字段说明：**

| 字段 | 说明 |
|------|------|
| `TYPE` | 截图类型：终端截图/日志截图/告警截图/任务截图/其他截图 |
| `BACKGROUND` | 背景颜色：黑色/白色/其他 |
| `FULL_TEXT` | OCR 文本（如有） |
| `DESCRIPTION` | 语义描述（供 RAG 检索） |

#### Stage 3: import（数据入库）

**输入：**
- `cache/{support_id}/raw.json`
- 图片二进制已随 IMPORT 原子写入 `kbd_image` 表（由 `upload-images` 或 IMPORT 阶段上传）

**处理：**
1. 解析 raw.json 提取 8 大章节
2. 建立图片全局序号映射，`images_json` 中 desc 初始留空
3. 生成结构化章节字段（含 `![img:N]` 占位符）
4. `content_md` 不在此生成，交由后端 `rebuild_content_md()` 统一渲染（样式高一致）
5. 调用 kb-service API 入库（同事务写 kbd_entry + kbd_image）

**输出：**
- `kbd_entry` 表记录
- `kbd_image` 表记录（如使用 upload-images）

**8 大章节字段：**
- `problem_description` - 问题描述
- `alert_info` - 告警信息
- `steps_text` - 有效排查步骤
- `root_cause` - 根因
- `solution` - 解决方案
- `operational_impact` - 操作影响范围
- `is_temporary` - 是否是临时解决方案
- `recommendations` - 建议与总结

#### Stage 4: classify（AI 分类）

**输入：**
- `kbd_entry` 记录（已入库）

**处理：**
1. 提取问题侧文本（title + problem_description + alert_info + root_cause）
2. 调用 LLM 进行分类
3. 返回 category_id + 置信度 + 理由

**输出：**
- 更新 `kbd_entry.ai_category_id`
- 更新 `kbd_entry.ai_category_conf`
- 更新 `kbd_entry.ai_category_reason`

---

## 数据模型

### kbd_entry 表

```sql
CREATE TABLE kbd_entry (
    id                  BIGSERIAL PRIMARY KEY,
    support_id          VARCHAR(20) UNIQUE NOT NULL,    -- 幂等键
    title               TEXT NOT NULL,
    
    -- 8 大章节字段
    problem_description TEXT DEFAULT '',
    alert_info          TEXT DEFAULT '',
    steps_text          TEXT DEFAULT '',
    root_cause          TEXT DEFAULT '',
    solution            TEXT DEFAULT '',
    operational_impact  TEXT DEFAULT '',
    is_temporary        TEXT DEFAULT '',
    recommendations     TEXT DEFAULT '',
    
    -- 结构化字段
    steps_json          JSONB DEFAULT '[]',              -- 结构化工具步骤
    images_json         JSONB DEFAULT '[]',              -- 图片视觉描述列表
    
    -- 双通道数据
    content_md          TEXT,                            -- Markdown（含视觉描述）
    content_raw         TEXT,                            -- 纯文本（去噪）
    
    -- 分类字段
    category_id         VARCHAR(32),                     -- 人工确认分类
    ai_category_id      VARCHAR(32),                     -- AI 分类建议
    ai_category_conf    FLOAT,                           -- 分类置信度
    ai_category_reason  TEXT,                            -- 分类理由
    
    -- 检索字段
    embedding           vector(1536),                    -- 语义向量
    tsv                 tsvector,                        -- BM25 全文索引
    
    -- 状态机
    status              VARCHAR(20) DEFAULT 'draft',
    -- draft → published → archived
    --       → rejected
    
    created_at          TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at          TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
```

### kbd_image 表

```sql
CREATE TABLE kbd_image (
    id              SERIAL PRIMARY KEY,
    kbd_entry_id    BIGINT NOT NULL REFERENCES kbd_entry(id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,                     -- 图片序号（与 images_json 对应）
    image_data      BYTEA NOT NULL,                      -- 原始图片二进制
    mime_type       VARCHAR(50),                         -- MIME 类型
    width           INTEGER,                             -- 图片宽度
    height          INTEGER,                             -- 图片高度
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    
    UNIQUE (kbd_entry_id, seq)
);
```

**用途：**
- 存储原始图片二进制数据
- 支持 Admin UI "重新识图"功能
- 解耦 kb-service 对本地文件系统依赖

### images_json 结构

```json
[
  {
    "seq": 0,
    "section": "steps_text",
    "desc": "TYPE: 终端截图\nBACKGROUND: 黑色\nFULL_TEXT:\n- （无文字）\nDESCRIPTION:\n（无描述）"
  }
]
```

---

## 常见场景

### 场景 1：批量导入新案例

```bash
# 1. 准备 Excel 文件（包含 support_id 列）
# data-pipeline/kbd/cases.xlsx

# 2. 运行完整流水线
uv run python -m data-pipeline.kbd.run pipeline --excel

# 3. 检查日志
tail -f data-pipeline/kbd/logs/kbd_*.log

# 4. 查看进度
cat data-pipeline/kbd/logs/progress_*.json

# 5. 上传图片到数据库（支持重新识图）
uv run python -m data-pipeline.kbd.upload_images_to_db --excel

# 6. 在 Admin UI 审核、发布
```

### 场景 2：断点续传

```bash
# Pipeline 中断后继续运行
uv run python -m data-pipeline.kbd.run pipeline --excel --resume

# 或指定要恢复的 run_id
uv run python -m data-pipeline.kbd.run pipeline --excel --resume --resume-run-id 20260709_100000
```

### 场景 3：重新处理失败案例

```bash
# 仅处理识图失败的案例
uv run python -m data-pipeline.kbd.run vision --excel --failed-only

# 查看失败原因
grep -r "failed" data-pipeline/kbd/logs/kbd_*.log
```

### 场景 4：更新已发布的案例

```bash
# 强制覆盖已发布的记录
uv run python -m data-pipeline.kbd.run import --ids 26890 --override --override-status all
```

### 场景 5：图片重新识图

```bash
# 1. 上传图片到数据库（首次）
uv run python -m data-pipeline.kbd.upload_images_to_db --ids 26890

# 2. 在 Admin UI 点击"重新识图"按钮
# 或通过 API 调用：
kubectl port-forward -n hci-dev deployment/kb-service 8004:8004 &
curl -X POST http://localhost:8004/api/admin/kbd/2688/reanalyze-images \
  -H "Authorization: Bearer $(kubectl get secret -n hci-dev hci-secrets -o jsonpath='{.data.INTERNAL_API_TOKEN}' | base64 -d)"
```

### 场景 6：SOP 文档导入

```bash
# 方式 1：CLI 导入
uv run python -m data-pipeline.kbd.import_sop \
  --file /path/to/your.docx \
  --category-id "虚拟机-001" \
  --token $(kubectl get secret -n hci-dev hci-secrets -o jsonpath='{.data.INTERNAL_API_TOKEN}' | base64 -d) \
  --api-url http://localhost:8080

# 方式 2：Admin UI 上传
# 1. kubectl port-forward -n hci svc/api-gateway 8080:80 &
# 2. kubectl port-forward -n hci svc/admin-ui 3000:80 &
# 3. 打开 http://localhost:3000
# 4. 进入 知识库管理 → SOP 文档 → 导入 .docx
```

---

## 故障排除

### 常见错误

#### 1. `Cookie 已过期（401）`

**原因：** SANGFOR_COOKIE 过期

**解决：**
```bash
# 1. 从浏览器重新获取 Cookie
# 2. 更新 .env 文件
# 3. 重新运行
```

#### 2. `Vision LLM 失败：429 Too Many Requests`

**原因：** 并发配额超限

**解决：**
```bash
# 等待后重试，或使用 --failed-only 仅处理失败的案例
uv run python -m data-pipeline.kbd.run vision --excel --failed-only
```

#### 3. `该 KBD 无原始图片，无法重算识图`

**原因：** 图片未上传到 `kbd_image` 表

**解决：**
```bash
# 上传图片到数据库
uv run python -m data-pipeline.kbd.upload_images_to_db --ids 26890
```

#### 4. `connection is closed`

**原因：** 数据库连接超时

**解决：**
- 检查数据库连接配置
- 检查网络连通性
- 查看 kb-service 日志

#### 5. `案例 X 已存在，跳过`

**原因：** 幂等行为，默认跳过已存在记录

**解决：**
```bash
# 强制覆盖
uv run python -m data-pipeline.kbd.run import --ids X --override --override-status all
```

### 日志查看

```bash
# 查看最新日志
tail -f data-pipeline/kbd/logs/kbd_*.log

# 搜索错误
grep -i "error\|failed\|exception" data-pipeline/kbd/logs/kbd_*.log

# 查看进度
cat data-pipeline/kbd/logs/progress_*.json | jq .
```

### 配置检查

```bash
# 打印当前配置
uv run python -m data-pipeline.kbd.run config

# 检查环境变量
cat data-pipeline/kbd/.env
```

---

## 附录

### 环境变量说明

| 变量 | 必填 | 说明 |
|------|------|------|
| `SANGFOR_COOKIE` | ✓ | 深信服 Portal Cookie |
| `SANGFOR_API_BASE` | ✓ | API 基础 URL |
| `DATABASE_URL` | ✓ | PostgreSQL 连接串 |
| `KB_SERVICE_URL` | ✓ | kb-service API 地址 |
| `INTERNAL_API_TOKEN` | ✓ | 内部 API Token |
| `LLM_BASE_URL` | | LLM API 地址 |
| `LLM_API_KEY` | | LLM API Key |
| `LLM_DEFAULT_MODEL` | | 默认模型 |
| `KBD_CACHE_DIR` | | 缓存目录（默认 `data-pipeline/kbd/cache`） |
| `KBD_LOGS_DIR` | | 日志目录（默认 `data-pipeline/kbd/logs`） |

### 目录结构

```
data-pipeline/kbd/
├── cache/                    # 缓存目录
│   ├── 26890/               # 案例 ID
│   │   ├── raw.json         # 原始 API 响应
│   │   ├── img_0.png        # 图片文件
│   │   └── 26890.lock       # 并发锁
│   └── ...
├── logs/                     # 日志目录
│   ├── kbd_20260709_100000.log
│   └── progress_20260709_100000.json
├── .env                      # 环境配置
├── config.py                 # 配置模块
├── run.py                    # CLI 入口
├── pipeline.py               # Pipeline 逻辑
├── fetcher.py                # Stage 1
├── image_proc.py             # Stage 2
├── importer.py               # Stage 3
├── converter.py              # HTML→MD 转换
├── classifier.py             # Stage 4
├── upload_images_to_db.py    # 图片上传工具
└── README.md                 # 本文档
```

### 相关文档

- [知识库设计](../../docs/solution/knowledge-base/知识库设计.md) - 整体架构设计
- [KBD 双通道数据模型](../../docs/solution/events/2026-06-01-KBD双通道数据模型与解耦重构.md) - 数据模型详解
- [Pipeline 日志与进度追踪](../../docs/solution/events/2026-04-20-KBD-Pipeline日志与进度追踪改进.md) - 日志系统
- [SOP 导入操作指南](../../docs/deploy/events/2026-04-11-SOP导入操作指南.md) - SOP 文档导入

---

**维护者：** HCI 智能排障平台团队  
**最后更新：** 2026-07-10