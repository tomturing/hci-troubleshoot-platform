# Admin 路由冲突与 Token 鉴权修复部署

| 字段 | 值 |
|------|------|
| 日期 | 2026-04-07 |
| 环境 | dev (hci-dev namespace) |
| 任务文档 | [任务](../../task/events/2026-04-07-Admin路由冲突与Token鉴权修复任务.md) |

## 前置条件

- PR 合并到 main 分支
- K8s 集群 `hci-dev` 命名空间可访问
- `INTERNAL_API_TOKEN` Secret 已存在（当前值 `dev-internalapi-api-token-2026`，无需变更）

## 部署步骤

### 1. 构建镜像

```bash
# 只需重建两个服务
bash scripts/ops/k3s-build.sh kb-service
bash scripts/ops/k3s-build.sh api-gateway
```

### 2. 滚动重启

```bash
kubectl -n hci-dev rollout restart deployment/kb-service
kubectl -n hci-dev rollout restart deployment/api-gateway
```

### 3. 等待就绪

```bash
kubectl -n hci-dev rollout status deployment/kb-service --timeout=120s
kubectl -n hci-dev rollout status deployment/api-gateway --timeout=120s
```

## 回滚方案

```bash
# 回滚到上一个版本
kubectl -n hci-dev rollout undo deployment/kb-service
kubectl -n hci-dev rollout undo deployment/api-gateway
```

## 注意事项

- 本次修复**不涉及**数据库迁移
- 本次修复**不涉及**前端重新构建（token 问题由网关侧解决）
- 本次修复**不涉及** K8s Secret 变更
- 建议先部署 kb-service（解除路由冲突），再部署 api-gateway（Token 注入）
