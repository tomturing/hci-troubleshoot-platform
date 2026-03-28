# HCI 避坑指南（路由触发器）

> **本文件是触发路由器，不含实际条目内容。**  
> 所有 PIT 条目已迁移到 `docs/pitfalls/`（唯一权威来源，Git 管理）。  
> 完整内容和编号注册表见：`docs/pitfalls/_index.md`

## 触发场景

当用户遇到以下情况时，读取 `docs/pitfalls/_index.md` 获取路由，再按指示读取对应文件：

- 调试进程/服务/状态问题
- 编写或审查 Shell/Python/前端/K8s 代码
- 部署 K8s/Helm 服务，处理镜像/网络/HostPath 问题
- 排查 OpenClaw/Grafana/网络/Clash TUN 问题
- 任何"502/503/超时/SSL/AI不可用"类网络异常

## 快速路由表

| 触发场景 | 读取文件 |
|---------|---------|
| 任何调试/进程/状态问题 | `docs/pitfalls/debugging.md` |
| 网络/502/超时/Clash TUN/LLM | `docs/pitfalls/network-service-check.md` |
| Shell/Makefile/CI | `docs/pitfalls/shell.md` |
| Python/ORM/异常 | `docs/pitfalls/python.md` |
| 前端/pnpm/Vue/Docker build | `docs/pitfalls/frontend.md` |
| Dispatcher/状态机/幂等 | `docs/pitfalls/dispatcher.md` |
| K8s/K3s/Helm/镜像 | `docs/pitfalls/k8s.md` |
| OpenClaw/认证/WS/AI | `docs/pitfalls/openclaw.md` |
| Grafana/Ingress/iframe | `docs/pitfalls/grafana.md` |

---

