# HCI 避坑指南（路由触发器）

> **本文件是触发路由器，不含实际条目内容。**  
> 部署类 PIT 条目：`docs/deploy/pitfalls/`，验证类 PIT 条目：`docs/verify/pitfalls/`  
> 部署类路由索引：`docs/deploy/pitfalls/_index.md`  
> 验证类路由索引：`docs/verify/pitfalls/_index.md`

## 触发场景

当用户遇到以下情况时，先读取对应 `_index.md` 获取路由，再按指示读取对应文件：

- 调试进程/服务/状态问题
- 编写或审查 Shell/Python/前端/K8s 代码
- 部署 K8s/Helm 服务，处理镜像/网络/HostPath 问题
- 排查 OpenClaw/Grafana/网络/Clash TUN 问题
- 任何"502/503/超时/SSL/AI不可用"类网络异常

## 快速路由表

| 触发场景 | 读取文件 |
|---------|---------|
| 任何调试/进程/状态问题 | `docs/verify/pitfalls/debugging.md` |
| 网络/502/超时/Clash TUN/LLM | `docs/deploy/pitfalls/network-service-check.md` |
| Shell/Makefile/CI | `docs/deploy/pitfalls/shell.md` |
| Python/ORM/异常 | `docs/verify/pitfalls/python.md` |
| 前端/pnpm/Vue/Docker build | `docs/verify/pitfalls/frontend.md` |
| Dispatcher/状态机/幂等 | `docs/verify/pitfalls/dispatcher.md` |
| K8s/K3s/Helm/镜像 | `docs/deploy/pitfalls/k8s.md` |
| OpenClaw/认证/WS/AI | `docs/verify/pitfalls/openclaw.md` |
| Grafana/Ingress/iframe | `docs/deploy/pitfalls/grafana.md` |

---

