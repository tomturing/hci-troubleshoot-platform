# HCI 智能排障平台 — 文档状态索引

> **用途**：快速判断"应该看哪篇文档、忽略哪篇"，以及了解当前系统处于什么阶段。
> **维护规则**：每个 Phase 完成后更新本文件的进度状态，其他文档不需要频繁更新。

---

## 现在系统在做什么（一句话）

**HCI 排障助手 = 双轨知识注入 + 三级 Fallback + 六阶段诊断状态机**

```
用户描述故障
   ↓
[双轨知识检索]
   ├── SOP 轨道：症状匹配 SOP 手册 → 注入「SOP排障流程」→ AI 按步骤执行
   └── KB 轨道：语义检索历史案例  → 注入「历史案例参考」→ AI 提取假设

[三级 Fallback]
   SOP 命中 > 案例命中 > 机制推理（标注【机制推理】，不拒绝回答）

[六阶段诊断]
   S0 意图识别 → S1 故障定位 → S2 假设生成 → S3 验证执行 → S4 根因确认 → S6 验证闭环
```

**当前阶段：P0 系统基线修复 ✅ 已提交（2026-03-23）；P2/P3/P4 核心机制代码已部分落地（见下表）**

> ⚠️ **代码超前于任务规划文档**：ReAct 执行器、诊断状态机（S0-S6）、人工确认接口、工具审计日志、知识原子审核页等功能在代码层面已实现，但对应任务编排文档（32-35）尚未创建。

---

## 一、当前有效文档（阅读这里就够了）

### 理解系统为何这样设计
| 文档 | 一句话说明 | 必读程度 |
|------|-----------|---------|
| [architecture/08_HCI平台效果差距分析与重构方案.md](architecture/08_HCI平台效果差距分析与重构方案.md) | 现状哪里不够好、为什么要改 | ⭐⭐⭐ 必读 |
| [architecture/11_完整技术方案.md](architecture/11_完整技术方案.md) | 四个阶段的整体路线图 + 目标架构图 | ⭐⭐⭐ 必读 |

### 理解知识库（RAG）怎么工作
| 文档 | 一句话说明 | 时间维度 |
|------|-----------|---------|
| [architecture/06_知识库RAG设计.md](architecture/06_知识库RAG设计.md) | **当前已实现**：BM25 + pgvector 检索基础设施 | 现状 |
| [architecture/12_知识库重建设计方案.md](architecture/12_知识库重建设计方案.md) | **未来要做**：在 06 基础上增加「知识原子」结构 | Phase 1 目标 |

> 三文档关系：11 是地图；06 是当前知识库详图；12 是知识库的升级版蓝图。

### 理解 AI 对话层怎么工作
| 文档 | 一句话说明 |
|------|-----------|
| [architecture/05_AI助手层设计.md](architecture/05_AI助手层设计.md) | AI 助手层基础设计 |
| [architecture/10_各层最优设计.md](architecture/10_各层最优设计.md) | 5 段式 Prompt、ReAct 执行器的设计细节 |

### 当前执行中的任务
| 文档 | 状态 |
|------|------|
| [archive/31_文档代码审查结果.md](archive/31_文档代码审查结果.md) | ✅ **审查完成**（2026-03-24） |
| archive/32_任务编排_P1_知识库重建.md | ⚠️ **文件待创建**（P1 启动前需先建此文档） |
| archive/33_任务编排_P2_诊断状态机.md | ⚠️ **文件待创建**（注：部分代码已在主分支实现） |
| archive/34_任务编排_P3_ReAct引擎与工具接入.md | ⚠️ **文件待创建**（注：ReAct 执行器已实现） |
| archive/35_任务编排_P4_工具扩展与数据管道.md | ⚠️ **文件待创建**（注：工具审计日志已实现） |

---

## 二、已归档文档（仅供追溯，通常不需要阅读）

### archive/ 早期版本（已被 architecture/ 新文档取代）
- `archive/00~13` — 项目初期设计文档，内容已被 architecture/ 超越
- `archive/14~30` — 开发流程、发布规范等运维文档，按需查阅

### 备选方案（决策已做，无需再读）
- `architecture/09_框架全景比较.md` — 框架调研，ReAct 已选定
- `architecture/13_知识工程方案A_认知记忆架构.md`
- `architecture/14_知识工程方案B_因果图推理架构.md`
- `architecture/15_知识工程方案C_策略蒸馏架构.md`
  > 以上三个方案均已评估，决策：采用双轨知识架构（SOP + KB 案例），见 08 号文档

### 参考资料（灵感来源，非项目规范）
- `architecture/16_Claude式工作架构参考.md` — Claude 工作方式参考
- `architecture/17_AI数据格式友好度指南.md` — 数据格式最佳实践
- `architecture/18_Agent技术原理深度讲解.md` — Agent 原理学习资料
- `architecture/19_Context_Window深度讲解.md` — 上下文窗口原理

---

## 三、P0 完成后的实际变更记录

> 供下一个 Phase 启动时核对"现实与文档是否一致"。

| 变更项 | 计划（31号文档）| 实际结果 |
|--------|---------------|---------|
| `_SYSTEM_BASE` 重写 | 5 段式 Prompt | ✅ 完成 |
| 知识库 fallback 逻辑 | 三级 fallback（SOP/案例/机制推理） | ✅ 完成 |
| `diagnostic_stage` 预留 | Pydantic Schema，非 DB Column | ✅ 完成 |
| postgres 镜像 | `pgvector/pgvector:pg15` | ✅ 完成 |
| kb-service healthcheck | curl /health，30s 间隔 | ✅ 完成 |
| 5 个错误 SOP 隔离 | 改名 `.DEPRECATED` | ✅ 完成 |
| SOP 命名修正 | `vm_power_failure` → `vm_start_failure` | ✅ 完成（P0 额外完成，31号文档未提及）|
| index.json 更新 | 移除 5 条错误条目，加 `knowledge_type: "sop"` | ✅ 完成 |
| fix_sop_format.py | 格式修复脚本 + 17 个单测 | ✅ 完成 |

**P1 启动前需注意**：32 号文档 Task 05 步骤中提到处理 `vm_power_failure` 目录，
实际目录已改名为 `vm_start_failure`，执行时请使用新名称。

## 2026-03-25 kb-service FastAPI 0.109.0 status_code=204 断言修复

**问题**：`review.py` 的 `DELETE /{atom_id}` 路由使用 `status_code=204, response_class=Response`，但 FastAPI 0.109.0 的断言逻辑为：

```python
if self.response_model:
    assert is_body_allowed_for_status_code(status_code)
```

`-> None` 返回注解推断出 `NoneType`（truthy class），导致断言触发。

**修复**：改为 `response_model=None`（显式 `None` 为 falsy，跳过断言），移除 `-> None` 注解和孤立 `Response` import。

## 2026-03-26 代码审查 Bug 修复（docs/phase0-4-task-orchestration 分支）

本次修复基于对 `docs/phase0-4-task-orchestration` 分支代码审查报告结论，逐项修复所有问题。

## 2026-03-26 ArgoCD GitOps prod 集群 namespace 修正

**问题**：`hci-platform-prod` 和 `hci-platform-data-prod` ArgoCD Application 的 `destination.namespace` 错误配置为 `hci`，与 prod 集群实际使用的 namespace `hci-prod` 不一致。

**修复**：
- `deploy/gitops/argo-apps/hci-platform-prod.yaml`: namespace `hci` → `hci-prod`
- `deploy/gitops/argo-apps/hci-platform-data-prod.yaml`: namespace `hci` → `hci-prod`

### 🔴 高优先级修复

**`knowledge_retriever.py`：KB chunks 全部超限时 fallback_level 与 prompt 不一致**

- **问题**：当所有 KB chunks 均超出 `KB_CONTEXT_MAX_CHARS` 时，`chunks_text_parts` 为空，`SEGMENT_CASE_REFERENCE` 不会被注入，但 `fallback_level` 仍被设为 `"kb_case"`，导致 audit_meta 与实际 LLM 收到的 Prompt 不一致。
- **修复**：将 `fallback_level = "kb_case"` 移入 `if chunks_text_parts:` 分支内；新增 `else` 分支，将超限时的降级改为 `fallback_level = "mechanism"`，并注入 `SEGMENT_NO_REFERENCE`，同时 `logger.warning` 记录 `kb_chunks_all_oversized` 事件。

### 🟡 中优先级修复

**三处 OTel span 未记录异常（`tool.execute`、`conversation.react_stream`、`knowledge.retrieve`）**

- **`react_executor.py`（`tool.execute`）**：将 `execute()` 调用移入 inner `try/except`，异常时调用 `span.record_exception(e)` + `span.set_status(trace.StatusCode.ERROR, ...)` 后 `raise`，由外层 `except` 捕获并赋值 `error`/`result`。
- **`conversation_service.py`（`conversation.react_stream`）**：在 `_error` 路径的 `raise RuntimeError` 前插入 `span.record_exception(_err)` + `span.set_status(trace.StatusCode.ERROR, ...)`。
- **`knowledge_retriever.py`（`knowledge.retrieve`）**：将 `retrieve()` 方法体提取为私有方法 `_retrieve_impl()`，外层 `retrieve()` 在 span 内调用 `_retrieve_impl`，并用 `try/except` 拦截任何异常以 record + set_status 后 re-raise。

**`test_react_e2e.py`：`test_tool_audit_log_written_after_execution` 修改全局状态未还原**

- 在赋值 `async_client.app.state._audit_service = real_audit` 前保存原始值 `original_audit_service`，在 `finally:` 块中恢复。

### 🟢 低优先级修复

**`knowledge_retriever.py` 多项设计改善**：

- `SEGMENT_CODES` 字典类型混用（int→tuple 与 int→dict）拆分为三个独立常量：`_SEGMENT_CODE_A`（dict[int,tuple]）、`_SEGMENT_CODE_B`（dict[str,tuple]）、`_SEGMENT_CODE_D1`（tuple），消除所有 `# type: ignore` 注释。
- `_build_context_breakdown` 改用显式 `has_b_layer = len(sections) > 4` 替代脆弱的 `len(sections) == 4 / > 4` 多分支判断，逻辑更清晰且对未来段数变化有弹性；补充 docstring 说明 5 段/4 段两种结构。
- KB chunks 截断累计变量从 `total_chars` 重命名为 `chunks_total_chars`，与 audit_meta 中语义不同的 `total_chars`（全 context breakdown 总字符数）区分开。
- `has_sop` 检查中 `and not isinstance(sop_node, Exception)` 冗余判断已删除（该路径在并发容错处理后 `sop_node` 必为 `None` 或 `dict`），改为 `sop_node is not None`，并附注释说明原因。
