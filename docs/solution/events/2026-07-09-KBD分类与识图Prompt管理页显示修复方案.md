---
status: completed
category: solution
audience: developer
last_updated: 2026-07-09
owner: team
---

# KBD 分类与识图 Prompt 管理及重算链路修复方案

## 背景与需求
在 KBD 知识条目管理中，用户在触发“重新分类”与“重新识图”时，在管理端 UI 依然会报错：
1. **重新分类失败：HTTP 503**：发生在 `/api/v1/kbd/{id}/reclassify`。
2. **重新识图失败：HTTP 500**：发生在 `/api/v1/kbd/{id}/reanalyze-images`。
3. **KBD Prompts 无法查看/管理**：发生在前端 Prompt 管理页面。

## 方案与根因分析 (WHAT & WHY)

### 1. 重新分类失败 (HTTP 503) 根因与修复
* **根因**：网关代理层 `api-gateway` 中通过 `_kbd_proxy` 向 `kb-service` 转发请求，默认未传递 timeout 参数，导致使用 `httpx.AsyncClient(timeout=30.0)`。然而，调用 DashScope API 处理分类逻辑大约需要 50-60 秒（本次日志实测 57 秒），引发网关 30 秒超时抛出 `RequestError`，网关捕获后返回 HTTP 503。而实际上 `kb-service` 后端已经于稍后时间处理成功，属于假死/超时状态。
* **设计**：在 `api-gateway` 的 `_kbd_proxy` 中新增 `timeout` 关键字参数，将 `reclassify` 请求的代理超时提高到 120 秒（`timeout=120.0`），确保在大批分类逻辑耗时较长时网关不提前断开。

### 2. 重新识图失败 (HTTP 500) 根因与修复
* **根因**：
  1. **代码参数错误**：`api-gateway` 中调用 `_kbd_proxy(..., timeout=300.0)` 传递了 `timeout` 参数，但是 `_kbd_proxy` 函数定义并没有声明接收该关键字参数，从而抛出 `TypeError: _kbd_proxy() got an unexpected keyword argument 'timeout'` 的 Python 语法错误（HTTP 500）。
  2. **模型选择错误**：PR512 中为避免未注入环境变量导致不可用，将分类和识图默认模型都回退到了全局的 `LLM_DEFAULT_MODEL` (`glm-5`)。然而，`glm-5` 是文本模型，不支持 Vision 识图能力，因此绝不能作为 `VISION_MODEL`。
* **设计**：
  1. 完善 `api-gateway` 的 `_kbd_proxy` 函数签名支持 `timeout` 传参。
  2. 经验证，大模型 `qwen3.7-plus`（多模态/识图模型）在当前开发环境 DashScope 接口上功能完全可用且速度正常。
  3. 将 `kb-service` 中 `classify.py` 与 `vision_processor.py` 的模型回退默认值从 `glm-5` 变更为已验证多模态可用的 `qwen3.7-plus`。

### 3. KBD Prompts 无法管理修复
* **方案**：在 `PromptManageView.vue` 的阶段列表 `stages` 和占位符映射中新增 `KBD` 阶段项，并补齐 CSS 徽章背景色样式，使 `kbd_classify_v1` 和 `kbd_vision_v1` 对用户可见。

## 影响范围 (哪些现行全量文档需要更新)
- `docs/solution/架构设计.md` 变更历史已追加记录。
- `backend/api-gateway/app/routes/kb.py`
- `backend/kb-service/app/routes/classify.py`
- `backend/kb-service/app/services/vision_processor.py`
- `frontend/admin/src/views/PromptManageView.vue`

## 验收标准
1. 在管理台进入 Prompt 管理，能正确查看和修改 KBD 模板。
2. 触发重新分类，请求可以在 120 秒内顺利走完并返回最新分类建议（无 HTTP 503 报错）。
3. 触发重新识图，网关可透传 `timeout=300.0` 并且调用多模态模型 `qwen3.7-plus` 完成图片解析，返回识图结果（无 HTTP 500 报错）。
