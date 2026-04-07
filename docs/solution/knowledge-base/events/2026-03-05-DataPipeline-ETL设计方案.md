---
status: active
category: solution
audience: developer
last_updated: 2026-04-07
owner: team
---

# Data-Pipeline ETL 设计方案（已归档）

> **归档说明**：本方案为早期设计的知识库 ETL 数据管道方案，包含 data-pipeline 目录结构和 ETL 脚本设计。
> **现行方案**：知识摄入已简化为 KB Service 的 `/api/kb/ingest` 接口，ETL 脚本不再独立维护。
> **归档日期**：2026-04-07

---

## 一、方案背景

为 HCI 智能排障平台构建知识库检索增强生成（RAG）系统，需要将 ~7000 在网知识库案例进行结构化处理、存储与智能检索。

### 数据源分析

| # | 数据源 | 规模 | 格式 | 质量 |
|---|--------|------|------|------|
| 1 | 在网知识库（support.sangfor.com.cn） | ~7000 案例 | Web 页面 → HTML JSON | 70-80% CSS 噪声 |
| 2 | 已处理 Markdown | ~600 案例 | YAML frontmatter + 结构化 MD | 混合质量 |
| 3 | SOP 排障手册 | 1 本（37 章节） | keywords_map.json + 拆分 MD | 格式丢失 |

---

## 二、文件系统布局（原方案）

```text
data-pipeline/
├── config/                    # ETL 配置（进 Git）
│   ├── category.json          # 分类树（4层, ~260 节点）
│   ├── prompt_template.txt    # LLM 分类/元数据增强 prompt
│   ├── synonym_mapping.json   # 缩写映射
│   └── hci_dict.txt           # jieba 自定义词典
├── raw/                       # 原始数据（.gitignore）
├── draft/                     # AI 生成的 MD，待审核（.gitignore）
├── published/                 # 审核通过的正式 MD
├── sop_skills/                # SOP 决策树（进 Git）
├── assets/                    # 图片（.gitignore）
└── scripts/                   # ETL 脚本（进 Git）
    ├── fetcher.py             # 案例采集器
    ├── converter.py           # HTML → 结构化 MD
    ├── enricher.py            # LLM 元数据增强
    ├── image_processor.py     # 图片下载 + Vision OCR
    ├── reviewer_cli.py        # 命令行审核工具
    └── ingestor.py            # MD → DB 导入
```

---

## 三、ETL 模块化设计

```text
data-pipeline/scripts/
├── fetcher.py             # CaseFetcher — 从 support API 采集
├── converter.py           # CaseConverter — HTML → 结构化 MD
├── enricher.py            # LLM 元数据增强 — 分类/标签/摘要
├── image_processor.py     # 图片下载 + Vision OCR
├── reviewer_cli.py        # 命令行审核工具
├── ingestor.py            # 审核通过的 MD → KB Service 入库
└── pipeline.py            # 主编排脚本
```

---

## 四、为什么不再采用此方案

| 问题 | 说明 |
|------|------|
| 架构复杂 | 独立 ETL 管道维护成本高，与 KB Service 双写 |
| 职责不清 | knowledge 生产与检索边界模糊 |
| 实际需求简化 | 知识来源稳定，不需要复杂 ETL 流程 |

---

## 五、现行方案

知识摄入通过 KB Service 统一接口：

```
POST /api/kb/ingest
{
  "support_id": "12345",
  "title": "...",
  "content_md": "...",
  "category_id": "虚拟机-003"
}
```

- **SOP 导入**：`POST /api/kb/sop/ingest`
- **分类树管理**：`GET/POST /api/kb/categories`

---

*归档日期: 2026-04-07*