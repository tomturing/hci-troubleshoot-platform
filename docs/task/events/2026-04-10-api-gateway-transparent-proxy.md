---
status: active
category: task
audience: developer
last_updated: 2026-04-10
owner: team
---

# 任务：api-gateway 透明代理重构

## 关联方案

[方案事件文档](../solution/events/2026-04-10-api-gateway-transparent-proxy.md)

---

## 变更历史

| 日期 | 版本 | 变更内容 | 关联事件文档 |
|------|------|---------|------------|
| 2026-04-10 | v1.0 | 初版 | — |

---

## 任务清单

### T1: 重构 api-gateway category_import_proxy 接口
- **描述**：修改 `backend/api-gateway/app/routes/kb.py` 中的 `category_import_proxy` 函数，实现真正的透明透传
- **文件变更**：
  - `backend/api-gateway/app/routes/kb.py` - 修改代理逻辑
- **验收标准**：
  - 代码不再使用 `request.form()`
  - 直接透传 `request.body()` 和 Content-Type 头
- **依赖**：无
- **预计耗时**：30分钟
- **状态**：待开始

### T2: 移除 api-gateway python-multipart 依赖
- **描述**：从 `backend/api-gateway/requirements.txt` 中移除 `python-multipart` 依赖
- **文件变更**：
  - `backend/api-gateway/requirements.txt` - 删除依赖行
- **验收标准**：
  - requirements.txt 不包含 `python-multipart`
- **依赖**：T1
- **预计耗时**：5分钟
- **状态**：待开始

### T3: 更新知识库设计文档
- **描述**：更新 `docs/solution/knowledge-base/知识库设计.md` 变更历史，记录本次重构
- **文件变更**：
  - `docs/solution/knowledge-base/知识库设计.md` - 更新变更历史
- **验收标准**：
  - 变更历史包含本次重构记录
  - 记录问题原因和方案决策
- **依赖**：T1, T2
- **预计耗时**：15分钟
- **状态**：待开始

---

## 任务依赖图

```
T1 (重构接口) → T2 (移除依赖) → T3 (更新文档)
```

---

## 执行顺序建议

1. T1: 重构 api-gateway 接口
2. T2: 移除 python-multipart 依赖
3. T3: 更新文档

---

## 文档更新计划

- [x] `docs/requirement/events/2026-04-10-api-gateway-transparent-proxy.md` - 需求事件文档
- [x] `docs/solution/events/2026-04-10-api-gateway-transparent-proxy.md` - 方案事件文档
- [x] `docs/task/events/2026-04-10-api-gateway-transparent-proxy.md` - 任务事件文档（本文档）
- [ ] `docs/solution/knowledge-base/知识库设计.md` - 更新变更历史

---

## 测试计划

- 单元测试：无新增
- 集成测试：CI 自动运行
- 人工测试：
  - admin-ui 上传 YAML 文件，验证导入成功
  - 检查 api-gateway 日志无错误
  - 检查 kb-service 日志显示正确处理