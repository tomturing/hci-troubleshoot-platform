# Raw 聚合 JSON 与数据库标准 SOPNode 结构对比报告

本报告针对 `/data-pipeline/raw/内存ECC故障.json`（Raw 聚合数据）与数据库中实际发布的 `sop_document`（`SOPNode` 标准多叉决策树）进行对比分析，揭示两者的相同点、本质差异以及平台知识管道的流转机理。

---

## 一、 数据结构图示对比

### 1.1 Raw 聚合 JSON 拓扑（“图与状态机”逻辑）
Raw JSON 采用的是**“单链入口 + 扁平分支跳转”**的 Graph 表达形式：

```
[Flow 诊断链] (entry-1 ── goto ──► branch-B BIOS故障)
     │
     ├── entry-2 ── goto ──► branch-E UE/MCE宕机
     │
     └── entry-3 ── goto ──► branch-C 630R2未重启
                                 │
                            [Branch C 链]
                            check-1 ── if_true ──► check-2 ... 
                                 │                      │
                             (reboot)               (replace DIMM)
```

### 1.2 数据库标准 SOPNode 决策树（“树与遍历”逻辑）
DB JSON 采用的是标准的**“多叉树级联嵌套”**形式，无显式 `goto` 指令，靠相对层级和前置检查驱动：

```
n-1: 内存ECC故障
  ├── n-1-1: 历史告警残留 (Leaf)
  ├── n-1-2: BIOS配置缺失 (Leaf)
  ├── n-1-3: 630R2未重启导致隔离失败 (Leaf)
  └── n-1-4: 内存硬件异常 (Route)
        ├── n-1-4-1: 可纠正错误(CE) (Leaf)
        └── n-1-4-2: 不可纠正错误(UE) (Leaf)
```

---

## 二、 相同点分析

两者作为排障知识在不同生命周期的承载实体，具有以下共通的业务要素：

1. **业务要素高度对齐**：
   * 两者均包含排障的核心闭环：**前置过滤条件**（Raw 中的 `checks` / DB 中的 `prerequisite_items`）、**诊断动作**（Raw 中的 `action` / DB 中的 `diagnosis`）以及**解决方案**（Raw 中的 `solution_steps` / DB 中的 `solution`）。
2. **命令与预期回显驱动**：
   * 均能够精确声明需要执行的 CLI/API 诊断指令，并规定预期的回显特征。例如：内存 ECC 故障中均声明了 `ipmitool sel elist` 检查 `Correctable ECC`，以及 `dmesg` 匹配 `panic in uncorrected hardware memory error`。
3. **多分支故障覆盖**：
   * 两者均具备覆盖复杂场景分流的能力。在 Raw 数据中通过 `G1~G6` 的预分组（pre-groups）聚合成 7 个 branches；而在数据库树结构中，对应分化出不同的 `children` 叶子路径。
4. **环境与场景元数据**：
   * 两者都定义了场景的特征。Raw JSON 的 `entry` 里包含 `symptoms`（异常现象）、`alerts_or_keywords`（特征告警），这与 DB SOP 中 Markdown 顶部的变量声明以及前置检查信息完全一致。

---

## 三、 差异性对比

| 维度 | Raw 聚合 JSON (`内存ECC故障.json`) | 数据库标准 SOPNode (`tree_json`) |
|---|---|---|
| **拓扑表现** | **图 / 状态机 (Graph / FSM)**：由主干流程 `flow` 配合多个跳转分支 `branches` 组成，依靠 ID 连线。 | **严格多叉决策树 (Tree)**：节点依靠 Pydantic 模型下的 `children` 字段进行无限制嵌套生长。 |
| **逻辑流控制** | **显式跳转**：每个步骤里硬编码了控制流（如 `goto_branch_id`、`if_true_next`、`if_success_next` 等跳转指令）。 | **隐式自适应遍历**：数据结构中无跳转逻辑。由 AI Agent（ReAct）在运行时读取当前节点的 `prerequisite_items` 判断并执行遍历。 |
| **数据粗细度** | **微观原子步骤**：把打开浏览器、点击按钮、导出备份、点击清除等每一步原子动作都抽象为一个 check/solution step。 | **宏观聚合节点**：将诊断归纳为 `page_methods` 与 `acli_methods`，将方案合并为 `quick_recovery` 与 `thorough_fix` 两类，信息更精炼。 |
| **变量池集成** | 无内建的变量拓扑声明，仅在文本中通过占位符 `{vmid}` 隐式表达。 | **内建 Variable Schema**：每个树节点带有完备的 `variable_schema` 数组，明示依赖关系 (`depends_on`) 与获取策略。 |
| **生命周期阶段** | **知识生成中继态**：是 Data Pipeline 阶段的原始产物，由 LLM 从多达 21 个真实历史案例（Source Cases）提取聚合而来。 | **运行时生产态**：由 Raw 转换为 Markdown/Docx 文档人工确认后，审核入库生成的、面向 AI 消费的最终结构化树。 |

---

## 四、 平台知识转换管道 (Data Pipeline)

通过以上对比可以理清平台对于这两种数据的融合和流转流程：

```
[21个原始 Case 日志] 
       │  (Stage 1 & 2: 抓取与语义清洗)
       ▼
[Raw 聚合 JSON] (内存ECC故障.json — 状态机图)
       │  (Stage 3: 转换并生成 Word / Markdown)
       ▼
[SOP 文档 (docx / md)] (人工阅读、话术校验与审计修订)
       │  (POST /api/admin/sop/{id}/approve)
       ▼
[SOPNode 决策树] (写入数据库 sop_document.tree_json — 嵌套决策树，供 Agent 消费)
```
* **转换价值**：由于 LLM 生成的 Raw JSON 结构微观且带有繁多的跳转线，不便于工程师人工校验和编辑。因此，转换管道在 Stage 3 将其归一化为 Heading 等级清晰的 Docx/Markdown 文档；在发布（Approve）时，再由 `sop_parser` 自动解析出极具层次感的 `SOPNode` 树，从而兼顾了**“人工易读易修改”**和**“AI Agent 易程序化执行”**的双向需求。
