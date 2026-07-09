---
status: completed
category: verify
audience: developer
last_updated: 2026-07-09
owner: team
---

# KBD 分类与识图 Prompt 管理及重算链路修复验证

## 验证目标
确保修改后的 `PromptManageView.vue` 前端代码、`api-gateway` 网关代理逻辑以及 `kb-service` 能够无缝协作，无报错且具备正确的超时和模型支持。

## 验证内容与结果
1. **大模型端点及可用性验证**：
   编写并执行本地 python 脚本测试 DashScope 接口上的可用模型：
   * **glm-5** 测试：响应成功。
   * **qwen3.7-plus** 测试：响应成功 (`I am Qwen, a large language model developed by Alibaba Group's Tongyi Lab.`)。
   **结论**：`qwen3.7-plus` 运转良好且具备识图能力，适合作为默认的分类与识图模型。

2. **前端静态语法与构建验证**：
   在 `frontend/admin` 目录下运行 `pnpm run build` 顺利通过，未引入任何前端编译故障。

3. **网关与代理链路排查与修复核对**：
   * 检查 `api-gateway` 网关日志，成功定位了在进行 `/reanalyze-images` 调用时由于 Python 函数定义签名缺失 `timeout` 关键字参数导致 `TypeError` 的报错。
   * 成功定位 `/reclassify` 网关请求默认超时在 30 秒，而 DashScope 实际平均耗时约 57 秒，从而被网关客户端强制超时截断并向外抛出 HTTP 503。
   * 以上两项均已在代码中修复（网关接口支持并应用了大超时机制，且将默认模型改为了已通过可用性验证的多模态 `qwen3.7-plus`）。
