---
status: completed
category: task
audience: developer
last_updated: 2026-07-09
owner: team
---

# KBD 分类与识图 Prompt 管理及重算链路修复任务

## 任务目标
解决前端 Prompt 过滤导致 KBD 阶段不可见的问题，并彻底排查修复重新分类时 HTTP 503 超时与重新识图时 HTTP 500 TypeError / 模型缺失问题。

## 任务清单
- [x] 修改 `frontend/admin/src/views/PromptManageView.vue` 增加 `KBD` 阶段、占位符说明及 CSS 类
- [x] 修改 `backend/api-gateway/app/routes/kb.py` 中的 `_kbd_proxy` 函数签名，添加对 `timeout` 关键字参数的支持
- [x] 提高重新分类代理请求超时到 120.0s，防止 DashScope 接口响应缓慢导致网关 30s 提前断开 (HTTP 503)
- [x] 验证 `qwen3.7-plus` 模型在当前 DashScope 平台 API 开发端点的可用性 (通过脚本测试)
- [x] 修改 `backend/kb-service` 里的 `classify.py` 与 `vision_processor.py` 默认回退模型为具有多模态识图能力的 `qwen3.7-plus`
- [x] 更新 `docs/solution/架构设计.md` 变更历史与方案事件文档
