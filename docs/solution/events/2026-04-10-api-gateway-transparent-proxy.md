---
status: active
category: solution
audience: developer
last_updated: 2026-04-10
owner: team
---

# 方案：api-gateway 透明代理重构

## 变更历史

| 日期 | 版本 | 变更内容 | 关联事件文档 |
|------|------|---------|------------|
| 2026-04-10 | v1.0 | 初版 | [需求事件](../../requirement/events/2026-04-10-api-gateway-transparent-proxy.md) |

---

## 背景与需求

详见 [需求事件文档](../../requirement/events/2026-04-10-api-gateway-transparent-proxy.md)

---

## 方案概述 (WHAT)

重构 api-gateway 的 `/api/kb/categories/import` 接口，实现真正的透明透传：
1. 网关层直接透传请求体（body）和 Content-Type 头
2. 下游 kb-service 负责解析 multipart/form-data
3. 移除 api-gateway 对 `python-multipart` 的依赖

---

## 详细设计

### 问题根因分析

#### 现象
admin-ui 调用 `POST /api/kb/categories/import` 返回 500 错误。

#### 错误日志
```
AssertionError: The `python-multipart` library must be installed to use form parsing.
```

#### 代码分析
api-gateway 的 `backend/api-gateway/app/routes/kb.py:181-230` 实现如下：

```python
@categories_router.post("/import")
async def category_import_proxy(request: Request):
    """代理 YAML 导入请求 → kb-service。
    下游 kb-service 读取的是 YAML 原文字节流，不支持将 multipart/form-data 原样透传。
    因此这里需要在网关层对 multipart 上传进行解包，只转发文件内容本身。
    """
    content_type = request.headers.get("content-type", "")
    
    if content_type.startswith("multipart/form-data"):
        form = await request.form()  # ❌ 需要 python-multipart
        upload = form.get("file") or form.get("yaml_file") or ...
        raw_body = await upload.read()
    else:
        raw_body = await request.body()
    
    # 转发原始 YAML 内容给 kb-service
    resp = await client.post(url, content=raw_body, headers=outbound_headers, ...)
```

#### 根因
1. **设计问题**：网关层使用 `request.form()` 解析 multipart，需要额外依赖
2. **职责越界**：透明网关不应解析请求体，应直接透传
3. **依赖遗漏**：kb-service 添加了 `python-multipart`（PR #136），但漏掉了 api-gateway

---

### 方案对比

#### 方案 A：临时方案 - 添加依赖（已实施）

**实现**：在 api-gateway 的 `requirements.txt` 中添加 `python-multipart`

**优点**：
- 改动最小，立即生效
- 无需修改代码逻辑

**缺点**：
- 网关职责不清晰
- 引入不必要的依赖
- 未来新增文件上传接口需要重复处理
- 违反"透明代理"原则

**评分**：★★☆☆☆

#### 方案 B：最终方案 - 透明透传（用户选择）

**实现**：api-gateway 直接透传请求，kb-service 处理 multipart 解析

**优点**：
- 网关职责清晰，真正透明
- 无需额外依赖
- 未来扩展性好
- 符合微服务架构原则

**缺点**：
- 需要修改两个服务的代码
- 大文件上传时 kb-service 内存占用可能增加

**评分**：★★★★☆

#### 方案 C：KB 分离上传接口

**实现**：kb-service 新增独立的 multipart 上传接口

**优点**：
- 接口语义清晰
- 向后兼容

**缺点**：
- 增加接口数量
- 需要前端配合修改
- 维护成本增加

**评分**：★★★☆☆

---

### 决策依据 (WHY)

#### 为什么选择方案 B？

1. **第一性原理**：网关的本质职责是请求转发，不应承担业务逻辑
2. **架构清晰**：职责分离，api-gateway 只做路由，kb-service 处理业务
3. **维护成本低**：未来新增文件上传接口无需修改网关层
4. **依赖精简**：移除 api-gateway 的 `python-multipart`，减少攻击面

#### 为什么不选方案 A？

方案 A 只是治标不治本，虽然能解决问题，但：
- 引入了不必要的依赖
- 网关职责不清晰
- 技术债累积

#### 为什么不选方案 C？

方案 C 虽然可以工作，但：
- 需要前端配合修改
- 增加了不必要的接口复杂度
- 当前接口设计已经合理，没必要新增

---

### 详细设计

#### api-gateway 变更

**文件**：`backend/api-gateway/app/routes/kb.py`

```python
@categories_router.post("/import")
async def category_import_proxy(request: Request):
    """代理 YAML 导入请求 → kb-service（透明透传）"""
    # 直接透传请求体和 Content-Type
    raw_body = await request.body()
    content_type = request.headers.get("content-type", "application/x-yaml")
    
    headers = _internal_auth_headers()
    headers["content-type"] = content_type
    
    async with httpx.AsyncClient(timeout=60.0) as client:
        url = f"{KB_SERVICE_URL}/categories/import"
        resp = await client.post(
            url,
            content=raw_body,
            headers=headers,
            params=dict(request.query_params),
        )
        return JSONResponse(content=resp.json(), status_code=resp.status_code)
```

**依赖变更**：移除 `backend/api-gateway/requirements.txt` 中的 `python-multipart`

#### kb-service 变更

**文件**：`backend/kb-service/app/routes/categories.py`（PR #135 已实现）

```python
@router.post("/import")
async def import_categories(
    request: Request,
    file: UploadFile = File(..., description="YAML 分类文件"),
    dry_run: bool = Query(default=False, description="仅验证不写入"),
):
    """导入 YAML 分类数据（支持 multipart/form-data）"""
    content = await file.read()
    # ... 解析和导入逻辑
```

kb-service 已正确实现 multipart 文件上传处理。

---

## 影响范围

### 受影响的模块

| 模块 | 影响说明 |
|------|---------|
| api-gateway | 重构 `category_import_proxy`，移除 `python-multipart` 依赖 |
| kb-service | 无变更（PR #135 已支持 multipart） |

### 需要更新的文档

- [x] `docs/solution/knowledge-base/知识库设计.md` - 更新变更历史
- [ ] `docs/solution/events/2026-04-10-api-gateway-transparent-proxy.md` - 本文档

### API 兼容性

- 前端无需修改
- 请求格式不变（multipart/form-data）
- 响应格式不变

---

## 实施计划

1. 切换回 main 分支，创建重构分支
2. 修改 api-gateway 代码实现透明透传
3. 移除 api-gateway 的 `python-multipart` 依赖
4. 更新相关文档
5. 提交 PR，等待 CI 通过
6. 合并后验证功能

---

## 风险与缓解

| 风险 | 影响 | 概率 | 缓解措施 |
|------|------|------|---------|
| 大文件上传内存占用 | 中 | 低 | 后续可添加流式处理 |
| kb-service multipart 解析异常 | 高 | 低 | 已在 PR #135 中测试验证 |

---

## 测试策略

1. **单元测试**：无新增（透明透传不引入新逻辑）
2. **集成测试**：CI 自动运行
3. **人工测试**：
   - admin-ui 上传 YAML 文件，验证导入成功
   - 检查 kb-service 日志确认请求正确处理

---

## 验收标准

- [ ] admin-ui 分类 YAML 导入返回 200
- [ ] api-gateway `requirements.txt` 不包含 `python-multipart`
- [ ] CI 全部通过
- [ ] 文档已更新