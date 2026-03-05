# HCI 智能排障平台 - 知识库 RAG 设计

更新时间: 2026-03-05

---

> **文档边界说明**
> - **本文档范围**：知识库基础设施——数据源采集、文档生命周期、存储 Schema、检索引擎、ETL 数据管道、SOP 机制。
> - **不在本文档**：AI Agent 的行为规范、LearningClaw/ProductionClaw 架构、对话上下文组装逻辑。
> - **相关文档**：`12_AI层设计.md` — AI 层如何消费知识库；`03_接口设计.md` — KB Service REST API 规范。

---

## 一、概述

### 1.1 系统目标

为 HCI 智能排障平台构建知识库检索增强生成（RAG）系统，实现：

1. **7000+ 在网知识库案例**的结构化处理、存储与智能检索
2. **SOP 排障手册**的决策树式精确路由
3. **对话场景下的知识注入**，让 AI 助手基于真实案例和 SOP 生成精准答案
4. **知识生命周期管理**，支持批量生产、人工审核、增量更新、质量闭环

### 1.2 核心理念

- **精准优于召回**：有就是有，没有就是没有，减少幻觉，杜绝胡说
- **SOP 确定性路由优先**：关键字精确匹配零 LLM 开销，确保高频故障秒级响应
- **分类先行**：先定位问题域（1-3 轮追问），再针对性检索，避免全库盲搜
- **人工审核门控**：所有生成的知识文档必须人工审核后才能对外可见

### 1.3 架构选型：Agentic RAG (C) + 混合检索 (B)

经过 3 种检索策略的对比评估，最终采用 **Agentic RAG + 混合检索** 组合：

| 方案 | 描述 | 优劣 | 结论 |
|------|------|------|------|
| A: 纯向量检索 | Query → Embedding → pgvector top-K | 简单但中文同义词召回差 | ❌ 不采用 |
| B: 混合检索 | BM25 + Vector + RRF 融合 | 精度高但无分类前置 | 单独不够 |
| C: Agentic RAG | LLM 分类 → SOP 匹配 → 检索 | 精度最高，有分类门控 | ✅ 核心 |

**最终方案：C 为主框架（分类+SOP路由），B 为检索引擎（SOP不命中时兜底）**

---

## 二、数据源分析

### 2.1 三种数据源

| # | 数据源 | 规模 | 格式 | 质量 | 状态 |
|---|--------|------|------|------|------|
| 1 | 在网知识库（support.sangfor.com.cn） | ~7000 案例 | Web 页面 → HTML JSON | 70-80% CSS 噪声，需深度清洗 | 原始 JSON 已采集 ~700，需扩展 |
| 2 | 已处理 Markdown | ~600 案例 | YAML frontmatter + 结构化 MD | 混合质量（见§2.2） | 已完成 |
| 3 | SOP 排障手册 | 1 本（37 章节） | keywords_map.json + 拆分 MD | 格式丢失（DOCX→MD 换行丢失） | 需清洗 |

### 2.2 已处理 Markdown 质量问题

| 问题 | 详情 | 影响 | 处理方案 |
|------|------|------|---------|
| judgment_logic 幻觉 | 不同案例出现相同模板输出（如 "Verify if physical peth0 link status is UP via ethtool" 出现在不相关案例中） | 排查逻辑摘要不可信 | 修复 prompt 后重新生成（§八） |
| summary/judgment_logic 英文 | 中文案例生成英文元数据 | 语义损失 | 改为中文输出 |
| Prompt 模板结构缺陷 | 双重 Output Format 段、双 `{taxonomy_context}`、两个编号 6 | LLM 产生混乱输出 | 修复 prompt 模板 |
| SOP 格式丢失 | DOCX→MD 转换丢失所有换行/标题结构，内容成单段连排 | 不可直接用于检索 | 恢复 markdown 格式 |

### 2.3 数据规模估算

| 指标 | 数值 |
|------|------|
| 目标文档总数 | ~7000 |
| 平均文档长度 | ~1500 字 |
| 分块数（512 tokens, 128 overlap） | ~60,000 chunks |
| 向量维度 | 384 (bge-small-zh) 或 z.ai 维度 |
| SOP 节点数 | ~40 (1 本手册) |
| 分类树节点数 | ~260 (4 层, 6 个 L1) |

---

## 三、知识文档生命周期

### 3.1 完整旅程图

```text
┌─────────────────────────────────────────────────────────────────────────┐
│                               数据源 (3 种入口)                          │
├──────────────────────┬──────────────────┬───────────────────────────────┤
│      S1: 在网KB 7000+ │ S7: 增量新案例   │ S9: 客户端排障记录→AI即时生成MD │
│           (批量ETL)   │    (定期采集)    │ (实时，随工单关闭触发)          │
└──────────┬───────────┴────────┬─────────┴──────────────┬────────────────┘
           │                    │                        │
           ▼                    ▼                        ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                   ETL 处理管道 (data-pipeline)                           │
│        采集 → HTML清洗 → 图片OCR → AI元数据增强 → 标准化MD生成             │
└────────────────────────────────┬────────────────────────────────────────┘
                                 │ 输出: 标准化 MD (YAML frontmatter + 正文)
                                 ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                      ① Draft 暂存区 (待审核)                             │
│                                                                         │
│ 状态: draft                                                             │
│ 存储: 文件系统 draft/ 目录 + DB kb_document (status='draft')             │
│ 可见性: 仅 Admin 后台可见                                                │
│ 操作: 浏览、批注、编辑、批量通过/拒绝                                      │
└────────────────────────────────┬────────────────────────────────────────┘
                                 │
                          人工审核 (Admin UI)
                       ┌─────────┼─────────┐
                       ▼         ▼         ▼
                  ✅ 直接通过 ✏️ 编辑后通过 ❌ 拒绝
                (approved) (修改→approved) (rejected + 原因)
                       │         │         │
                       ▼         ▼         ▼
                            ┌────┴─────────┘ 返回ETL或人工修正
                            ▼ 后重新提交
┌─────────────────────────────────────────────────────────────────────────┐
│                      ② Production 正式区 (已发布)                        │
│                                                                         │
│ 状态: published                                                         │
│ 双写:                                                                   │
│ 文件: published/ 目录 (人可读，可 Git 管理，支持 diff/review)             │
│ DB: kb_document (status='published') → 触发分块 → embedding → 索引       │
│ 可见性: 检索可用 + Admin 可管理                                           │
└────────────────────────────────┬────────────────────────────────────────┘
                                 │ 触发
                                 ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                      ③ 知识索引层 (派生，可重建)                          │
│                                                                         │
│ kb_chunk: 文档分块 (512 tokens, 128 overlap)                             │
│ embedding: 向量索引 (pgvector, z.ai/bge-small)                           │
│ tsvector: 全文索引 (BM25, jieba 分词)                                    │
│ kb_sop_node: SOP 关键字路由表                                            │
│ kb_category: 分类树                                                      │
│                                                                         │
│ 特性: 全部可从 Production 正式区重建                                      │
└────────────────────────────────┬────────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                      ④ KB Service 检索引擎 (运行时)                      │
│                                                                         │
│ LLM分类 → SOP精确匹配 → 混合检索(BM25+Vector+RRF) → Rerank                │
│ ↓                                                                       │
│ 返回 ranked results (chunks + SOP节点 + 分类路径)                         │
│                                                                         │
│ ⚑ KB Service 职责止于「返回」。上下文组装（4-Tier Prompt 拼装）            │
│   与 LLM 调用属于 AI 层职责，详见《12_AI层设计.md》。                      │
└─────────────────────────────────────────────────────────────────────────┘
                                 │
                            用户对话结束
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                      ⑤ 知识反馈闭环 (远期)                               │
│                                                                         │
│ S9: 排障记录 → AI 生成 MD → 回到 ① Draft 暂存区 → 人工审核 → 发布          │
│ 用户评价 → 文档质量评分 → 标记低质量文档 → 人工优化                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### 3.2 文档状态机

```text
draft → under_review → approved → published
↓ ↓
rejected archived (下线/过期)
↓
revised (修正后重新提交 → draft)
```

### 3.3 使用场景矩阵

| # | 场景 | 读/写 | 频率 | 关键需求 |
|---|------|-------|------|---------|
| S1 | ETL 批量生产 — 7000+ 在网 KB → MD | 写 | 一次性+增量 | 断点续传、并发、幂等 |
| S2 | KB 导入 — MD → 分块 → Embedding → DB | 读→转换→写DB | 全量一次+增量 | 批量读取、增量检测 |
| S3 | SOP 加载 — 路由表+章节 → DB | 读→写DB | 低频 | 版本管理、热更新 |
| S4 | 检索上下文组装 — chunk/SOP → prompt | 读(从DB) | 高频(每次对话) | 毫秒级延迟 |
| S5 | 知识管理后台 — 浏览/编辑/审核文档 | 读+写 | 中频 | 全文预览、编辑回写 |
| S6 | 质量审核 — 人工抽检、批注标记 | 读 | 中频 | 原始 MD 可读 |
| S7 | 增量更新 — 新案例发布 → 采集处理入库 | 写→读→写DB | 定期(日/周) | 去重、自动触发 |
| S8 | 全量重建 — 修改策略后重跑 | 读→重写→重导入 | 极低频 | 幂等、可回滚 |
| S9 | 客户端实时生成 — 排障记录 → 即时 MD | 写 | 中频(随工单) | 实时性、审核门控 |
| S10 | 导出/备份 — 知识库快照 | 读 | 极低频 | 完整性 |

---

## 四、系统架构

### 4.1 核心查询链路

```text
用户问题
│
▼
┌──────────────────┐
│ LLM 分类器 │ 输入: 用户问题 + kb_category 树
│ (1-3轮追问) │ 输出: { l1, l2, confidence }
└────────┬─────────┘ confidence < 0.7 → 追问用户
│
▼
┌──────────────────┐
│ SOP 精确匹配 │ 遍历 keywords_map.json
│ (确定性路由) │ 关键字命中 → 返回章节全文
└────────┬─────────┘
│
┌────┴────┐
│ │
命中 未命中
│ │
▼ ▼
直接使用 ┌──────────────────┐
SOP内容 │ 混合检索引擎 │
│ BM25 top-20 │
│ + Vector top-20 │
│ → RRF 融合 │
│ → top-5 │
└────────┬─────────┘
│
▼
┌──────────────────┐
│ Reranker │
│ LLM 相关性评分 │
│ 过滤 < 0.5 │
│ 全部 < 0.5 │
│ → "未找到相关知识" │
└────────┬─────────┘
│
▼
┌──────────────────────────────┐
│ 上下文组装 (4-Tier Prompt) │
│ Tier-1: System (角色定义) │
│ Tier-2: SOP (匹配章节全文) │
│ Tier-3: RAG (检索 top-K) │
│ Tier-4: History (对话历史) │
└──────────────────────────────┘
│
▼
LLM 生成回答
```

### 4.2 KB Service 模块设计

KB Service 作为新微服务部署在端口 8004，遵循现有项目架构模式（FastAPI + app.state DI）。

```text
backend/kb-service/
├── Dockerfile
├── requirements.txt
├── app/
│ ├── init.py
│ ├── config.py # KB_SERVICE_PORT=8004, EMBEDDING_MODEL, CHUNK_SIZE...
│ ├── main.py # FastAPI app, OTel 初始化, app.state DI
│ ├── models/
│ │ ├── init.py
│ │ ├── document.py # kb_document SQLAlchemy 模型
│ │ ├── chunk.py # kb_chunk 模型 (含 pgvector Vector 类型)
│ │ └── sop_node.py # kb_sop_node 模型
│ ├── repositories/
│ │ ├── init.py
│ │ ├── document_repo.py # 文档 CRUD
│ │ ├── chunk_repo.py # 向量/全文检索
│ │ └── sop_repo.py # SOP 节点 CRUD
│ ├── routes/
│ │ ├── init.py
│ │ ├── search.py # GET /api/kb/search — 统一检索入口
│ │ ├── ingest.py # POST /api/kb/ingest — 文档导入
│ │ ├── admin.py # 管理接口（审核、发布、下线）
│ │ └── health.py # 健康检查
│ ├── services/
│ │ ├── init.py
│ │ ├── classifier.py # LLM 分类器
│ │ ├── search_engine.py # BM25 + Vector + RRF 混合检索
│ │ ├── sop_matcher.py # SOP 关键字精确匹配
│ │ ├── embedding.py # 双模式 Embedding (z.ai + bge-small)
│ │ ├── chunker.py # 文本分块
│ │ └── ingestor.py # 文档导入（解析MD→分块→embedding→入库）
│ └── utils/
│ ├── init.py
│ ├── html_cleaner.py # HTML 清洗
│ ├── jieba_hci.py # jieba + HCI 自定义词典
│ └── text_splitter.py # RecursiveCharacterTextSplitter
```

### 4.3 与现有服务集成关系

```text
外部消费者（均通过 HTTP 调用）

  ┌─────────────────────────────────┐
  │         KB Service (8004)       │
  │                                 │
  │  POST /api/kb/search            │  ← Conversation Service / ProductionClaw
  │  POST /api/kb/sop/match         │  ← Conversation Service
  │  POST /api/kb/ingest            │  ← LearningClaw / ETL 脚本
  │  GET  /api/kb/documents         │  ← Admin UI (管理后台)
  │  PATCH /api/kb/documents/{id}   │  ← Admin UI (审核/发布)
  │                                 │
  └───────────────┬─────────────────┘
                  │
                  ▼
          PostgreSQL (pgvector)
          kb_document / kb_chunk / kb_sop_node
```

**集成边界说明:**

- KB Service 只负责知识的存储与检索，返回 ranked chunks/SOP 节点给调用者
- **Conversation Service** 调用 `/api/kb/search` 后，在 AI 层进行 4-Tier Prompt 组装，详见《12_AI层设计.md》
- **LearningClaw** 调用 `/api/kb/ingest` 将学习成果写入知识库，详见《12_AI层设计.md §第二部分》
- API Gateway 注册 `/api/v1/kb/` 路由转发到 KB Service
- `docker-compose.yml` / Helm values 新增 `kb-service` 服务

## 五、数据存储设计

### 5.1 存储分层策略

采用**改良版混合模式**：文件系统是内容的 Source of Truth（人审核 MD 文件），DB 是状态和索引的 Source of Truth（状态机 + 检索索引），索引层为纯派生（可从 published/ 文件全量重建）。

| 数据层 | Source of Truth | 人可读 | 机可读 | 进 Git | 备注 |
|--------|----------------|--------|--------|--------|------|
| config/ | 文件 | ✅ | ✅ | ✅ | ETL 配置 |
| raw/ | 文件 | ⚠️ HTML | - | ❌ | 原始采集，按需保留 |
| draft/ | 文件 + DB(status=draft) | ✅ | ✅ | ❌ | 审核前暂存 |
| published/ | 文件 + DB(status=published) | ✅ | ✅ | ✅ LFS 可选 | 正式知识库 |
| 索引层 | DB (派生) | - | ✅ | - | 从 published 重建 |
| sop_skills/ | 文件 | ✅ | ✅ | ✅ | 决策树 |
| assets/ | 文件 | ✅ | - | ❌ | 图片，volume 挂载 |

### 5.2 文件系统布局

```text
data-pipeline/
├── config/ # ETL 配置（进 Git）
│ ├── category.json # 分类树（4层, ~260 节点）
│ ├── prompt_template.txt # LLM 分类/元数据增强 prompt
│ ├── synonym_mapping.json # 缩写映射（HCI→超融合等）
│ └── hci_dict.txt # jieba 自定义词典
├── raw/ # 原始数据（.gitignore）
│ └── {id}.json
├── draft/ # AI 生成的 MD，待审核（.gitignore）
│ ├── batch_{date}/ # 按批次组织
│ │ ├── {id}{title}.md
│ │ └── ...
│ └── realtime/ # S9 实时生成
│ └── case{id}_record.md
├── published/ # 审核通过的正式 MD（可选 Git LFS）
│ ├── 虚拟机/
│ ├── 存储/
│ ├── 平台/
│ ├── 硬件/
│ ├── 网络/
│ └── 客户机硬件/
├── sop_skills/ # SOP 决策树（进 Git）
│ ├── registry.json # SOP 技能注册表
│ └── vm_boot_failure/ # 虚拟机开关机失败排障
│ ├── README.md
│ ├── keywords_map.json
│ └── chapters/
│ ├── 01_前置检查.md
│ ├── 04_CPU不足.md
│ └── ...
├── assets/ # 图片（.gitignore）
│ └── {hash}.png
└── scripts/ # ETL 脚本（进 Git）
├── fetcher.py # 案例采集器
├── converter.py # HTML → 结构化 MD
├── enricher.py # LLM 元数据增强
├── image_processor.py # 图片下载 + Vision OCR
├── reviewer_cli.py # 命令行审核工具
└── ingestor.py # MD → DB 导入
```

### 5.3 数据库 Schema

#### kb_document — 知识文档

```sql
CREATE TABLE kb_document (
    id              SERIAL PRIMARY KEY,
    source_id       VARCHAR(50) UNIQUE,           -- 原始案例ID
    title           VARCHAR(500) NOT NULL,
    product         VARCHAR(100) DEFAULT '超融合HCI',
    content_md      TEXT NOT NULL,                 -- MD 全文
    content_hash    VARCHAR(64),                   -- SHA256，变更检测
    yaml_meta       JSONB,                         -- 结构化元数据
    category_l1     VARCHAR(100),                  -- 一级分类
    category_l2     VARCHAR(100),                  -- 二级分类
    tags            TEXT[],                        -- 标签数组
    judgment_logic  TEXT,                          -- 排查逻辑（中文）
    summary         TEXT,                          -- 摘要（中文）
    difficulty      SMALLINT DEFAULT 3,
    status          VARCHAR(20) DEFAULT 'draft',   -- draft/under_review/approved/published/rejected/archived
    review_note     TEXT,                          -- 审核批注
    reviewer        VARCHAR(100),                  -- 审核人
    reviewed_at     TIMESTAMP,
    source_type     VARCHAR(20) DEFAULT 'kb',      -- kb/sop/realtime
    has_images      BOOLEAN DEFAULT FALSE,
    verified_version VARCHAR(50),
    trace_id        VARCHAR(64),
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_kb_document_status ON kb_document(status);
CREATE INDEX idx_kb_document_category ON kb_document(category_l1, category_l2);
CREATE INDEX idx_kb_document_source_id ON kb_document(source_id);
```

#### kb_chunk — 文档分块 + 向量

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE kb_chunk (
    id              SERIAL PRIMARY KEY,
    document_id     INTEGER REFERENCES kb_document(id) ON DELETE CASCADE,
    chunk_index     SMALLINT NOT NULL,             -- 块序号
    content         TEXT NOT NULL,                  -- 块文本
    embedding       vector(384),                    -- 向量 (bge-small-zh: 384维)
    token_count     SMALLINT,
    metadata        JSONB,                          -- 块级元数据（标题层级等）
    tsv             tsvector,                       -- BM25 全文索引
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_kb_chunk_document ON kb_chunk(document_id);
CREATE INDEX idx_kb_chunk_embedding ON kb_chunk USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX idx_kb_chunk_tsv ON kb_chunk USING GIN (tsv);
```

#### kb_sop_node — SOP 决策树节点

```sql
CREATE TABLE kb_sop_node (
    id              SERIAL PRIMARY KEY,
    skill_id        VARCHAR(100) NOT NULL,          -- 技能ID (如 vm_boot_failure)
    node_name       VARCHAR(200) NOT NULL,          -- 节点名称
    parent_id       INTEGER REFERENCES kb_sop_node(id),
    keywords        TEXT[] NOT NULL,                 -- 关键字列表
    file_path       VARCHAR(500),                    -- 对应 MD 文件路径
    content         TEXT,                            -- 章节全文
    level           SMALLINT DEFAULT 1,              -- 层级 (1=主章节, 2=子章节)
    sort_order      SMALLINT DEFAULT 0,
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_kb_sop_node_skill ON kb_sop_node(skill_id);
CREATE INDEX idx_kb_sop_node_keywords ON kb_sop_node USING GIN (keywords);
```

#### kb_category — 分类树

```sql
CREATE TABLE kb_category (
    id              SERIAL PRIMARY KEY,
    parent_id       INTEGER REFERENCES kb_category(id),
    name            VARCHAR(100) NOT NULL,
    level           SMALLINT NOT NULL,               -- 1=L1, 2=L2, 3=L3, 4=L4
    keywords        TEXT[],
    source          VARCHAR(20) DEFAULT 'manual',    -- manual/auto_generated/auto_suggested
    version         VARCHAR(20) DEFAULT '1.0',
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_kb_category_parent ON kb_category(parent_id);
CREATE INDEX idx_kb_category_level ON kb_category(level);
```

#### kb_synonym — 同义词映射

```sql
CREATE TABLE kb_synonym (
    id              SERIAL PRIMARY KEY,
    term            VARCHAR(100) NOT NULL,           -- 缩写/别名
    canonical       VARCHAR(100) NOT NULL,           -- 标准名
    created_at      TIMESTAMP DEFAULT NOW(),
    UNIQUE(term, canonical)
);
```

---

## 六、SOP Skills 机制

### 6.1 概念：SOP 即 Skills

将 SOP 排障手册视为"技能"（类似 Claude 的 skills 机制），每个 SOP 拆分为：

| 组件 | 作用 | 类比 |
|------|------|------|
| README.md | 索引目录，快速定位章节 | Claude skills 目录结构 |
| keywords_map.json | 关键字→章节映射图 | 路由规则 |
| 拆分后的章节 .md | 单个排障流程的完整内容 | 单个 skill 文件 |
| registry.json | 技能注册表，管理多个 SOP | 技能清单 |

### 6.2 现有 SOP 数据结构

已从 排障手册优化版_v2 验证：

- **来源**: 《虚拟机开关机失败排障手册》（完整 DOCX → 拆分 MD）
- **规模**: 23 主章节 + 14 "内部异常"子章节 = 37 个叶子节点
- **keywords_map.json**: 24 个一级条目 + 13 个子条目，~150 个关键字
- **覆盖场景**: CPU不足、内存不足、内部异常(14种子类)、序列号过期、镜像忙、存储不可访问等

### 6.3 SOP 匹配算法

```python
def match_sop(user_query: str, keywords_map: dict) -> MatchResult:
    """
    遍历 keywords_map，对用户原文做关键字包含匹配。
    支持一级匹配和二级匹配（内部异常子章节）。
    子章节命中加分（更精确的匹配优先）。
    """
    best_match = None
    best_score = 0.0
    
    for chapter, config in keywords_map.items():
        # 一级匹配
        for kw in config["keywords"]:
            if kw.lower() in user_query.lower():
                score = len(kw) / len(user_query)  # 关键字覆盖率
                if score > best_score:
                    best_match = config["file"]
                    best_score = score
        
        # 二级匹配（子章节，如"内部异常"下的14种子类型）
        if "subchapters" in config:
            for sub_name, sub_config in config["subchapters"].items():
                for kw in sub_config["keywords"]:
                    if kw.lower() in user_query.lower():
                        score = len(kw) / len(user_query) + 0.1  # 子章节额外加分
                        if score > best_score:
                            best_match = sub_config["file"]
                            best_score = score
    
    if best_match:
        match_type = "exact" if best_score > 0.3 else "fuzzy"
    else:
        match_type = "none"
    
    return MatchResult(file=best_match, score=best_score, match_type=match_type)
```

### 6.4 SOP 不命中时的 RAG 兜底

当 SOP 无法精确匹配时，自动降级到混合检索引擎：

- **BM25 检索**: jieba 分词 → tsvector 全文匹配 → top-20
- **向量检索**: z.ai embedding → pgvector cosine → top-20
- **RRF 融合**: score = Σ 1/(k + rank_i), k=60
- **Rerank**: 取 top-5, LLM 相关性评分过滤 < 0.5 的结果
- 若 top-1 score < threshold → 回答 "未找到相关知识，建议联系 400"

### 6.5 SOP 扩展规范

新增 SOP 手册时，按以下结构注册：

```text
sop_skills/
├── registry.json              # 技能注册表
├── vm_boot_failure/           # 已有: 虚拟机开关机失败
│   ├── README.md
│   ├── keywords_map.json
│   └── chapters/
├── storage_failure/           # [未来] 存储故障
│   ├── README.md
│   ├── keywords_map.json
│   └── chapters/
└── network_failure/           # [未来] 网络故障
    ├── README.md
    ├── keywords_map.json
    └── chapters/
```

**registry.json 示例:**

```json
{
  "skills": [
    {
      "id": "vm_boot_failure",
      "name": "虚拟机开关机失败排障",
      "scope": ["虚拟机开机失败", "虚拟机关机失败"],
      "l1_category": "虚拟机",
      "keywords_map": "vm_boot_failure/keywords_map.json",
      "version": "2.0",
      "source": "虚拟机开关机失败排障手册.pdf",
      "chapters_count": 37
    }
  ]
}
```

---

## 七、检索引擎设计

### 7.1 LLM 分类器

| 参数 | 值 |
|------|---|
| 输入 | 用户问题 + kb_category 树（L1/L2 名称+关键字） |
| 输出 | { l1: str, l2: str, confidence: float } |
| 追问条件 | confidence < 0.7 |
| 最大追问轮数 | 3 |
| 追问方式 | 生成澄清问题（如 "请问是虚拟机开机失败还是虚拟机卡慢？"） |

分类器作用：缩小检索范围。确定 L1/L2 后，BM25 和 Vector 检索加入 category 过滤条件，避免跨域误召回。

### 7.2 BM25 全文检索

| 组件 | 技术选型 |
|------|---------|
| 分词器 | jieba + HCI 自定义词典 |
| 索引 | PostgreSQL tsvector + GIN 索引 |
| 查询 | tsquery 构建，支持 AND/OR |
| 自定义词典 | 包含 HCI 专业术语（如 "虚拟存储", "kvm_intel", "peth0", "acli"） |

**jieba 自定义词典示例 (hci_dict.txt):**

```text
虚拟存储 5 n
虚拟机镜像 5 n
kvm_intel 5 eng
peth0 5 eng
pflash 3 eng
嵌套虚拟化 5 n
集群锁 4 n
```

### 7.3 向量检索

| 参数 | 值 |
|------|---|
| Embedding 主力 | z.ai API |
| Embedding 降级 | bge-small-zh-v1.5 (本地, 384维) |
| 向量索引 | pgvector IVFFlat (lists=100, cosine) |
| 检索方式 | ORDER BY embedding <=> query_embedding LIMIT 20 |

**双模式切换逻辑：**

```python
async def get_embedding(text: str) -> list[float]:
    try:
        return await zai_embedding(text)  # 主力: z.ai API
    except (httpx.TimeoutException, httpx.HTTPStatusError):
        logger.warning("z.ai embedding 降级到本地 bge-small")
        return local_bge_embedding(text)  # 降级: 本地模型
```

### 7.4 RRF 融合

Reciprocal Rank Fusion 将 BM25 和 Vector 两路结果融合：

```python
def rrf_fusion(bm25_results: list, vector_results: list, k: int = 60) -> list:
    """
    RRF 融合公式: score(d) = Σ 1/(k + rank_i(d))
    k=60 是经验值，平衡两路权重
    """
    scores = defaultdict(float)
    
    for rank, doc in enumerate(bm25_results):
        scores[doc.id] += 1.0 / (k + rank + 1)
    
    for rank, doc in enumerate(vector_results):
        scores[doc.id] += 1.0 / (k + rank + 1)
    
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)
```

### 7.5 Reranker

| 参数 | 值 |
|------|---|
| 输入 | 用户问题 + top-5 候选 chunk |
| 评分 | LLM 为每个 chunk 打 0-1 相关性分 |
| 过滤 | score < 0.5 → 丢弃 |
| 全空判定 | 所有 chunk score < 0.5 → "未找到相关知识" |

---

## 八、数据管道设计

### 8.1 项目整合方案

将 AItroubleshooting 项目的核心 ETL 能力整合到 hci-troubleshoot-platform 的 data-pipeline/ 模块：

| 来源 (AItroubleshooting) | 目标 (data-pipeline/) | 改进 |
|--------------------------|----------------------|------|
| knowledge_base_etl.py (794行单文件) | scripts 拆分为多模块 | 模块化、可测试 |
| config/category.json | config/category.json | 直接复用 |
| config/llm_prompt_template.txt | config/prompt_template.txt | 修复缺陷（见§8.3） |
| config/synonym_mapping.json | config/synonym_mapping.json | 直接复用 |
| data/raw_json/ (~700) | raw/ (目标 7000+) | 扩展采集范围 |
| data/markdown/ (~600) | 经审核后 → published/ | 需人工审核 |
| data/assets/ | assets/ | 补全 OCR |
| AItroubleshooting 项目 | 归档，不再维护 | — |

### 8.2 ETL 模块化设计

从 794 行单文件重构为：

```text
data-pipeline/scripts/
├── fetcher.py          # CaseFetcher — 从 support API 采集，支持并发+断点续传
├── converter.py        # CaseConverter — HTML → 结构化 MD
├── enricher.py         # LLM 元数据增强 — 分类/标签/摘要/judgment_logic
├── image_processor.py  # 图片下载 + Vision OCR
├── reviewer_cli.py     # 命令行审核工具（批量浏览/通过/拒绝）
├── ingestor.py         # 审核通过的 MD → KB Service 入库
├── pipeline.py         # 主编排脚本（串联上述步骤）
└── utils/
    ├── registry.py     # KnowledgeRegistry（从 AItroubleshooting 迁移）
    └── html_cleaner.py # HTML 清洗工具
```

### 8.3 Prompt 模板修复方案

当前 llm_prompt_template.txt 存在的缺陷及修复：

| # | 缺陷 | 修复 |
|---|------|------|
| 1 | 两个重复的 Output Format 段（第 1 个在第 38 行，第 2 个在末尾） | 合并为一个 |
| 2 | 两个 {taxonomy_context} 占位符 | 保留第二个（带详细指引的），删除第一个 |
| 3 | 两个编号为 6 的字段（第一个是 Judgment Logic，第二个也是 6） | 重新编号 |
| 4 | 输出语言为英文 | 改为中文输出指令（summary 和 judgment_logic 必须中文） |
| 5 | 无 few-shot 示例 | 添加 3 个高质量中文示例 |
| 6 | judgment_logic 格式无约束 | 约束为 "1. 检查[对象]的[指标]; 2. 确认[条件]是否成立" |

### 8.4 judgment_logic 重生成方案

| 项目 | 详情 |
|------|------|
| 范围 | 全量 7000 案例（包含已有 600 + 新增 ~6400） |
| 模型 | glm-4-flash（低成本，~0.02元/次） |
| 成本 | ~140 元 |
| 耗时 | 并发处理约 4-6 小时 |
| 质量保障 | 修复 prompt → 小批量测试(50例) → 人工抽检 → 全量执行 |
| 输出格式 | 中文，"1. 检查[对象]的[指标]; 2. 确认[条件]" |

---

## 九、category.json 复用与扩展

### 9.1 现有分类树概况

| 维度 | 数值 |
|------|------|
| L1 类别 | 6 个（平台、网络、存储、硬件、客户机硬件、虚拟机） |
| L2 类别 | ~50 个（人工定义 + auto_generated 混合） |
| L3+ 叶子节点 | ~200+ |
| 总关键字 | ~500+ |
| source 标记 | manual / auto_generated |

### 9.2 复用方案

```text
流程:
  1. 首次导入: category.json → kb_category 表（保留层级关系 + source 标记）
  2. LLM 分类时: 从 kb_category 表加载树 → 作为 taxonomy_context 注入 prompt
  3. 新案例分类:
     命中现有节点 → 直接关联
     LLM 建议新子类 → 标记 source="auto_suggested", 待人工审核
  4. 人工维护: Admin 管理接口支持节点 CRUD + 批量重分类
```

### 9.3 扩展性机制

| 需求 | 机制 |
|------|------|
| 新增 L1 类别 | Admin 管理接口 → kb_category 新增根节点 → 触发分类器 prompt 缓存刷新 |
| 新增 L2/L3 | LLM 自动建议 + source="auto_suggested" → 人工审核通过后 source="manual" |
| 词表版本管理 | kb_category.version 字段 + 全局 category_version |
| 全量重分类 | 管理接口触发 → 遍历所有案例重跑分类器 |
| 同义词扩展 | kb_synonym 表(term, canonical) + jieba 自定义词典同步 |

---

## 十、PageIndex 对比分析

### 10.1 PageIndex (VectifyAI) 概述

PageIndex 是一个无向量、纯 LLM 推理的 RAG 框架，核心理念："Similarity ≠ Relevance"。

| 维度 | PageIndex | 本项目方案 |
|------|-----------|-----------|
| 索引方式 | LLM 逐节点生成摘要 → 树形索引 | Embedding + BM25 双路索引 |
| 检索方式 | LLM 从根节点推理遍历树 → 定位叶子 | 关键字精确匹配 + 向量/全文混合检索 |
| 核心理念 | 用 LLM 推理替代向量相似度 | SOP 确定性路由 + 语义兜底 |
| 文档结构 | PDF/MD → 自动检测 TOC → 生成树 | MD → YAML frontmatter + 正文分段 |
| 向量依赖 | 无（纯 LLM） | 有（pgvector） |

### 10.2 适用性评估

**借鉴点:**

- 树形索引思路 → 用于 SOP skill 的层级路由（keywords_map.json）
- 可解释检索路径 → 返回 l1 > l2 > 章节名 命中路径给用户
- "不是所有问题都需要向量" → SOP 精确匹配不走向量

**不采用的原因:**

- 索引成本极高（每节点 LLM 调用，7000 文档不可接受）
- 不理解 SOP 流程依赖（步骤 1→2→3 的顺序逻辑）
- 无增量更新能力
- 强 OpenAI 依赖，中文 HCI 领域适配差

### 10.3 决策

不采用 PageIndex，借鉴其树形可解释性思路，结合 keywords_map 确定性路由实现同等效果，且零 LLM 开销。

---

## 十一、技术选型汇总

| 组件 | 选型 | 备选/降级 | 理由 |
|------|------|----------|------|
| Embedding | z.ai API | bge-small-zh-v1.5 (本地, 384维) | z.ai 与现有 AI 服务一致；bge-small 作为网络故障降级 |
| 向量存储 | pgvector (PostgreSQL) | - | 复用现有 PostgreSQL，60K 向量完全胜任 |
| 向量索引 | IVFFlat (lists=100, cosine) | HNSW (未来规模增长时) | IVFFlat 构建快，60K 规模够用 |
| BM25 | PostgreSQL tsvector + GIN | Elasticsearch (未来) | 复用现有 DB，避免新增组件 |
| 中文分词 | jieba + HCI 自定义词典 | - | 成熟、可控、支持自定义词典 |
| 文本分块 | RecursiveCharacterTextSplitter | - | 512 tokens, 128 overlap |
| LLM 分类器 | z.ai GLM | - | 与对话使用同一模型 |
| Reranker | LLM 评分 (0-1) | Cross-encoder (未来) | 初版用 LLM，规模大时换专用模型 |
| SOP 匹配 | keywords_map.json 确定性路由 | - | 精确、零成本、可解释 |

---

## 附录

### 附录 A: keywords_map.json 结构参考

```json
{
  "CPU不足": {
    "keywords": ["CPU不足", "剩余可配置CPU不足", "此主机剩余可配置CPU不足"],
    "file": "04_CPU不足.md"
  },
  "内部异常": {
    "keywords": ["内部异常", "内部异常，请稍后重试"],
    "file": "06_内部异常.md",
    "subchapters": {
      "KVM驱动缺失": {
        "keywords": ["failed to initialize KVM", "kvm: disable by bios", "kvm_intel"],
        "file": "03_内部异常/0302_KVM驱动缺失.md"
      }
    }
  }
}
```

完整 keywords_map.json 包含 24 个一级条目 + 13 个"内部异常"子条目，~150 个关键字。

### 附录 B: category.json 树形结构摘要

```text
云计算 (根)
├── 平台 (L1)
│   ├── 物理主机 (L2): 主机安装/扩容/删除/离线重启/分区空间不足
│   ├── 系统管理 (L2): 授权管理/账号密码/版本升级/补丁升级/集群检测
│   ├── 虚拟存储 (L2, auto)
│   ├── 虚拟网络 (L2, auto)
│   └── ... (13 个 L2 节点)
├── 网络 (L1)
│   ├── 物理网络 (L2): 网口创建/复用/编辑/删除/丢包
│   ├── 虚拟网络 (L2): 网络设备/防火墙/流量镜像/连通性
│   └── ... (7 个 L2 节点)
├── 存储 (L1)
│   ├── 虚拟存储 (L2): 创建/删除/扩容/缩容/磁盘管理/性能/功能特性
│   ├── FC / ISCSI / NFS / 本地存储 (L2)
│   └── ...
├── 硬件 (L1)
│   ├── 风扇/电源/主板/CPU/内存/网卡/硬盘/BIOS/RAID卡/IPMI
│   └── ... (15 个 L2 节点)
├── 客户机硬件 (L1)
│   └── 盒子开机/启动/死机/USB故障
└── 虚拟机 (L1)
    ├── 创建/删除/开机/关机/重启/导入/导出/克隆/挂起
    ├── 模板/迁移/卡慢/操作系统/网络/备份/快照
    └── ... (40+ 个 L2 节点，含大量 auto_generated)
```

### 附录 C: 新增 SQL DDL 汇总

见 §5.3 全部 5 张表的 CREATE 语句。需在 init_schema.sql 中追加：

- `CREATE EXTENSION IF NOT EXISTS vector;`
- kb_document 表
- kb_chunk 表（含向量列和 IVFFlat 索引）
- kb_sop_node 表
- kb_category 表
- kb_synonym 表
