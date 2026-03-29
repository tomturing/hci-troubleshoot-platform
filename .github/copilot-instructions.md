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

## Git 推送规则（强制）

### 文档门禁
改动 `backend/`、`frontend/`、`deploy/`、`scripts/`、`database/`、`.github/workflows/` 时，
**必须在同一 commit/PR 中**同步更新 `docs/`、`README.md`、`AGENTS.md` 或 `CLAUDE.md` 至少一项。
否则 CI `docs-governance` job 失败，PR 无法合并。

### 分支与 PR 流程
- main 分支有保护规则，**禁止直接推送**，必须通过 PR
- 提交流程：创建 feature/hotfix 分支 → 推送远程 → 创建 PR → CI 全绿后合并

### PIT-023：并发 hotfix 前置检查
创建 hotfix 分支**前**必须先执行：
```bash
gh pr list --state open
```
确认无其他 PR 正在修改同一目录。有并发 PR 时先协调合并，避免产生重复配置块。

### PIT-024：安全基线改造必须分批 PR
全量修改 `securityContext`、`probe`、`resources.limits` 时，
必须按负载类型（**nginx / Python / Node.js**）拆成独立 PR，
不可一次提交跨多种运行时的安全基线变更。

### PIT-025：修改 runAsNonRoot 前确认镜像文件系统
修改 `securityContext.runAsUser` 或 `runAsNonRoot` 前，确认镜像在非 root 下的写权限需求：
- **nginx 官方镜像**：需写 `/var/cache/nginx` 和 `/var/run`，必须挂载 `emptyDir` 覆盖这两个路径
- Python/Node.js 镜像：确认应用日志、临时文件写入路径的权限
