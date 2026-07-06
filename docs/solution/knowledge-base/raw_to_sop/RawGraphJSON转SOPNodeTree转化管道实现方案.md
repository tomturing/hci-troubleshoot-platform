# Raw Graph JSON → SOPNode Tree 转化管道实现方案

## 背景与目标

### 外部格式说明（关键约束）

`data-pipeline/raw/*.json`（如 `内存ECC故障.json`）是**外部 AI Pipeline 生产的文件**，与本项目代码**零耦合**，仅放置在 `raw/` 目录作为输入原料。其 Schema 由外部定义，本项目无控制权。

现有两类知识资产：
- **Raw Graph JSON**（外部导入，`data-pipeline/raw/*.json`）：由外部 AI 从真实案例提取的状态机图，包含 `flow`（主干分流）+ `branches`（子诊断图），保留置信度元数据与溯源信息。
- **DB SOPNode Tree**（`sop_document.tree_json`）：经过人工审校的标准多叉决策树，已接入 Agent 框架的 Variable Gate、ReAct 引擎等所有上层能力。

**目标**：新增**独立的 ETL 转化工具** `data-pipeline/raw_to_sop/`，将外部 Raw Graph JSON 无痛转化为符合 `SOPNode` 规范的 Markdown 文档，再通过现有的 `POST /api/sop/ingest` API 链路写入数据库，最终由 `sop_parser` 解析生成 `tree_json`。

> [!IMPORTANT]
> **架构原则**：转化工具是一个独立的单向 ETL 工具（读取外部文件 → 调用项目 API），**不**集成进 `kbd/` 的 KBD 生产管道（fetch/vision/import/classify），**不**依赖 `kbd/` 模块的任何内部代码（如 `config.py`、`pipeline.py`）。

---

## 第一性原理推导的设计关键决策

### 为什么不直接生成 tree_json？

现有管道的黄金路径是：
```
Markdown 文档 → sop_parser → tree_json → DB
```
`sop_parser` 内含叶优先锚定算法和宽松校验，是 SOPNode 的唯一可信生成器。如果绕过它直接写 `tree_json`，会：
1. 绕过 `validation_issues` 校验，引入静默错误
2. `id`、`line_number` 等字段需要手动计算，维护成本极高
3. 未来 schema 变更时需要双向同步

**结论**：目标产物是**结构化的 Markdown 文档**，而不是 JSON。

### 映射关系（核心设计契约）

| Raw Graph 元素 | SOPNode Markdown 对应 |
|---|---|
| `meta.node_name` | `# H1` 根标题（根节点） |
| `branch.branch_name` | `## H2` 或 `### H3` 子节点标题 |
| `branch.routing_signals` | `#### 前置检查` 段落内容 |
| `branch.checks` (all pass) | `#### 判断方法` → `acli_methods` |
| `branch.solution_steps` | `#### 解决方案` → `quick_recovery` |
| `branch.temporary_workaround` | `#### 解决方案` → `quick_recovery` |
| `branch.permanent_fix` | `#### 解决方案` → `thorough_fix` |
| `branch.root_causes` | `#### 判断方法` 末尾的 `**根因**：` 段落 |
| `branch.source_case_count` | `<!-- source_cases: N -->` HTML 注释（保留溯源） |
| `flow[].command_example` | H2 节点下的 `#### 前置检查` 中的命令 |

### 树结构化策略（Graph → Tree 的核心算法）

Raw Graph 是 **flow 主干 + branches 子图** 结构，需映射为纯树。

**算法**：Flow 步骤作为树的中间路由节点（`prerequisite_items` 载体），Branches 作为叶子节点（`diagnosis + solution` 载体）。

```
Raw Graph:
  flow[entry-1] ──→ branch-B (条件1)
                ──→ branch-F (条件2)
                ──→ flow[entry-2] (no_match)
                      ──→ branch-E (条件3)
                      ──→ flow[entry-3] (no_match)
                            ──→ branch-D (条件4)
                            ...

生成 Markdown 树:
  # 内存ECC故障                          ← 根节点 (n-1)
  ## 历史告警残留(branch-A)              ← 直接叶节点 (entry-5 末尾兜底)
  ## BIOS配置缺失(branch-B)             ← 直接叶节点 (entry-1条件1)
  ## 630R2未重启(branch-C)              ← 直接叶节点 (entry-3条件)
  ## 内存CE硬件错误(branch-D)           ← 直接叶节点 (entry-4条件)
  ## 内存UE严重故障(branch-E)           ← 直接叶节点 (entry-2条件)
  ## 瞬间复位导致重启(branch-F)          ← 直接叶节点 (entry-1条件2)
  ## eth5亚健康隔离(branch-G)           ← 直接叶节点 (entry-1条件3)
```

**关键洞察**：Raw 格式中，`flow` 是诊断的"前置过滤链"，但对 SOPNode 来说，这些过滤条件应该下沉为每个 branch/叶节点的 `prerequisite_items`，而不是产生中间路由节点。这样可以避免生成不必要的中间层。

---

## 方案详细说明

### 架构图

```
data-pipeline/
├── raw/
│   └── 内存ECC故障.json      ← [EXTERNAL] 外部导入，只读，不属于项目代码
│
└── raw_to_sop/              ← [NEW] 独立 ETL 工具目录（不依赖 kbd/ 任何代码）
    ├── __init__.py
    ├── converter.py         ← 核心转化逻辑（Graph → Markdown）
    │   ├── RawGraphAnalyzer     ← Phase 1：图结构分析，建立索引
    │   ├── PrerequisiteBuilder  ← Phase 2：前置条件聚合
    │   └── MarkdownSynthesizer  ← Phase 3：Markdown 文档生成
    ├── ingestor.py          ← 调用 POST /api/sop/ingest API（独立 httpx 客户端）
    ├── config.py            ← 独立配置（KB_SERVICE_URL, INTERNAL_API_TOKEN）
    ├── .env.example         ← 配置示例
    └── __main__.py          ← CLI 入口 python -m raw_to_sop
```

> [!NOTE]
> 与 `kbd/` 完全独立：两者共享的只有 HTTP API 契约（`POST /api/sop/ingest`），代码层面无任何 `import` 关系。

---

## 变更详情

### [NEW] `data-pipeline/raw_to_sop/converter.py`

**职责**：将外部 Raw Graph JSON 纯规则转化为符合 SOPNode 规范的 Markdown 文档（无 LLM 调用，无数据库依赖，纯 Python 计算）

**核心数据转化逻辑（分三阶段）**：

#### Phase 1：图结构分析（Graph Analysis）
解析 Raw JSON，建立以下索引：
- `branch_map`: `{branch_id → branch_data}` 快速查找
- `flow_to_branch_map`: `{entry_id → [(when_condition, branch_id), ...]}` 建立 flow 到 branch 的路由关系
- `branch_reachability`: 计算每个 branch 通过何种 flow 路径可达，提取其进入条件

#### Phase 2：前置条件合并（Prerequisite Aggregation）
对每个 branch：
1. 从 `branch.routing_signals` 提取直接路由信号（作为该叶节点的基本前置条件）
2. 从 `flow_to_branch_map` 查找到达该 branch 的 `when` 条件（更精确的语义路由条件）
3. 从 `branch.prerequisites` 提取手动声明的前置条件
4. 三路合并为 `prerequisite_items` → 以 `####前置检查` 段落写入

#### Phase 3：Markdown 生成（Markdown Synthesis）
按照 `sop_template_rules.yaml` 定义的关键词约定，生成标准的 SOP Markdown：
- 根节点 → `# {node_name}`
- 每个 branch → `## {branch_name}`（直接挂根下，扁平化结构）
  - `#### 前置检查`（路由信号 + flow条件）
  - `#### 判断方法`（checks 系列命令聚合）
  - `#### 解决方案`（solution_steps 聚合）

```python
# 核心转化类概览（伪代码）

class RawGraphToMarkdown:
    def __init__(self, raw_data: dict):
        self.meta = raw_data["meta"]
        self.flow = raw_data.get("flow", [])
        self.branches = raw_data.get("branches", [])
        self._branch_map = {b["branch_id"]: b for b in self.branches if b.get("branch_id")}
        self._flow_entry_conditions = self._build_flow_entry_conditions()

    def _build_flow_entry_conditions(self) -> dict[str, list[str]]:
        """从 flow 中为每个 branch 提取其进入条件"""
        conditions: dict[str, list[str]] = {}
        for step in self.flow:
            for branch_ref in step.get("branches", []):
                bid = branch_ref.get("goto_branch_id")
                when = branch_ref.get("when", "")
                if bid and when:
                    conditions.setdefault(bid, []).append(when)
        return conditions

    def _branch_to_markdown_section(self, branch: dict, heading_level: int = 2) -> str:
        """将单个 branch 转为标准 SOP Markdown 段落"""
        heading = "#" * heading_level
        lines = [f"{heading} {branch['branch_name']}"]
        
        # 前置检查（路由条件 + 入口 when 条件 + 声明前置条件）
        prereqs = self._collect_prerequisites(branch)
        if prereqs:
            lines.append("\n#### 前置检查\n")
            for p in prereqs:
                lines.append(f"- {p}")

        # 判断方法（从 checks 聚合诊断命令）
        diagnosis = self._collect_diagnosis(branch)
        if diagnosis:
            lines.append("\n#### 判断方法\n")
            lines.extend(diagnosis)

        # 解决方案（solution_steps → quick_recovery + thorough_fix）
        quick, thorough = self._collect_solution(branch)
        lines.append("\n#### 解决方案\n")
        if quick:
            lines.append("**快速恢复**：\n")
            lines.extend(quick)
        if thorough:
            lines.append("\n**彻底解决**：\n")
            lines.extend(thorough)

        return "\n".join(lines)

    def to_markdown(self) -> str:
        """生成完整 SOP Markdown 文档"""
        parts = [f"# {self.meta['node_name']}"]
        for branch in self.branches:
            if branch.get("branch_id"):
                parts.append(self._branch_to_markdown_section(branch))
        return "\n\n---\n\n".join(parts)
```

**字段映射细节**：

| `branch` 字段 | Markdown 映射策略 | 说明 |
|---|---|---|
| `routing_signals` | `#### 前置检查` 中的 `-` 列表 | 触发该分支的信号词 |
| `prerequisites` | `#### 前置检查` 追加 | 手动声明的前置条件 |
| `flow_entry_conditions` | `#### 前置检查` 追加（标注"*入口条件"） | 从 flow 中提取的精确进入条件 |
| `checks[].command_example` | `#### 判断方法` 的代码块 | 使用 `` ``` `` 围栏 |
| `checks[].action` | `#### 判断方法` 中的说明文字 | 判断步骤描述 |
| `checks[].expected_result` | `#### 判断方法` 中的期望输出 | 判断标准 |
| `solution_steps` (临时/无标记) | `#### 解决方案` → `**快速恢复**` | 步骤化列表 |
| `temporary_workaround` | `#### 解决方案` → `**快速恢复**` | 临时方案 |
| `permanent_fix` | `#### 解决方案` → `**彻底解决**` | 根治方案 |
| `root_causes` | `#### 判断方法` 末尾的 `**根因分析**` 段落 | 保留溯源价值 |
| `source_case_count` | `<!-- source_cases: N confidence: H/M -->` HTML注释 | 保留置信度元数据 |

**variable_schema 提取策略（可选 LLM 辅助增强）**：

基础版（纯规则）：
- 扫描 `command_example` 中的 `{placeholder}` 模式 → 自动声明为 `user_input` 变量
- 识别 `node_ip`、`disk_dev` 等已知名称 → 映射为 `env:ssh_context` 策略

增强版（LLM 辅助，可选）：
- 将单个 branch 的诊断命令发给 LLM，提取结构化变量及其依赖关系
- 与现有 `variable_schema` 字段合并（不覆盖已发布的人工编辑值）

---

### [NEW] `data-pipeline/raw_to_sop/__main__.py`

独立 CLI，不挂靠 `kbd/run.py`：

```bash
# 用法（在 data-pipeline/ 目录下运行）
python -m raw_to_sop --file raw/内存ECC故障.json --category-id "硬件-内存" --dry-run
python -m raw_to_sop --dir raw/ --category-id "硬件"
```

参数设计：
- `--file` / `--dir`：指定单个 Raw JSON 文件或整个目录
- `--category-id`：SOP 分类（可选）
- `--dry-run`：仅生成 Markdown，不调用 API，输出到 `./out/` 目录，供人工审核后再决定是否入库
- `--output-dir`：将生成的 Markdown 文件保存到本地指定目录（与 `--dry-run` 配合）

### [NEW] `data-pipeline/raw_to_sop/ingestor.py`

独立 httpx 客户端，调用 `POST /api/sop/ingest` API（与 `kbd/import_sop.py` 逻辑相同，但独立实现，不 import 对方）：

```python
async def ingest_markdown(
    kb_service_url: str,
    token: str,
    source_id: str,
    title: str,
    content_md: str,
    category_id: str | None = None,
) -> dict: ...
```

---

## 关键设计决策

### 决策 1：扁平化策略（避免深层中间节点）

> [!IMPORTANT]
> **推荐**：将所有 branches 直接挂载在根节点下（H1 → H2），不产生中间路由层。`flow` 的路由条件下沉为每个叶节点的 `prerequisite_items`。
>
> 原因：
> - 现有 65 节点 SOP（`虚拟机开机失败`）已证明扁平一层叶子结构能够在 Agent ReAct 中正常工作
> - 避免中间路由节点带来的 `children` 非空校验（中间节点无需 `diagnosis` 和 `solution`）
> - 生成的 Markdown 更易于人工二次审校

> [!NOTE]
> **可选**：如果 `flow_visual` 中存在明确的二级分组（如 branch-D 和 branch-E 同属"硬件异常"大类），可引入一层 H2 路由节点（含 `prerequisite_items` 但无 `diagnosis/solution`），将相关 branches 以 H3 挂载其下。

### 决策 2：Markdown 作为中间层（不绕过 sop_parser）

所有生成的内容先写为 Markdown，再走 `POST /api/sop/ingest` → `sop_parser` 链路。不直接生成 `tree_json`。

### 决策 3：`draft` 状态入库（人工二次审校）

由于 Raw JSON 的自然语言内容经过 LLM 聚合，在分词、措辞等方面可能与人工编写的 SOP 存在出入，所有转化结果以 `draft` 状态入库，需人工确认后通过 `POST /api/admin/sop/{id}/approve` 发布。

### 决策 4：幂等性保证

使用 `meta.node_uid` 计算 `source_id`（如 `raw-{node_uid}`），通过 `import_sop.py` 的 `docx_hash` 机制实现幂等：重复导入同一个 Raw JSON 不产生重复记录。

---

## 验证方案

### 自动验证
```bash
# 1. dry-run 模式检查 Markdown 格式
python -m kbd.run raw-to-sop --file data-pipeline/raw/内存ECC故障.json --dry-run

# 2. 实际入库（draft 状态）
python -m kbd.run raw-to-sop --file data-pipeline/raw/内存ECC故障.json --category-id "硬件-内存"

# 3. 调用现有校验 API 查看 tree_validation_issues
curl -H "Authorization: Bearer $TOKEN" http://localhost:8004/api/admin/sop/{id}
```

### 人工验证
1. 在 Admin UI SOP 管理页面查看草稿 SOP 的决策树可视化
2. 确认每个叶节点的 `diagnosis.acli_methods` 非空
3. 确认 `prerequisite_items` 准确反映了 `routing_signals`
4. 对照原始 Raw JSON 核查分支覆盖完整性（7 个 branches 全部出现）
5. 人工 Approve 后，在 staging 环境触发 ReAct 引导测试

---

## 开放问题

> [!IMPORTANT]
> **Q1：树层级策略（扁平 vs. 分层）**
> 是否需要从 `flow_visual` 中提取中间路由层的语义（即，当 flow 步骤自身包含可诊断的过滤条件时，是否将其也作为一个中间路由节点生成）？
> 还是一律采用"扁平化"策略（所有 branches 直接挂根节点，路由条件作为叶节点前置）？

> [!NOTE]
> **Q2：variable_schema 增强策略**
> 是否在本期实现 LLM 辅助的变量提取（识别命令中的 `{placeholder}`，自动生成 `variable_schema`）？
> 还是保持纯规则提取（仅处理 `{placeholder}` 模式），将 variable_schema 的完善留给人工 Approve 阶段？

> [!NOTE]
> **Q3：solution_steps 分类策略**
> Raw JSON 的 `solution_steps` 未区分 `quick_recovery` 和 `thorough_fix`。
> 建议策略：全部归为 `quick_recovery`，`thorough_fix` 从 `permanent_fix` 字段提取（如果非空）。如果两者均为空，则 `thorough_fix` 复用 `quick_recovery`（与现有磁盘寿命 SOP 的处理逻辑一致）。

> [!NOTE]
> **Q4：现有 Raw JSON 目录规范**
> `data-pipeline/raw/` 目前包含 `内存ECC故障.json` 和 `虚拟机开关机失败排障手册.docx`。
> 是否需要约定新的子目录结构（如 `raw/sop/` 存放 Raw Graph JSON，`raw/docx/` 存放原始 Word 文档）？
