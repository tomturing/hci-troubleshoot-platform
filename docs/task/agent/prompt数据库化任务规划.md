# HCI 智能排障平台 — Prompt 数据库化收敛与动态加载任务规划

本规划用于指导“Prompt 数据库化管理及动态热生效方案”的实施与验收。本开发周期内所有任务均遵循**代码结构主导原则**与**校验报错喧闹化**要求。

---

## 一、 任务分解清单

| 任务 ID | 任务模块 | 描述 | 优先级 | 影响范围 |
| :--- | :--- | :--- | :--- | :--- |
| **T-PRM-01** | 数据库/种子 | 修改 `seeds/02_system_prompts.sql`，写入从 Python 代码中抽离的 7 大核心模板，清退无效/空置模板。 | P0 | `system_prompt` 数据表 |
| **T-PRM-02** | 共享核心组件 | 在 `shared/utils/` 下创建 `StrictPromptLoader` 实现类，编写占位符静态解析与强校验逻辑，有错必报。 | P0 | `shared/utils/` |
| **T-PRM-03** | 依赖注入改造 | 改造 `agent-service` 的主 `lifespan` 引导，向各 Agent 构造函数传入数据库会话工厂 `db_session_factory`。 | P0 | `agent-service/app/main.py` |
| **T-PRM-04** | TriageAgent 重构 | 改造 S0 意图识别 Agent，移除硬编码，改由数据库动态加载 `base_identity`、`base_methodology`、`s0_intent_recognition`，注入环境上下文与分类。 | P1 | `triage_agent.py` |
| **T-PRM-05** | InvestigationAgent 重构 | 改造 S1-S4 诊断调查 Agent。将 SOP 导航新建、SOP导航恢复、SOP降级、Fallback 机制推理 4 种模式的模板从数据库动态按需拉取并强校验。 | P1 | `investigation_agent.py` |
| **T-PRM-06** | RemediationAgent 重构 | 改造 S5 方案修复 Agent，动态拉取 `s5_solution_v1` 模板并注入根因和修复计划。 | P1 | `remediation_agent.py` |
| **T-PRM-07** | 报错传导与拦截 | 改造 `agent-service` 的 `_event_stream` 流处理逻辑，捕获 `PromptLoadError` 等异常并格式化为 `error` 事件输出到 SSE 通道。 | P0 | `agent-service/app/routes/agent.py` |
| **T-PRM-08** | 测试与集成验收 | 编写单元测试验证校验逻辑；在前台通过修改 Prompt 触发“四川方言测试”和“拼写错误校验弹框拦截”。 | P1 | `tests/unit/`, 整体联调 |

---

## 二、 详细开发步骤

### 1. [T-PRM-01] 数据库 SQL 重置与初始化
*   **动作**：清空并重写 `database/seeds/02_system_prompts.sql`。
*   **对齐规范**：
    *   BASE 阶段 ➔ `base_identity_v1`, `base_methodology_v1`, `base_case_context_v1`
    *   S0 阶段 ➔ `s0_intent_recognition_v1`
    *   S1 阶段 ➔ `s1_sop_react_new_v1`
    *   S2 阶段 ➔ `s2_sop_react_resume_v1`
    *   S3 阶段 ➔ `s3_sop_legacy_v1`
    *   S4 阶段 ➔ `s4_fallback_v1`
    *   S5 阶段 ➔ `s5_solution_v1`
*   **占位符合规**：种子模板中的占位符必须与代码消费层传参保持 100% 一致。

### 2. [T-PRM-02] `StrictPromptLoader` 公共校验器开发
*   **实现位置**：`backend/shared/utils/prompt_loader.py`。
*   **方法设计**：
    *   `get_template_placeholders(template: str) -> set[str]`：静态分析大括号。
    *   `load_and_validate(session, name, expected_list) -> str`：DB 查询 ➔ 缺失元素校验（求差集）➔ 冗余元素校验 ➔ 抛出 `PromptValidationError` / `PromptLoadError`。
*   **核心准则**：严禁吞掉异常，确保有错必报。

### 3. [T-PRM-03] 依赖注入与生命周期改造
*   **修改目标**：`backend/agent-service/app/main.py`。
*   **动作**：
    *   修改 `TriageAgent`、`InvestigationAgent` 和 `RemediationAgent` 的构造签名，引入 `db_session_factory` 参数。
    *   在 `lifespan` 内实例化这三个 Agent 时，将 `db_manager.async_session_factory` 作为实参注入。

### 4. [T-PRM-04] TriageAgent 重构
*   **修改目标**：`backend/agent-service/app/adapters/agents/htp/triage_agent.py`。
*   **动作**：
    *   移除段落硬编码 `SEGMENT_IDENTITY` 等。
    *   在 `process()` 开始时，通过 `StrictPromptLoader` 获取 `base_identity_v1`, `base_methodology_v1`, `s0_intent_recognition_v1`, `base_case_context_v1` 模板并运行校验。
    *   进行参数拼接格式化。

### 5. [T-PRM-05] InvestigationAgent 重构
*   **修改目标**：`backend/agent-service/app/adapters/agents/htp/investigation_agent.py`。
*   **动作**：
    *   重构 `_build_sop_react_prompt`：加载 `base_identity_v1` + `base_methodology_v1` + `s1_sop_react_new_v1` + `base_case_context_v1` 并强校验。
    *   重构 `_build_sop_resume_prompt`：加载 `base_identity_v1` + `base_methodology_v1` + `s2_sop_react_resume_v1` + `base_case_context_v1`。
    *   重构 `_build_sop_prompt_legacy`：加载 `base_identity_v1` + `base_methodology_v1` + `s3_sop_legacy_v1` + `base_case_context_v1`。
    *   重构 `_build_fallback_prompt`：加载 `base_identity_v1` + `base_methodology_v1` + `s4_fallback_v1` + `base_case_context_v1`。

### 6. [T-PRM-06] RemediationAgent 重构
*   **修改目标**：`backend/agent-service/app/adapters/agents/htp/remediation_agent.py`。
*   **动作**：
    *   移除 `_S5_SYSTEM_PROMPT_TEMPLATE`。
    *   在 `process()` 开始时加载 `base_identity_v1` + `base_methodology_v1` + `s5_solution_v1` + `base_case_context_v1` 并强校验后格式化输出。

### 7. [T-PRM-07] 异常传导拦截
*   **修改目标**：`backend/agent-service/app/routes/agent.py` 的 `_event_stream`。
*   **动作**：
    *   捕获 `PromptLoadError` 和 `PromptValidationError` 异常。
    *   一旦捕获，中止推理生成，通过 `yield _sse({"type": "error", "message": ...})` 传导到下游，阻止 AI 错误运行。

---

## 三、 验收标准与测试验证

### 1. 自动化单元测试 (UnitTest)
*   编写 `tests/unit/test_prompt_loader.py`，覆盖：
    *   数据正常且模板占位符契合时，正常加载并返回内容。
    *   模板中误删必填占位符时，断言抛出 `PromptValidationError` 并附带具体字段名。
    *   模板中增加无法解析的非法占位符时，断言抛出 `PromptValidationError`。
    *   数据库连接断开时，抛出 `PromptLoadError`。

### 2. 界面联调与“喧闹化”报错人工验证
1.  **即时热更新验证**：在 Admin 页面修改 S0 模板文本（追加自定义方言输出提示），保存后在前台发起会话，模型应立即执行新模板。
2.  **配错喧闹阻断验证**：在 Admin 页面修改 S0 模板，将 `{categories_text}` 占位符修改为拼写错误的 `{categories_text_wrong}` 并保存。在前台发送消息，前台应即时中断并显式弹出配置校验错误的红色警示卡片，证明错误暴露机制有效，无静默吞噬。
