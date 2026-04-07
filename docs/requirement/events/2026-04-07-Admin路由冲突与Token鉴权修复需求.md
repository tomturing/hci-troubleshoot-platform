# Admin 路由冲突与 Token 鉴权修复需求

| 字段 | 值 |
|------|------|
| 日期 | 2026-04-07 |
| 关联 PR | #115（原始修复）→ 部署后发现遗留问题 |
| 优先级 | P0（生产环境页面不可用） |

## 背景

PR #115 修复了管理台三个页面的 API 路由问题并已合并部署，但部署后验证发现：
1. **分类基线页面**：无报错但数据为空
2. **KBD 审核页面**：报错"加载 KBD 条目失败，请刷新重试"

## 问题描述

| 页面 | 现象 | 根因 |
|------|------|------|
| 分类基线 | 无数据 | `classify.py` 的 `GET /api/kb/categories` 先注册→遮蔽 `categories.py` 同路径端点，返回 `{"categories": [...]}` 而非前端期望的 `{"domains": {...}}` |
| KBD 审核 | 401 报错 | 前端 `VITE_INTERNAL_API_TOKEN` 未注入，回退到 `hci-dev-internal-token`；K8s 实际 token 为 `dev-internalapi-api-token-2026` |

## 验收标准

- [ ] 分类基线页面正确展示按域分组的分类数据
- [ ] KBD 审核页面成功加载待审核 KBD 条目
- [ ] `_internal_auth_headers()` 在 token 未配置时返回 500 而非静默传空
