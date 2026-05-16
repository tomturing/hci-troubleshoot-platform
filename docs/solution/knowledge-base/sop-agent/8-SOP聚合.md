# SOP Agent 聚合流程分析

> 本文档详细分析 `sop-agent` 项目中 SOP 聚合的实现逻辑。
>
> 源代码位置：`/mnt/d/aihci/sop-agent/scripts/aggregate_node_sop.py`（5504 行）

---

## 一、核心目标

### 1.1 要解决的问题

节点选择阶段确定了**在哪个层级产出 SOP**，聚合阶段解决的是**如何生成 SOP 内容**。

**核心问题**：一个节点下有多个案例，每个案例都有自己的诊断流程，如何合并成一个标准化、可复用的 SOP？

```
节点：虚拟机开机失败（50 个案例）

输入：
├─ case-1: BIOS 配置问题 → 修复 BIOS 设置
├─ case-2: 磁盘引导失败 → 重建引导分区
├─ case-3: 资源不足 → 调整 CPU/内存
├─ ...
└─ case-50: 镜像损坏 → 重新上传镜像

处理：
├─ 预分组：按故障场景分组（BIOS、磁盘、资源、镜像...）
├─ 批次归并：每组生成候选诊断分支
└─ 最终合并：生成入口检查 + 多分支诊断流程

输出：标准化 SOP
├─ entry flow: 入口检查步骤
├─ branch-A: BIOS 配置问题排查
├─ branch-B: 磁盘引导问题排查
├─ branch-C: 资源不足问题排查
└─ branch-D: 镜像问题排查
```

### 1.2 与节点选择与聚类的区别

| 方面 | 节点选择与聚类 | SOP聚合 |
|------|---------------|---------|
| **核心问题** | 在分类树的**哪个层级**产出 SOP？ | **如何生成** SOP 内容？ |
| **决策维度** | 分类树结构上的位置选择 | 案例内容的语义合并 |
| **输入** | 分类结果（案例 → 叶子节点映射） | 结构化事实卡（case_facts.jsonl） |
| **输出** | 节点列表 + 案例归属表 | 标准化 SOP 流程（node_sops.jsonl） |
| **处理粒度** | 节点级别 | 案例内容级别 |

### 1.3 输出示例

```json
{
  "node_path": "云计算-排障-虚拟机-虚拟机生命周期-虚拟机管理-虚拟机开机失败",
  "node_name": "虚拟机开机失败",
  "source_case_count": 50,
  "entry": {
    "step_id": "entry",
    "action": "检查虚拟机状态",
    "branches": ["branch-A", "branch-B", "branch-C"]
  },
  "branches": [
    {
      "branch_id": "branch-A",
      "branch_name": "BIOS 配置问题",
      "checks": [...],
      "solution_steps": [...]
    }
  ],
  "excluded_cases": [
    {"case_id": "case-100", "reason": "无明确故障信号"}
  ]
}
```

### 1.3 节点与案例的对应关系（1:N）

节点与案例是 **一对多** 关系：一个节点对应多个案例，聚合时将多个案例事实卡合并为一个 SOP。

| 数据结构 | 数量 | 说明 |
|---------|------|------|
| selected_nodes.csv | N 个节点 | 选中的 SOP 聚合节点 |
| case_facts.jsonl | M 行 | 每个案例一行事实卡（M >> N） |
| node_sops.jsonl | N 行 | 每个节点一行 SOP |

**聚合时的关系**：

```
节点A（虚拟机开机失败）
    │ 输入：50 个 case_facts（1:1 对应案例）
    │
    ▼ SOP聚合
    │ 输出：1 个 node_sop
    │
    └─ node_sops.jsonl 中的一行
```

---

## 二、概述

聚合阶段将同一节点下的多个案例事实合并为标准化的 SOP（标准操作程序）。

**核心思路**：
- 使用 LLM 进行多阶段智能聚合
- 本地代码校验确保输出质量
- 支持大规模案例的分层归约
- 生成结构化的诊断流程和处理步骤

---

## 二、整体流程

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           SOP 聚合流程                                        │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  Phase 0: Pre-group (语义预分组)                                             │
│  ┌──────────────────┐    ┌──────────────────┐    ┌──────────────────┐      │
│  │   define         │ → │     merge        │ → │     assign       │      │
│  │  定义分组结构     │    │  合并分组定义    │    │  分配案例到分组  │      │
│  └──────────────────┘    └──────────────────┘    └──────────────────┘      │
│                                                                             │
│  Phase 1: Batch Reduce (批次归并)                                            │
│  ┌──────────────────────────────────────────────────────────────────┐      │
│  │  将案例分批，每批独立生成候选分支（并行执行）                         │      │
│  └──────────────────────────────────────────────────────────────────┘      │
│                                                                             │
│  Phase 2: Intermediate Merge (中间归约)                                      │
│  ┌──────────────────────────────────────────────────────────────────┐      │
│  │  当批次数 > 3 时，进行层级归约（树状合并）                            │      │
│  └──────────────────────────────────────────────────────────────────┘      │
│                                                                             │
│  Phase 3: Final Merge (最终合并)                                             │
│  ┌──────────────────┐    ┌──────────────────────────────────────────┐      │
│  │   plan           │ → │  branch_content (并行生成各分支内容)        │      │
│  │  规划结构        │    │                                            │      │
│  └──────────────────┘    └──────────────────────────────────────────┘      │
│                                                                             │
│  输出: node_sops.jsonl (标准化 SOP)                                          │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 三、阶段详解

### Phase 0: Pre-group（语义预分组）

**触发条件**：案例数 > 6 且提供了预分组提示词

**目的**：将相似案例预先分组，提高后续聚合的质量和效率

#### 三阶段流程

```
┌─────────────────┐
│ Stage 1: Define │  按分块定义候选分组结构
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Stage 2: Merge  │  合并多个分块的分组定义
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Stage 3: Assign │  将案例分配到最终分组
└─────────────────┘
```

#### 代码实现

```python
# 第3107-3212行
async def run_pre_group(...):
    # 小数据集直接单次调用
    if n < PRE_GROUP_CHUNKED_THRESHOLD:  # 默认 80
        return await run_pre_group_single_call(...)
    
    # 三阶段模式
    # Stage 1: 分块定义
    chunks = [features[i:i + PRE_GROUP_DEFINE_CHUNK_SIZE]  # 默认 70
              for i in range(0, len(features), PRE_GROUP_DEFINE_CHUNK_SIZE)]
    chunk_results = await asyncio.gather(*define_tasks)
    
    # Stage 2: 合并定义
    unified_defs = await run_pre_group_merge(...)
    
    # Stage 3: 批量分配
    assignments = await asyncio.gather(*assign_tasks)
```

#### 参数配置

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `PRE_GROUP_MIN_CASES` | 6 | 触发预分组的最小案例数 |
| `PRE_GROUP_CHUNKED_THRESHOLD` | 80 | 触发三阶段模式的阈值 |
| `PRE_GROUP_DEFINE_CHUNK_SIZE` | 70 | 定义阶段每块案例数 |
| `PRE_GROUP_ASSIGN_BATCH_SIZE` | 50 | 分配阶段每批案例数 |

---

### Phase 1: Batch Reduce（批次归并）

**目的**：将案例分批处理，每批独立生成候选分支

#### 处理流程

```
案例列表
    │
    ▼ chunk_cases() 或 group_aware_batch()
┌─────────────────────────────────────┐
│ batch-A: [case1, case2, ...]       │ ─→ LLM ─→ 候选分支组 A
│ batch-B: [caseN, caseN+1, ...]     │ ─→ LLM ─→ 候选分支组 B
│ ...                                 │
└─────────────────────────────────────┘
        并行执行
```

#### 代码实现

```python
# 第2778-2877行
async def run_batch_reduce(...):
    # 1. 构建案例摘要
    case_digests = [build_case_digest(case) for case in node_cases]
    
    # 2. 调用 LLM
    for attempt in range(1, MAX_RETRIES + 1):
        response = await call_api(...)
        result = extract_json_result(response)
        
        # 3. 本地校验
        is_valid, err, normalized = validate_batch_result(result, input_case_ids)
        if is_valid:
            return normalized
        
        # 4. 失败重试（带反馈）
        messages.append({
            "role": "user",
            "content": f"<VALIDATION_FEEDBACK>\n- {err}\n</VALIDATION_FEEDBACK>"
        })
```

#### 校验内容

| 校验项 | 说明 |
|--------|------|
| JSON 格式 | 响应是否为合法 JSON |
| 必填字段 | `branch_id`、`branch_name`、`branch_type` 等 |
| 分支类型 | `runbook`、`info_collection`、`escalation` |
| 来源类型 | `explicit`、`inferred`、`mixed` |
| 步骤 ID | 层级结构是否正确 |

#### 参数配置

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `BATCH_SIZE` | 24 | 每批案例数 |
| `BATCH_CHAR_BUDGET` | 42000 | 每批字符预算 |
| `MAX_RETRIES` | 10 | 单批最大重试次数 |
| `FRESH_START_EVERY` | 2 | 连续失败后丢弃对话历史的轮数 |

---

### Phase 2: Intermediate Merge（中间归约）

**触发条件**：批次结果数 > 3 且提供了中间归约提示词

**目的**：对大量批次结果进行树状归约，减少最终合并的输入量

#### 层级归约示意

```
初始: [batch-A, batch-B, batch-C, batch-D, batch-E, batch-F]
                    │
                    ▼ 第一轮归约
        ┌───────────┼───────────┐
        │           │           │
    [A+B+C]     [D+E+F]     (每3个归约为1个)
        │           │
        └─────┬─────┘
              │
              ▼ 第二轮归约
          [最终结果]
```

#### 代码实现

```python
# 第3565-3704行
async def hierarchical_reduce(...):
    while len(batch_results) > max_input:
        # 分批归约
        new_results = []
        for chunk in chunks:
            merged = await run_intermediate_merge(...)
            new_results.append(merged)
        batch_results = new_results
    return batch_results
```

---

### Phase 3: Final Merge（最终合并）

**目的**：将所有候选分支合并为最终 SOP

#### Plan-Execute 模式

```
┌─────────────────────────────────────────────────────────────┐
│                     Final Merge                              │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  Step A: Plan（规划阶段 - 使用关键模型）                      │
│  ┌─────────────────────────────────────────────────────┐   │
│  │ 输入: 案例索引 + 候选分支摘要                          │   │
│  │ 输出: entry flow + branch_plan + excluded_cases      │   │
│  └─────────────────────────────────────────────────────┘   │
│                          │                                  │
│                          ▼                                  │
│  Step B: Branch Content（分支内容生成 - 并行执行）           │
│  ┌───────────┐ ┌───────────┐ ┌───────────┐                │
│  │ branch-A  │ │ branch-B  │ │ branch-C  │  ...           │
│  │ 内容生成   │ │ 内容生成   │ │ 内容生成   │                │
│  └───────────┘ └───────────┘ └───────────┘                │
│                          │                                  │
│                          ▼                                  │
│  Assemble: 组装最终结果                                      │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

#### 代码实现

```python
# 第4205-4322行
async def run_final_merge(...):
    # Step A: 规划（使用关键模型）
    plan = await _run_plan_step(
        client=effective_plan_client,  # CRITICAL 模型
        system_prompt=plan_prompt,
        user_msg=plan_user_msg,
        validate_fn=lambda r: validate_final_merge_plan(r, input_case_ids),
    )
    
    # Step B: 并行生成各分支内容
    async def _gen_one(bp):
        content = await run_branch_content(...)
        return bp["branch_id"], content
    
    tasks = [asyncio.create_task(_gen_one(bp)) for bp in plan["branch_plan"]]
    results = await asyncio.gather(*tasks)
    
    # 组装
    branches = assemble_branches_from_plan(plan["branch_plan"], branch_contents)
    return {
        "entry": plan["entry"],
        "flow": plan["flow"],
        "branches": branches,
        "excluded_cases": plan["excluded_cases"],
    }
```

---

## 四、输出结构

### 4.1 SOP 结构

```json
{
  "node_path": "云计算-排障-虚拟机-虚拟机生命周期-虚拟机管理-虚拟机开机失败",
  "node_name": "虚拟机开机失败",
  "source_case_count": 45,
  "entry": {
    "step_id": "entry",
    "action": "检查虚拟机状态",
    "branches": ["branch-A", "branch-B", "branch-C"],
    ...
  },
  "flow": [
    {"step_id": "1", "action": "...", ...},
    {"step_id": "2", "action": "...", ...}
  ],
  "branches": [
    {
      "branch_id": "branch-A",
      "branch_name": "资源不足导致开机失败",
      "branch_type": "runbook",
      "checks": [...],
      "solution_steps": [...],
      "symptoms": [...],
      "root_causes": [...],
      "source_case_ids": ["case-1", "case-5", "case-12"],
      ...
    }
  ],
  "excluded_cases": [
    {"case_id": "case-100", "reason": "无明确故障信号"}
  ]
}
```

### 4.2 步骤结构

#### 检查步骤（Check Step）

```json
{
  "step_id": "1.1",
  "action": "检查虚拟机 CPU 分配",
  "command_or_path": "virsh vcpuinfo",
  "command_example": "virsh vcpuinfo vm-001",
  "expected_result": "CPU 时间片分配正常",
  "matched_signals": ["CPU 使用率过高", "虚拟机卡顿"],
  "is_high_risk_operation": false,
  "command_line_executable": true,
  "if_true_next": "1.2",
  "if_false_next": "2.1",
  "if_true_exit_reason": "",
  "if_false_exit_reason": "",
  "source_type": "explicit",
  "evidence_quotes": ["原文引用..."]
}
```

#### 解决步骤（Solution Step）

```json
{
  "step_id": "2.1.1",
  "action": "调整 CPU 资源配额",
  "command_or_path": "界面操作",
  "expected_result": "虚拟机 CPU 资源增加",
  "is_high_risk_operation": false,
  "if_success_next": "2.1.2",
  "if_failure_next": "3.1",
  "if_success_exit_reason": "resolved",
  "if_failure_exit_reason": "",
  "source_type": "explicit"
}
```

---

## 五、分支类型

| 类型 | 说明 | 适用场景 |
|------|------|---------|
| `runbook` | 故障排查手册 | 有明确故障现象和解决步骤 |
| `info_collection` | 信息收集 | 需要先收集信息再判断 |
| `escalation` | 升级处理 | 需要人工介入或升级支持 |

---

## 六、校验机制

### 6.1 本地校验类型

| 校验类型 | 函数 | 说明 |
|---------|------|------|
| JSON 格式 | `extract_json_result` | 响应是否为合法 JSON |
| 批次结果 | `validate_batch_result` | 分支结构、字段完整性 |
| 预分组结果 | `validate_pre_group_result` | 分组定义、案例覆盖 |
| 规划结果 | `validate_final_merge_plan` | 流程结构、案例覆盖 |
| 步骤图 | `validate_step_graph` | 步骤 ID 连贯性、无循环 |
| CLI 命令 | `validate_cli_command` | Shell 命令语法（`bash -n`） |
| 命令保真 | `validate_command_retention` | 命令是否丢失 |

### 6.2 步骤图校验

```python
# 第1523-1675行
def validate_step_graph(steps, ...):
    # 1. 检查步骤 ID 层级结构
    # 2. 检查跳转目标是否存在
    # 3. 检查是否存在循环
    # 4. 检查退出原因是否合法
```

### 6.3 命令语法校验

```python
# 第1425-1453行
def validate_cli_command(step):
    command = step.get("command_example", "")
    if not command:
        return True, ""
    
    # 使用 bash -n 检查语法
    result = subprocess.run(
        ["bash", "-n"],
        input=command,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False, f"语法错误: {result.stderr}"
    return True, ""
```

---

## 七、模型配置

### 7.1 双模型架构

```python
# 主力模型：大量并行任务
WORKHORSE_MODEL_NAME = "gemini-3-flash-preview"

# 关键模型：高难度决策任务
CRITICAL_MODEL_NAME = "gemini-3-flash-preview"  # 可单独配置
```

### 7.2 模型分工

| 阶段 | 使用模型 | 原因 |
|------|---------|------|
| batch_reduce | WORKHORSE | 大量并行执行 |
| pre_group_define/assign | WORKHORSE | 大量并行执行 |
| pre_group_merge | CRITICAL | 需要综合判断 |
| intermediate_merge_plan | CRITICAL | 需要综合判断 |
| final_merge_plan | CRITICAL | 需要综合判断 |
| branch_content | WORKHORSE | 大量并行执行 |

### 7.3 Structured Output

```python
# 支持 JSON Schema 约束
STRUCTURED_OUTPUT_MODE = "json_object"  # 或 "json_schema" / "none"
```

---

## 八、断点续跑机制

### 8.1 Checkpoint 结构

```python
class NodeCheckpoint:
    def get_groups() -> list | None
    def save_groups(groups)
    
    def get_batch_result(index) -> dict | None
    def save_batch_result(index, result)
    
    def get_reduced_results() -> list | None
    def save_reduced_results(results)
    
    def get_final_payload() -> tuple | None
    def save_final_payload(record, intermediate)
```

### 8.2 恢复流程

```
启动聚合
    │
    ▼
检查 checkpoint
    │
    ├─ 有最终结果 → 直接返回
    │
    ├─ 有中间结果 → 从断点继续
    │
    └─ 无 checkpoint → 从头开始
```

---

## 九、参数配置汇总

### 9.1 核心参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `MAX_CONCURRENCY` | 50 | 最大并发请求数 |
| `MAX_RETRIES` | 10 | 单次操作最大重试次数 |
| `RETRY_BASE_DELAY` | 3 | 重试基础等待秒数 |
| `REQUEST_TIMEOUT` | 900 | 单次请求超时秒数 |
| `TEMPERATURE` | 0 | 采样温度（确定性输出） |

### 9.2 分批参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `BATCH_SIZE` | 24 | 每批案例数 |
| `BATCH_CHAR_BUDGET` | 42000 | 每批字符预算 |
| `FINAL_MERGE_MAX_INPUT_BATCHES` | 3 | 触发中间归约的阈值 |

### 9.3 虚拟拆分参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `VIRTUAL_SPLIT_CASE_THRESHOLD` | 120 | 触发虚拟拆分的案例数 |
| `VIRTUAL_SPLIT_MAX_DEPTH` | 2 | 虚拟拆分最大深度 |
| `VIRTUAL_SPLIT_MIN_GROUP_CASES` | 8 | 每组最小案例数 |

---

## 十、常见问题解答

### Q1：为什么需要预分组？

**原因**：
- 大量案例直接聚合可能丢失细节
- 相似案例分在一组，提高聚合质量
- 分组后可以并行处理，提高效率

---

### Q2：batch_reduce 和 final_merge 的区别是什么？

| 方面 | batch_reduce | final_merge |
|------|--------------|-------------|
| 输入 | 原始案例 | 批次归并结果 |
| 输出 | 候选分支组 | 最终 SOP |
| 数量 | 多个批次并行 | 单次合并 |
| 模型 | WORKHORSE | CRITICAL（规划阶段） |

---

### Q3：excluded_cases 是什么？

**定义**：被排除的案例，不参与 SOP 生成。

**排除原因**：
- 无真实故障信号
- 无稳定根因
- 只是功能说明或配置介绍
- 信息不足以生成有效步骤

---

### Q4：entry flow 是什么？

**定义**：SOP 的入口流程，包含初始检查步骤和分支路由。

**结构**：
```json
{
  "step_id": "entry",
  "action": "入口检查",
  "branches": ["branch-A", "branch-B"],
  "if_no_match_next": "3.1",
  "if_no_match_exit_reason": "manual_review"
}
```

---

### Q5：如何保证命令的准确性？

**机制**：
1. `source_type` 字段标记来源（explicit/inferred/mixed）
2. `bash -n` 语法校验
3. `validate_command_retention` 检查命令是否丢失
4. `evidence_quotes` 保留原文引用，便于溯源

---

## 十一、相关文件

| 文件 | 说明 |
|------|------|
| `scripts/aggregate_node_sop.py` | 聚合主脚本 |
| `prompt/node_sop/node_sop_batch_reduce_prompt_hci.md` | 批次归并提示词 |
| `prompt/node_sop/node_sop_pre_group_prompt_hci.md` | 预分组提示词 |
| `prompt/node_sop/node_sop_final_merge_plan_prompt_hci.md` | 最终合并规划提示词 |
| `prompt/node_sop/node_sop_branch_content_prompt_hci.md` | 分支内容提示词 |

---

## 十二、与上下游的关系

```
extract_case_facts.py
        │
        │ 输出: case_facts.jsonl
        │
        ▼
aggregate_node_sop.py  ←── 本次分析
        │
        │ 输出: node_sops.jsonl
        │
        ▼
render_sop_html.py / render_sop_docx.py
```

---

*文档创建时间：2026-05-16*
*最后更新：2026-05-16*
