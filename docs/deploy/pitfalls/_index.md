---
status: active
category: deploy
audience: agent
last_updated: 2026-04-16
owner: team
update_trigger: 新增部署坑 / 发现部署问题 / PIT 编号变更
---

# 部署类避坑指南路由索引

> **唯一来源：** `docs/deploy/pitfalls/`（Git 管理，随代码演进）
> **写坑规则：**
> 1. 先在下方"PIT 编号注册表"分配编号（D- 前缀为新编号格式，旧 PIT-xxx 保留）
> 2. 再写入对应分类文件
> 3. 同一 commit/PR 提交，不允许分开提交
>
> **下一个可用编号：D-007**（旧格式延续：PIT-048）

---

## 触发规则（AI Agent 必读）

遇到以下场景，**必须在操作前读取对应文件**，不得跳过：

| 触发场景 | 读取文件 | 当前条目 |
|---------|---------|---------|
| 网络/502/503/超时/SSL/Clash TUN/LLM | [network-service-check.md](network-service-check.md) | §一~十一, PIT-039, PIT-046 |
| 编写/审查 Shell/Makefile/CI 脚本 | [shell.md](shell.md) | PIT-001, PIT-002 |
| K8s/K3s 镜像/Helm/网络/HostPath/DB 迁移/ArgoCD | [k8s.md](k8s.md) | PIT-014~019, PIT-021, PIT-022, PIT-024, PIT-034, PIT-037, PIT-038, PIT-043, PIT-044, PIT-045, D-001, D-002, D-003, D-004 |
| ArgoCD 升级/多集群/PreSync SA/Redis EOF/PreSync Hook 镜像 | [k8s.md](k8s.md) | D-001, D-002, D-003, D-004, D-005 |
| Grafana 重定向/Ingress/iframe 白屏 | [grafana.md](grafana.md) | PIT-011, PIT-012, PIT-020, PIT-036 |

---

## PIT 编号注册表（部署类）

> 旧版全局编号注册表见 git 历史 `docs/pitfalls/_index.md`。
> 此处仅登记隶属本目录的 PIT 条目，防止新增编号重复。

| 编号 | 文件 | 描述 |
|------|------|------|
| PIT-001 | shell.md | here-doc 缩进问题 |
| PIT-002 | shell.md | nohup 后台进程 |
| PIT-011 | grafana.md | localhost 重定向 |
| PIT-012 | grafana.md | 空 rules |
| PIT-014 | k8s.md | Clash ClusterIP 冲突 |
| PIT-015 | k8s.md | Helm pending |
| PIT-016 | k8s.md | K3s 镜像导入 |
| PIT-017 | k8s.md | RESTARTS 虚高 |
| PIT-018 | k8s.md | HostPath 截断 |
| PIT-019 | k8s.md | UID 不匹配 |
| PIT-020 | grafana.md | IP 访问 iframe |
| PIT-021 | k8s.md | Traefik 端口 |
| PIT-022 | k8s.md | DB 密码特殊字符 |
| PIT-024 | k8s.md | Traefik 跨 NS |
| **PIT-031** | — | **预留（已删除/合并，禁止复用）** |
| PIT-033 | k8s.md | K3s 服务检查 RunBook |
| PIT-034 | k8s.md | fake-ip |
| PIT-036 | grafana.md | /grafana 路由 |
| PIT-037 | k8s.md | Docker build apt/pip |
| PIT-038 | k8s.md | Docker 172.16 端口映射 |
| PIT-039 | network-service-check.md | CoreDNS hosts 插件冲突 |
| PIT-043 | k8s.md | ArgoCD Application 手动覆盖导致 releaseName 漂移 |
| PIT-044 | k8s.md | 迁移体系切换后遗留触发器双倍计数 |
| PIT-045 | k8s.md | nginx 启动时 upstream DNS 解析失败 |
| PIT-046 | network-service-check.md | WSL resolv.conf 自动生成为 Clash fake-IP DNS（10.255.255.254），K3s 全局 DNS 劫持 |
| **D-001** | k8s.md | ArgoCD 多集群 App of Apps 分层 + 环境标识方式 |
| **D-002** | k8s.md | K3s + ECR 镜像离线导入（docker pull → tag → k3s ctr import）|
| **D-003** | k8s.md | ArgoCD PreSync Job 依赖 SA 鸡蛋问题（首次部署需手动预创建 SA）|
| **D-004** | k8s.md | ArgoCD v3.x repo-server Redis EOF（连接池空闲）+ K8s Pod git 网络（Clash TUN 不拦截 flannel 流量）|
| **D-005** | k8s.md | ArgoCD PreSync/PostSync Hook 需使用包含目标工具的镜像（kubectl/helm/aws CLI）|
| **D-006** | k8s.md | GitHub PAT 失效导致 ghcr.io 镜像拉取失败（ImagePullBackOff）|

---

## 内容健康状态（季度审计）

| 检查项 | 上次检查 | 状态 |
|--------|---------|------|
| 编号重复检查 | 2026-04-05 | ✅ 迁移自 docs/pitfalls/_index.md |
| 幽灵路径检查 | 2026-04-05 | ✅ 路径已更新 |
| symlink 验证 | — | 运行 `bash scripts/dev/setup-dev-env.sh` |