# 服务间身份签名机制

## 背景

为修复 IDOR 越权访问漏洞（CWE-639），系统引入网关签名的身份信任链，确保客户端身份不可伪造，资源归属校验可靠执行。

## 架构

```
┌─────────────┐    X-Client-ID    ┌─────────────┐    X-Client-ID    ┌──────────────────────┐
│   Client    │ ───────────────▶ │ API Gateway │ ───────────────▶ │ Conversation Service │
│             │                   │             │   X-Client-      │                      │
│             │                   │   重签身份   │   Signature       │   验签 + 归属校验     │
└─────────────┘                   └─────────────┘                   └──────────────────────┘
```

## 关键组件

### 1. 签名生成（api-gateway）

位置：`backend/shared/security/signature.py`

```python
from shared.security.signature import sign_client_identity

headers = sign_client_identity(client_id, settings.INTERNAL_API_TOKEN)
# 输出：
# {
#   "X-Client-ID": "user-12345",
#   "X-Client-Signature": "1724523456.a1b2c3d4e5f6..."
# }
```

签名格式：`<timestamp>.<hmac-sha256>`

- `timestamp`：当前 Unix 时间戳（秒级）
- `hmac`：`HMAC-SHA256(secret, "<timestamp>:<client_id>")`

### 2. 签名验证（conversation-service）

位置：`backend/conversation-service/app/security/auth.py`

```python
from app.security.auth import verify_conversation_ownership, verify_case_ownership

# 验证会话归属
await verify_conversation_ownership(conversation_id, request, service)
# - 验证签名有效性
# - 检查会话存在性（404 不存在）
# - 比对 case.client_id（403 非本人）

# 验证工单归属
await verify_case_ownership(case_id, request, service)
```

### 3. 归属比对

跨服务无共享 ORM，使用参数化 SQL 查询：

```python
# backend/conversation-service/app/security/auth.py
result = await service.repository.session.execute(
    text('SELECT client_id FROM "case" WHERE case_id = :case_id'),
    {"case_id": case_id}
)
```

## 配置

### 必需配置

两个服务必须共享同一 `INTERNAL_API_TOKEN`：

- api-gateway：`settings.INTERNAL_API_TOKEN`
- conversation-service：`settings.INTERNAL_API_TOKEN`

Helm 部署时通过 `secrets.internalApiToken` 注入。

### 行为开关

```python
# backend/conversation-service/app/config.py
STRICT_IDENTITY_SIGNATURE: bool = True  # 默认严格模式
```

| 模式  | 无签名请求行为                    | 适用场景                     |
|-------|-----------------------------------|------------------------------|
| True  | 返回 401，记录警告                | 生产环境（推荐）             |
| False | 放行但告警，跳过归属比对          | 过渡期、存量脚本迁移         |

## 防护机制

### 1. 防重放攻击

签名时间戳必须在当前时间 ±300 秒窗口内：

```python
CLOCK_SKEW_SECONDS = 300  # 5 分钟
```

过期签名会被拒绝。

### 2. 防篡改

- Client ID 格式校验：`^[A-Za-z0-9_-]{1,128}$`
- 签名完整性验证：`hmac.compare_digest(expected, actual)`
- 网关剥离客户端伪造的 `X-Client-ID` / `X-Client-Signature` 头

### 3. 零信任集群内访问

即使攻击者绕过 api-gateway 直连 conversation-service:8002，无有效签名仍返回 401，弥补 NetworkPolicy 缺口。

## 前端对接

前端已通过拦截器注入 `X-Client-ID`：

```typescript
// frontend/shared/src/api.ts
axiosInstance.interceptors.request.use(config => {
  config.headers['X-Client-ID'] = getClientId();
  return config;
});
```

网关收到后会重签，无需前端改动。

## 测试验证

```bash
# 运行安全加固测试
pytest tests/unit/test_security_hardening.py -v
```

覆盖场景：
- ✅ 签名往返验证
- ✅ 篡改检测
- ✅ 重放攻击防护
- ✅ 格式校验
- ✅ sim-ssh 合法链路兼容
- ✅ metadata 注入防护

## 故障排查

### 问题：401 Unauthorized

**症状**：合法请求返回 401

**排查步骤**：
1. 检查 `INTERNAL_API_TOKEN` 是否在两个服务中一致
2. 检查前端是否正确发送 `X-Client-ID`
3. 查看网关日志确认重签逻辑执行
4. 临时设置 `STRICT_IDENTITY_SIGNATURE=false` 观察告警日志

### 问题：集群内脚本调用失败

**症状**：直连 conversation-service 的脚本返回 401

**解决方案**：
1. **推荐**：改为通过 api-gateway 调用
2. **过渡**：在 conversation-service 设置 `STRICT_IDENTITY_SIGNATURE=false`，并在日志中监控无签名请求来源

## 变更历史

- 2026-08-25：初始版本，修复 IDOR 漏洞（CWE-639）