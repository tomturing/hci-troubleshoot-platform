# Skill 执行异常边界与 Agent/LLM 问题分层

## 背景与问题

排查工单 `Q2026092190071` 时发现：SOP 变量 `node_ip` 声明由 Skill `hci-alert-parsing`
自动获取，执行失败返回的错误是：

> 变量 node_ip 声明由 Skill hci-alert-parsing 自动获取，但 Skill 不存在、未启用或输出不可用

但「平台技能管理」里该技能**确实存在且已启用**（staging `hci_troubleshoot.skill_definition`
`is_active=t`）。真实根因是 **LLM 后端（火山 ARK coding 端点）在该时段读超时**，`ai_client.invoke`
抛出 `AI_TIMEOUT`，异常被 `engine.py` 的 `except Exception` 统一泛化为上述误导文案，导致排障时被
误导去查技能管理。

根因在于：skill 运行链路把**所有**异常（含 LLM 超时/限流/鉴权失败/网络不可达）与
「技能缺失/未启用」混为一谈，返回同一条 `sop_skill_variable_acquire_failed` 文案。

## 设计原则（第一性原理）

按**责任边界**将 Skill 执行异常二分为两类，各自携带机器可识别的 `boundary` 与 `error_code`：

### Agent/技能域（`boundary="agent"`）— 平台自身责任

排障应查：技能管理（是否创建/启用）、SOP 变量声明、`output_path` 配置、工具管理、AI 客户端注册。

| 异常类 | error_code | 触发场景 |
| --- | --- | --- |
| `SkillNotFoundError` | `skill_not_found_or_disabled` | 技能不存在或未启用（`is_active=false`） |
| `SkillToolDependencyError` | `skill_tool_dependency_missing` | `allowed_tools` 引用了不存在/未启用的工具 |
| `SkillAIClientError` | `skill_ai_client_missing` | 技能所需 AI 客户端（`assistant_type`）未配置 |
| `SkillOutputUnavailableError` | `skill_output_unavailable` | 技能执行成功但未返回所请求的变量/路径 |

### LLM/提供商域（`boundary="llm"`）— 外部依赖责任，与技能配置无关

排障应查：LLM 后端健康度、API Key、配额、模型部署、网络连通性。

| 异常类 | error_code | 触发场景 |
| --- | --- | --- |
| `SkillLLMTimeoutError` | `skill_llm_timeout` | 读/连接超时（`AI_TIMEOUT`） |
| `SkillLLMRateLimitError` | `skill_llm_rate_limited` | 限流（`AI_RATE_LIMITED`） |
| `SkillLLMAuthError` | `skill_llm_auth_failed` | 鉴权失败（`AI_AUTH_FAILED`） |
| `SkillLLMUpstreamError` | `skill_llm_upstream` | 上游 5xx/502/503（`AI_UPSTREAM_ERROR`） |
| `SkillLLMUnavailableError` | `skill_llm_unavailable` | 服务不可用/网络不可达（`AI_UNAVAILABLE`） |
| `SkillLLMModelNotFoundError` | `skill_llm_model_not_found` | 模型不存在（chat/completions 404） |
| `SkillLLMOutputError` | `skill_llm_output_invalid` | LLM 返回非合法 JSON / 结构错误（输出质量问题） |

## 关键不变量

1. 凡是 `SkillLLMError` 及其子类，`boundary` 必为 `"llm"`，文案中明确声明「与技能配置无关」。
2. 凡是 `SkillError`（非 LLM）子类，`boundary` 必为 `"agent"`，文案指向具体平台配置项。
3. `dynamic_runner.execute` 在调用 `ai_client.invoke` 处用 `classify_llm_exception` 将
   `AIStreamError` / httpx 异常精确归类，任何未知异常兜底为 `SkillLLMUnavailableError`
   （仍标注 `boundary="llm"`），**绝不**伪装成技能缺失。
4. `engine.sop_request_variable` 对 `STRATEGY_SKILL_CALL` 分支按异常类型分别返回结构化错误，
   携带 `boundary` / `error_code` / `llm_code`；依赖失败向上传播时（`dependency_error`）同样透传这些字段，
   因此上层变量（如 `asan_disks`）的错误能直接显示真实边界，不再误导。

## 调用链

```
sop_request_variable (engine.py, STRATEGY_SKILL_CALL)
  └─ DynamicSkillRunner.execute (dynamic_runner.py)
       ├─ get_active_skill        → SkillNotFoundError (agent) 仅当技能缺失/未启用
       ├─ _validate_allowed_tools→ SkillToolDependencyError (agent)
       ├─ ai_client.invoke        → classify_llm_exception → SkillLLM* (llm)  [NEW]
       ├─ json.loads 失败         → SkillLLMOutputError (llm)  [NEW]
       └─ 取值为空 / ok=false     → SkillOutputUnavailableError (agent)
```
