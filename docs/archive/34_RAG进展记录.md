# HCI智能排障平台 - RAG知识库进展

更新时间: 2026-03-02

---

## 一、进展概览

| Phase | 名称 | 状态 | 预估工时 | 依赖 |
|-------|------|------|---------|------|
| 设计 | RAG 系统设计 | ✅ 已完成 | — | — |
| A | 数据清洗 | ⏳ 未开始 | 3天 | — |
| B | 数据库 + 基础设施 | ⏳ 未开始 | 2天 | — |
| C | 知识导入 | ⏳ 未开始 | 3天 | A, B |
| D | 检索引擎 | ⏳ 未开始 | 4天 | B, C |
| E | 集成 | ⏳ 未开始 | 2天 | D |
| F | 验证 + 调优 | ⏳ 未开始 | 2天 | E |

**总计约 16 天**

---

## 二、关键决策记录

| # | 决策项 | 结论 | 讨论日期 |
|---|--------|------|---------|
| 1 | 检索架构 | Agentic RAG (C) + 混合检索 (B) 组合 | 2026-03-02 |
| 2 | SOP 机制 | Skills 模式: README + keywords_map + 章节文件，确定性路由优先 | 2026-03-02 |
| 3 | judgment_logic | 修复 prompt 后重新生成中文版（方案 A） | 2026-03-02 |
| 4 | category.json | 复用到 kb_category 表 + 版本管理 + LLM 自动建议/人工审核 | 2026-03-02 |
| 5 | 数据存储 | 改良混合模式：文件为内容 SoT，DB 为状态/索引 SoT，索引可重建 | 2026-03-02 |
| 6 | 项目整合 | AItroubleshooting ETL 整合到 data-pipeline/ 模块，原项目归档 | 2026-03-02 |
| 7 | PageIndex | 不采用，借鉴树形可解释性思路 | 2026-03-02 |
| 8 | Embedding | z.ai API 主力 + bge-small-zh 本地降级 | 2026-03-02 |
| 9 | 审核机制 | 所有 MD 必须人工审核后才能发布（Admin UI 支持浏览/批注/编辑/通过/拒绝） | 2026-03-02 |
| 10 | 数据范围 | 目标 7000+ 案例（非仅 600 已处理），含在网全量 KB | 2026-03-02 |

---

## 三、实施计划

### Phase A: 数据清洗（3天）

**目标**: 修复 ETL 管道，全量处理 7000+ 案例为标准化 MD

| # | 任务 | 详情 | 产出 |
|---|------|------|------|
| A1 | 整合 ETL 到 data-pipeline/ | 从 AItroubleshooting 迁移核心逻辑，拆分为模块化架构 | `data-pipeline/scripts/` 多模块 |
| A2 | 修复 prompt 模板 | 去重 Output Format/taxonomy_context，全中文指令，加 3 个 few-shot | `config/prompt_template.txt` |
| A3 | 小批量验证 | 50 案例测试新 prompt → 人工抽检 judgment_logic 质量 | 质量报告 |
| A4 | 全量 ETL 执行 | 7000+ 案例并发处理（采集→清洗→OCR→LLM增强→输出MD） | `draft/batch_{date}/` 7000+ MD |
| A5 | SOP 格式修复 | 恢复 37 个 SOP 章节的 markdown 格式（标题/换行/代码块） | `sop_skills/vm_boot_failure/chapters/` |
| A6 | 补全 Vision OCR | 对未处理图片补充 Vision OCR 分析 | 更新 assets/ 和 MD 中的图片描述 |

**依赖**: 无前置依赖，可立即开始

### Phase B: 数据库 + 基础设施（2天）

**目标**: 搭建 KB Service 骨架和数据库表

| # | 任务 | 详情 | 产出 |
|---|------|------|------|
| B1 | 数据库 Schema | `init_schema.sql` 追加 pgvector 扩展 + 5 张新表 | SQL DDL |
| B2 | KB Service 脚手架 | FastAPI app + config + health + Dockerfile + requirements.txt | `backend/kb-service/` |
| B3 | Docker 注册 | docker-compose.yml 新增 kb-service (port 8004) | docker-compose.yml |
| B4 | API Gateway 路由 | 注册 `/api/v1/kb/` 路由到 KB Service | `routes/kb.py` |

**依赖**: 无前置依赖，可与 Phase A 并行

### Phase C: 知识导入（3天）

**目标**: 将审核通过的 MD 和 SOP 数据导入 KB Service

| # | 任务 | 详情 | 产出 |
|---|------|------|------|
| C1 | 文档导入器 | 解析 MD (YAML frontmatter + 正文) → kb_document 表 | `ingestor.py` |
| C2 | 文本分块 | RecursiveCharacterTextSplitter (512 tokens, 128 overlap) → kb_chunk | `chunker.py` |
| C3 | Embedding 生成 | 双模式 (z.ai API + bge-small 降级) → kb_chunk.embedding | `embedding.py` |
| C4 | BM25 索引生成 | jieba + HCI 词典分词 → tsvector → GIN 索引 | `jieba_hci.py` |
| C5 | SOP 导入 | registry.json + keywords_map.json → kb_sop_node 表 | `sop_loader.py` |
| C6 | 分类树导入 | category.json → kb_category 表 (保留层级 + source) | `category_loader.py` |

**依赖**: A（数据就绪）+ B（DB + 服务就绪）

### Phase D: 检索引擎（4天）

**目标**: 实现完整的 Agentic RAG 查询链路

| # | 任务 | 详情 | 产出 |
|---|------|------|------|
| D1 | LLM 分类器 | 输入问题 + kb_category 树 → {l1, l2, confidence}，追问逻辑 | `classifier.py` |
| D2 | SOP 匹配器 | 加载 kb_sop_node → 关键字匹配 → {file, score, match_type} | `sop_matcher.py` |
| D3 | 混合检索引擎 | BM25 top-20 + Vector top-20 → RRF 融合(k=60) → top-5 | `search_engine.py` |
| D4 | Reranker | LLM 相关性评分 0-1，过滤 <0.5，全空则"未找到" | `reranker.py` |
| D5 | 统一检索路由 | `/api/kb/search` — classify → sop_match → hybrid → rerank | `routes/search.py` |

**依赖**: B（服务骨架）+ C（数据已索引）

### Phase E: 集成（2天）

**目标**: KB Service 与 Conversation Service 对接，实现知识注入对话

| # | 任务 | 详情 | 产出 |
|---|------|------|------|
| E1 | KB 客户端 | Conversation Service 新增 HTTP 客户端调用 KB Service | `kb_client.py` |
| E2 | 上下文组装 | 4-Tier Prompt 组装 (System + SOP + RAG + History) | `assemble_context()` |
| E3 | 端到端联调 | 用户提问 → 分类 → SOP/RAG → AI 回答，全链路验证 | 联调报告 |

**依赖**: D（检索引擎就绪）

### Phase F: 验证 + 调优（2天）

**目标**: 验证检索质量，调优参数

| # | 任务 | 详情 | 产出 |
|---|------|------|------|
| F1 | 测试集构建 | 50 个标准问题（覆盖 SOP精确/模糊/纯RAG/无答案 4 种场景） | 测试用例集 |
| F2 | 精度评估 | 自动化评估脚本，对比预期答案 vs 实际答案 | 评估报告 |
| F3 | 参数调优 | RRF k 值、Reranker 阈值、分块大小、top-K | 最优参数配置 |
| F4 | "我不知道"边界 | 校准 confidence 阈值，确保无答案时不幻觉 | 阈值配置 |
| F5 | 可观测性 | 全链路 trace_id，检索耗时/命中率/LLM 调用次数 Prometheus 指标 | 监控面板 |

**依赖**: E（全链路就绪）

---

## 四、Phase A: 数据清洗

状态: ⏳ 未开始

> 待启动后在此记录进展

---

## 五、Phase B: 数据库 + 基础设施

状态: ⏳ 未开始

> 待启动后在此记录进展

---

## 六、Phase C: 知识导入

状态: ⏳ 未开始

> 待启动后在此记录进展

---

## 七、Phase D: 检索引擎

状态: ⏳ 未开始

> 待启动后在此记录进展

---

## 八、Phase E: 集成

状态: ⏳ 未开始

> 待启动后在此记录进展

---

## 九、Phase F: 验证 + 调优

状态: ⏳ 未开始

> 待启动后在此记录进展