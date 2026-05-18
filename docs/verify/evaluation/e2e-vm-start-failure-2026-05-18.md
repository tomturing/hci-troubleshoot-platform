# E2E 测试评估报告：虚拟机开机失败排查

**测试时间**: 2026-05-18  
**工单编号**: Q2026051810128  
**客户端 ID**: client-mpayv9r9-2cox3cx  
**使用模型**: GLM-5  
**触发场景**: 虚拟机开机失败 → 错误提示"虚拟机镜像忙，正在执行其他操作！"  
**预期路径**: SOP branch-T（KVM进程残留导致镜像锁占用）  
**实际路径**: 机制推理兜底（B3），无任何 SOP / KBD 参考

---

## 一、根本发现：SOP 完全未命中（系统性缺陷）

### 1.1 核心结论

**整个 S1-S6 阶段，AI 给出的所有建议完全基于自身推理，未参考任何知识库内容。**

数据库证据（工单 `Q2026051810128` 的 `audit_log`）：

| 字段 | 值 | 含义 |
|------|----|------|
| `has_sop` | `false` × 4 次 | 全程 4 次对话均未命中 SOP |
| `kb_chunks_count` | `0` × 4 次 | 未检索到任何 KB 知识片段 |
| `kb_top_score` | `NULL` × 4 次 | 无知识分数 |
| `context_breakdown` | `[A1, A2, A3, B3, D1]` | B3=机制推理兜底，无 B1(SOP)/B2(KBD) |
| `conversation.sop_document_id` | `NULL` | 从未写入 SOP 文档引用 |

---

### 1.2 根因链路分析（第一性原理）

#### 问题1：整个过程是否命中 SOP？给 AI 的 prompt 是什么？

**结论：没有命中 SOP。**

调用链：
```
用户输入"虚拟机镜像忙" 
  → S0 意图识别 → 确认分类 category_id="虚拟机-003"
  → S1 调用 kb_client.route_by_category("虚拟机-003", query)
  → GET /api/kb/route?category_id=虚拟机-003&query=...
  → route.py: SOP 轨被注释跳过 → KBD 查询 → kbd_entry 0条
  → 返回 track="human_escalation", results=[]
  → knowledge_retriever.py: track != "sop" → has_sop=False
  → 注入 B3(机制推理兜底) 段，而非 B1(SOP内容)
```

**加载给 AI 的 System Prompt 实际结构**（来自 `context_breakdown`）：

| 段代码 | 段名称 | 字符数 |
|--------|--------|--------|
| A1 | 专家身份定义 | 124 |
| A2 | 诊断方法论 | 241 |
| A3 | 推理规范 | 343 |
| **B3** | **机制推理兜底**（← 应为B1: SOP内容） | 152 |
| D1 | 工单上下文 | 26 |

B1（SOP内容注入）、B2（KBD知识条目）**均缺失**。

---

#### 问题2：为何 `conversation.sop_document_id = NULL`？

**根因：`route.py` 第1轨 SOP 查询被硬编码注释。**

文件：`backend/kb-service/app/routes/route.py`，第 104-132 行：

```python
# 第 1 轨：SOP 优先（sop_document 表尚未创建，暂时跳过）
# TODO: sop_document 表创建后启用
# sop_docs = await _db_manager.fetch(
#     """
#     SELECT id, title, content
#     FROM sop_document
#     WHERE category_id = $1 AND status = 'published'
#     ORDER BY updated_at DESC
#     LIMIT $2
#     """,
#     category_id,
#     top_k,
# )
# if sop_docs:
#     return RouteResponse(track="sop", ...)
```

**悖论**：数据库中实际已有发布的 SOP 文档：

```sql
SELECT id, title, category_id, status FROM sop_document;
-- id=1, title='虚拟机开机失败排查流程', category_id='虚拟机-003', status='published'
```

SOP 数据已就绪，但代码永远不会查询它。因此：
- `route_by_category` 永远不返回 `track="sop"`
- `knowledge_retriever.py` 中 `sop_document_id` 赋值逻辑（`if track == "sop"`）永远不执行
- `conversation.sop_document_id` 永远为 `NULL`

---

#### 问题3：为何 `audit_log.has_sop = false`？

**根因同上。** `has_sop = True` 的唯一触发条件在 `knowledge_retriever.py`：

```python
if track == "sop" and results:
    # ...
    has_sop = True  # ← 唯一入口
```

由于 `route.py` SOP 轨被注释，`track` 永远是 `"kbd"` 或 `"human_escalation"`，`has_sop` 永远为 `False`。

---

### 1.3 最终根因

> `route.py` 的 SOP 查询代码被 `# TODO` 注释保留为未完成状态，导致整个 SOP 知识体系形同虚设。
>
> 各组件均已就绪：
> - ✅ `sop_ingest.py` 可导入 SOP 文档  
> - ✅ `sop_document` 表有数据（category_id='虚拟机-003'，status='published'）  
> - ✅ `knowledge_retriever.py` 有完整的 SOP 命中处理逻辑  
> - ✅ `admin.py` 有 SOP 审核接口  
> - **❌ `route.py` 的 SOP 查询被注释 → 整个链路断开**

---

## 二、AI 输出质量问题清单（基于 SOP branch-T 对比）

> **前提说明**：以下问题在"系统应已命中SOP"的前提下成立。
> 实际测试中 SOP 完全未被加载，AI 所有建议均为自由推理，偏差是必然结果。

### P0 - 严重（直接影响操作安全/正确性）

#### ISS-01：修复命令错误
- **AI 给出**: `kill -9 <PID>` 或 `kill <PID>`
- **SOP branch-T 要求**: `acli system kill <PID>`
- **风险**: Linux 通用 kill 命令在 HCI 平台可能无法正常终止 KVM 进程，且不经过 HCI 平台的进程管理层，可能导致状态不一致。

#### ISS-02：引入非 SOP 命令（virsh）
- **AI 额外提供**: `virsh list --all`、`virsh destroy <domain>` 等
- **SOP branch-T**: 无此命令，HCI 平台不使用 virsh 直接管理虚拟机
- **风险**: virsh 在 HCI 环境中可能无效或产生平台层面的状态不一致

#### ISS-03：遗漏高危操作授权警告
- **AI 表述**: 仅标注"请谨慎操作"
- **SOP branch-T 要求**: "执行前必须获得明确授权（高危）"
- **风险**: 未按规范执行授权流程，违反运维安全基线

### P1 - 重要（影响诊断准确性/流程规范性）

#### ISS-04：跳过 SOP check-1 验证步骤
- **AI 行为**: 未引导执行 `acli task get -v <vmid> -t <date> -k '虚拟机镜像忙' -s 'failed'`
- **SOP branch-T check-1**: 必须先查询任务失败记录，确认存在"虚拟机镜像忙"报错，才能进入 check-2
- **影响**: 跳过了分支确认逻辑，可能走错诊断路径

#### ISS-05：工作流进度条未随对话阶段推进
- **现象**: 全程停在第2步"信息确认"，未随 S2→S3→S4→S5 推进
- **影响**: 用户无法直观感知诊断进展，界面可观测性缺失

#### ISS-06：阶段跳跃，缺少用户交互确认
- **现象**: S2（问题确认）/S3（原因分析）/S5（解决方案）在单条消息中连续呈现
- **设计意图**: 每个阶段应等待用户确认，ReAct 执行器应逐阶段推进
- **影响**: 用户无法在中间节点介入纠正诊断路径

### P2 - 一般（影响诊断精度/完整性）

#### ISS-07：进程查找命令不精确
- **AI 给出**: `ps aux | grep qemu | grep <vmname>`（用名称）
- **SOP branch-T**: `ps aux | grep qemu | grep <vmid>`（用 VMID）
- **影响**: 虚拟机名称可能重复或含特殊字符，VMID 更精确

#### ISS-08：全程未引导采集环境数据
- **现象**: 界面始终显示"⚠️ 未采集环境数据"警告
- **影响**: AI 无实际环境信息辅助，全凭文字描述推断，诊断精度下降

#### ISS-09：缺少 solution-1.1 后验证步骤
- **AI 行为**: 给出 `kill` 命令后直接进入虚拟机重启步骤
- **SOP branch-T solution-1.1**: 执行进程终止后，应再次执行 `ps aux | grep qemu | grep <vmid>` 确认进程已消失
- **影响**: 无验证可能导致"进程未终止但已尝试重启"的隐患

---

## 三、修复建议

### 紧急修复（BUG-01）：启用 route.py SOP 查询轨道

**文件**: `backend/kb-service/app/routes/route.py`，第 104 行起

**操作**: 取消注释 SOP 查询代码，并修改查询字段（`content` → `content_md`）：

```python
# 第 1 轨：SOP 优先
sop_docs = await _db_manager.fetch(
    """
    SELECT id, title, content_md
    FROM sop_document
    WHERE category_id = $1 AND status = 'published'
    ORDER BY updated_at DESC
    LIMIT $2
    """,
    category_id,
    top_k,
)
if sop_docs:
    logger.info(event="route_sop_matched", category_id=category_id, count=len(sop_docs))
    return RouteResponse(
        track="sop",
        category_id=category_id,
        results=[
            RouteResult(
                id=doc["id"],
                title=doc["title"],
                content_md=doc.get("content_md"),
                support_id=f"sop-{doc['id']}",
                category_id=category_id,
            )
            for doc in sop_docs
        ],
    )
```

**预期效果**：修复后对 `虚拟机-003` 的路由将返回 `track="sop"`，`has_sop=true`，AI 获得 SOP 内容指导。

### 配套改进

| 编号 | 类型 | 描述 |
|------|------|------|
| IMP-01 | 功能修复 | ISS-01/02/03 依赖 SOP 被加载，BUG-01 修复后重新验证 |
| IMP-02 | 前端 | 修复工作流进度条不跟随阶段推进的问题（ISS-05） |
| IMP-03 | 对话流程 | S2/S3/S5 每阶段必须等待用户确认后才继续（ISS-06） |
| IMP-04 | 数据采集 | 引导用户在 S1 阶段执行环境数据采集（ISS-08） |

---

## 四、完成标准（再次验证条件）

修复 BUG-01 并重新执行同场景 E2E 测试，通过标准：

- [ ] `audit_log.has_sop = true`（至少 S1 首条）
- [ ] `conversation.sop_document_id = 1`（虚拟机-003 SOP 的 id）
- [ ] `audit_log.context_breakdown` 中出现 `B1`（SOP内容注入段）
- [ ] AI 给出 `acli system kill <PID>` 而非 `kill -9`
- [ ] AI 不提及 `virsh` 命令
- [ ] AI 明确提示"执行前需获得授权（高危操作）"
- [ ] AI 引导先执行 `acli task get` 确认任务报错（check-1）
- [ ] 进程终止后 AI 引导验证进程已消失（check solution-1.1）
