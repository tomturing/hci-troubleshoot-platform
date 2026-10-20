---
status: active
category: requirement
audience: developer
last_updated: 2026-04-10
owner: team
---

# 需求：api-gateway 透明代理重构

## 变更历史

| 日期 | 版本 | 变更内容 | 关联事件文档 |
|------|------|---------|------------|
| 2026-04-10 | v1.0 | 初版 | — |

---

## 背景与问题

### 业务背景
admin-ui 提供分类 YAML 导入功能，用户通过前端上传 YAML 文件，后端解析并导入到知识库分类表中。

### 核心痛点
当前实现存在架构问题：
1. **api-gateway 不够透明**：网关层使用 `request.form()` 解析 multipart/form-data，需要额外依赖 `python-multipart`
2. **职责混乱**：网关本应只做请求转发，却承担了文件解析职责
3. **维护成本高**：每个使用 multipart 的接口都需要在网关层特殊处理

### 影响范围
- `POST /api/kb/categories/import` 接口返回 500 错误
- admin-ui 分类管理页面无法正常导入 YAML

---

## 需求描述

### 功能概述
重构 api-gateway 为真正的透明代理，将 multipart/form-data 请求原样透传给下游 kb-service，由下游服务处理文件解析。

### 用户场景
用户在 admin-ui 分类管理页面点击"导入"，选择 YAML 文件上传，系统正确解析并导入分类数据。

### 预期收益
1. api-gateway 职责清晰，只做请求转发
2. 移除 api-gateway 对 `python-multipart` 的依赖
3. 未来新增文件上传接口无需修改网关层

---

## 功能需求

1. api-gateway `/api/kb/categories/import` 接口实现真正的透明透传
2. kb-service `/api/kb/categories/import` 接口正确处理 multipart/form-data 文件上传
3. 移除 api-gateway 的 `python-multipart` 依赖

---

## 非功能需求

- 性能要求：与当前实现相当，无明显性能下降
- 可观测性：保留现有的日志和追踪能力
- 兼容性：前端无需修改，接口调用方式不变

---

## 验收标准

- [ ] admin-ui 分类 YAML 导入功能正常，返回 200
- [ ] api-gateway 不再依赖 `python-multipart`
- [ ] kb-service 日志显示正确的请求处理记录
- [ ] CI 全部通过
- [ ] 文档已更新，记录问题原因和方案决策

---

## 约束条件

- 技术约束：使用 FastAPI 原生能力处理 multipart，不引入新依赖
- 兼容约束：前端代码无需修改
- 时间约束：本次迭代完成

---

## 风险与假设

### 已知风险
- 大文件上传时内存占用可能增加（FastAPI 默认将文件加载到内存）

### 假设条件
- 前端发送的 multipart/form-data 格式正确
- kb-service 已正确实现文件上传接口（PR #135 已完成）