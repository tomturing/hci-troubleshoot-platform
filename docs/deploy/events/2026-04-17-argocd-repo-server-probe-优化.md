---
status: active
category: solution
audience: architect, operator
last_updated: 2026-04-17
owner: team
---

# ArgoCD repo-server Liveness Probe 优化方案

## 背景与问题

**问题现象**：
- ArgoCD repo-server Pod 处于 CrashLoopBackOff 状态
- `hci-platform-data-dev` Application sync operation 状态为 Error
- 错误信息：`dial tcp 10.43.205.25:8081: connect: connection refused`

**根因分析**（五层叠加）：

| 层级 | 问题 |
|-----|------|
| L1 | `/healthz?full=true` 调用 gRPC HealthCheck，请求会被排队 |
| L2 | gRPC 队列阻塞时，healthcheck 请求等待超过 HTTP timeout（10秒） |
| L3 | 连续 3 次 timeout → liveness probe 失败 → Pod 被 kill |
| L4 | Pod 重启后再次阻塞 → CrashLoopBackOff 循环 |
| L5 | ArgoCD Application 无法连接 repo-server → sync Error |

**源码分析**（`argocd_repo_server.go`）：

```go
// /healthz?full=true 的实现
if val, ok := r.URL.Query()["full"]; ok && val[0] == "true" {
    // 创建 gRPC 连接（超时 60 秒）
    conn, err := apiclient.NewConnection("localhost:8081", 60, ...)
    
    // 调用 gRPC HealthCheck（会被排队等待）
    client := grpc_health_v1.NewHealthClient(conn)
    res, err := client.Check(r.Context(), &grpc_health_v1.HealthCheckRequest{})
    // ...
}
```

**核心矛盾**：
- gRPC 连接超时：60秒（源码硬编码）
- HTTP request context：继承自 liveness probe timeout（10秒）
- 结果：HTTP context canceled，返回 "context deadline exceeded"

## 方案概述

**核心思路**：分离 liveness 和 readiness probe 的职责

- **liveness probe** → 只检查进程存活（`/healthz`）
- **readiness probe** → 检查功能可用（`/healthz?full=true`）

## 详细设计

### Probe 策略变更

| Probe | 修改前 | 修改后 |
|-------|--------|--------|
| **liveness** | `/healthz?full=true` + timeout 5秒 | `/healthz` + timeout 5秒 |
| **readiness** | 无 | `/healthz?full=true` + timeout 60秒 |

### 配置文件

```yaml
# argocd-repo-server-probe-patch.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: argocd-repo-server
  namespace: argocd
spec:
  template:
    spec:
      containers:
        - name: argocd-repo-server
          livenessProbe:
            httpGet:
              path: /healthz           # 不使用 full=true
              port: 8084
            timeoutSeconds: 5          # 进程存活检查，5秒足够
          
          readinessProbe:
            httpGet:
              path: /healthz?full=true # 完整功能检查
              port: 8084
            timeoutSeconds: 60         # 与 gRPC 连接超时匹配
          
          env:
            - name: ARGOCD_REPO_SERVER_PARALLELISM_LIMIT
              value: "4"                # 限制并发 gRPC 调用
```

### GitOps 管理方式

将 patch 文件放入 `deploy/gitops/argocd-ops/` 目录，由 `argocd-ops` Application 自动同步管理。

## 决策依据 (WHY)

### 方案选择对比

| 方案 | 优点 | 缺点 | 评分 |
|------|------|------|------|
| **A: 分离 liveness/readiness** | 彻底解决阻塞问题；符合 Kubernetes 最佳实践 | 需要修改 ArgoCD Deployment | ★★★★★ |
| B: 增加 timeoutSeconds | 快速实施 | 治标不治本；增加重启延迟 | ★★★☆☆ |
| C: 配置代理 | 加速 git fetch | healthcheck 不经过网络，无效 | ★☆☆☆☆ |
| D: 增加 replicas | 高可用 | 每个 replica 都会遇到同样问题 | ★★☆☆☆ |

### 为什么选择方案 A？

1. **根本性解决**：liveness probe 不再因 gRPC 阻塞而失败
2. **符合设计原则**：liveness 检进程存活，readiness 检功能可用
3. **自动恢复**：gRPC 阻塞时 readiness 失败，完成后自动恢复（无需重启）
4. **业界共识**：参考 [ArgoCD Issue #6106](https://github.com/argoproj/argo-cd/issues/6106) 的讨论

### 为什么不选其他方案？

- **B**: 只是把问题推迟，git fetch 可能超过 60秒
- **C**: `/healthz?full=true` 调用 localhost gRPC，代理对此无效
- **D**: 问题不是高可用，而是单 Pod 稳定性

## 影响范围

### 受影响的组件
- `argocd-repo-server` Deployment（ArgoCD 核心组件）

### 需要更新的文档
- `docs/deploy/部署设计.md`（新增变更历史）

## 实施计划

1. 创建 `deploy/gitops/argocd-ops/argocd-repo-server-probe-patch.yaml`
2. 提交 PR，CI 验证
3. ArgoCD `argocd-ops` Application 自动同步
4. 验证 repo-server Pod 稳定运行

## 验收标准

- [ ] argocd-repo-server Pod 不再 CrashLoopBackOff
- [ ] hci-platform-data-dev Application sync 状态为 Succeeded
- [ ] 所有 Application 运行状态为 Healthy

## 变更历史

| 日期 | 版本 | 变更内容 |
|------|------|---------|
| 2026-04-17 | v1.0 | 初版：分离 liveness/readiness probe 方案 |