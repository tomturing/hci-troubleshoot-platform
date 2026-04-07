# Admin 路由冲突与 Token 鉴权修复验证

| 字段 | 值 |
|------|------|
| 日期 | 2026-04-07 |
| 环境 | dev (hci-dev namespace) |
| 部署文档 | [部署](../../deploy/events/2026-04-07-Admin路由冲突与Token鉴权修复部署.md) |

## 验证清单

### V1: 分类基线页 — 路由冲突已解除

**完成标准**：`GET /api/kb/categories` 返回 `{"domains": {...}, "total_domains": N}` 格式

```bash
# 通过网关调用（需使用有效 token）
curl -s http://hci.local/api/kb/categories \
  -H "Authorization: Bearer dev-internalapi-api-token-2026" | jq '.domains | keys'
```

**预期结果**：返回 JSON 包含 `domains` 键（字典类型），内含分组后的分类数据。

**失败标志**：返回 `{"categories": [...]}` 平铺格式 → 路由冲突未解除。

### V2: KBD 审核页 — Token 鉴权通过

**完成标准**：`GET /api/admin/kbd/entries` 返回 200 + 数据列表

```bash
curl -s -o /dev/null -w "%{http_code}" \
  http://hci.local/api/admin/kbd/entries \
  -H "Authorization: Bearer dev-internalapi-api-token-2026"
```

**预期结果**：HTTP 200

**失败标志**：HTTP 401 → api-gateway 未正确注入 Token。

### V3: 分类 LLM 分类不受影响

**完成标准**：`POST /api/kb/classify` 正常工作

```bash
curl -s -X POST http://hci.local/api/kb/classify \
  -H "Authorization: Bearer dev-internalapi-api-token-2026" \
  -H "Content-Type: application/json" \
  -d '{"text": "测试文本", "top_k": 3}' | jq '.status'
```

**预期结果**：返回分类结果或 LLM 连接错误（非 404/500）

### V4: 意图识别不受影响

**完成标准**：`POST /api/kb/classify/intent` 正常工作

```bash
curl -s -X POST http://hci.local/api/kb/classify/intent \
  -H "Authorization: Bearer dev-internalapi-api-token-2026" \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "网络不通"}]}' | jq '.intent'
```

**预期结果**：返回意图识别结果（非 404）

### V5: 前端页面验证

1. 打开 `http://hci.local/admin/` → 分类基线页
2. **预期**：表格加载出分类数据，不再显示"加载分类失败"
3. 切换到 KBD 审核页
4. **预期**：表格加载出 KBD 条目数据，不再显示"加载KBD条目失败"

### V6: 防御性校验（可选）

临时清空 Token 环境变量，验证 500 错误：

```bash
# 在 api-gateway Pod 内验证（不要在生产执行）
kubectl -n hci-dev exec deployment/api-gateway -- \
  env INTERNAL_API_TOKEN="" python -c "
from app.routes.kb import _internal_auth_headers
try:
    _internal_auth_headers()
except Exception as e:
    print(f'防御校验生效: {e}')
"
```

## 验收结论

| 项目 | 结果 | 验证人 | 时间 |
|------|------|--------|------|
| V1 分类基线 | ☐ 通过 / ☐ 未通过 | | |
| V2 KBD 审核 | ☐ 通过 / ☐ 未通过 | | |
| V3 LLM 分类 | ☐ 通过 / ☐ 未通过 | | |
| V4 意图识别 | ☐ 通过 / ☐ 未通过 | | |
| V5 前端页面 | ☐ 通过 / ☐ 未通过 | | |
