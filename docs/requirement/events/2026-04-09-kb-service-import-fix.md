---
status: active
category: requirement
audience: developer
last_updated: 2026-04-09
owner: team
---

# kb-service 导入功能修复需求

## 变更历史
| 日期 | 版本 | 变更内容 | 关联事件文档 |
|------|------|---------|------------|
| 2026-04-09 | v1.0 | 初版 | — |

## 背景与问题

### 问题一：admin-ui 页面导入分类 YAML 报错 HTTP 500
- **业务背景**：管理员通过 admin-ui 的分类管理页面上传 YAML 文件导入分类数据
- **核心痛点**：上传后报错 `解析失败：HTTP 500`，无法完成导入
- **根因分析**：
  - 前端使用 `FormData` (multipart/form-data) 上传文件
  - 后端 `/api/kb/categories/import` 端点期望 JSON body
  - FastAPI 无法将 multipart 数据解析为 Pydantic 模型，抛出 422 错误被转换为 500

### 问题二：导入 SOP 脚本报错连接失败
- **业务背景**：开发者使用 `data-pipeline/kbd/import_sop.py` 导入 SOP 文档
- **核心痛点**：
  1. `All connection attempts failed` — kb-service 在 K3s 中运行，Service 类型是 ClusterIP，无法从 localhost:8004 直接访问
  2. `401 Unauthorized` — 脚本使用的默认 token 与 K3s 实际配置不一致
- **影响范围**：K3s 环境下所有需要导入 SOP 的场景

## 需求描述

### 问题一修复
- 修改后端 `/api/kb/categories/import` 端点，正确处理 multipart/form-data 文件上传
- 使用 FastAPI 的 `UploadFile` 和 `Query` 参数替代错误的 raw body 解析

### 问题二修复
- 更新 `data-pipeline/kbd/.env.example` 文档，说明 Docker Compose 和 K3s 两种环境的 token 配置方法
- 更新 `data-pipeline/kbd/import_sop.py` 用法说明，添加完整的前置操作步骤

## 验收标准
- [ ] admin-ui 页面可以成功导入分类 YAML 文件
- [ ] import_sop.py 文档说明清晰，包含 K3s 环境的前置操作

## 约束条件
- 不改变 API 语义，保持向后兼容
- 文档更新需符合文档门禁要求