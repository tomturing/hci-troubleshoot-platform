# GitHub Copilot 项目指令 — HCI 智能排障平台

## 避坑指南（生成代码或辅助调试前必读）

本项目有经过持续验证的避坑指南，存放于 `docs/pitfalls/`。  
遇到以下场景时，**参考对应文件后再编写代码**，避免已知问题重现：

| 场景关键词 | 必读文件 |
|-----------|---------|
| 调试/进程/状态/日志定位 | `docs/pitfalls/debugging.md` |
| 网络异常/502/超时/Clash TUN | `docs/pitfalls/network-service-check.md` |
| Shell/Makefile/bash 脚本 | `docs/pitfalls/shell.md` |
| Python/FastAPI/SQLAlchemy/Pydantic | `docs/pitfalls/python.md` |
| Vue/TypeScript/pnpm/Docker build/nginx | `docs/pitfalls/frontend.md` |
| K8s/K3s/Helm/镜像导入/HostPath | `docs/pitfalls/k8s.md` |
| OpenClaw/认证 token/WebSocket/AI 超时 | `docs/pitfalls/openclaw.md` |
| Grafana/Traefik Ingress/iframe | `docs/pitfalls/grafana.md` |
| Dispatcher/状态机/幂等 | `docs/pitfalls/dispatcher.md` |

完整触发路由和 PIT 编号注册表见 `docs/pitfalls/_index.md`。

---

## 编码约定

- 代码注释和 Git commit 消息使用**中文**
- Python 包管理：`uv`；前端包管理：`pnpm`；禁止直接使用 pip/npm 安装
- 所有 HTTP 请求日志必须携带 `trace_id`（W3C traceparent 格式）
- 禁止在代码中硬编码 API Key / Token / 密码
- Python lint/format：`ruff`，行长 120，`target-version = "py312"`
- 前端：ESLint + Prettier + TypeScript strict mode

## 测试约定

- 各微服务测试必须隔离运行：`uv run pytest backend/<service>/tests/ -q`
- 禁止跨服务共享 test fixture 导致命名空间冲突

## 部署约定

- K3s 环境每次构建镜像后必须手动导入：`docker save ... | sudo k3s ctr images import -`（见 PIT-016）
- Docker build 在 Clash TUN 宿主机上必须加 `--network host`（见 PIT-028）
