---
status: active
category: deploy
audience: agent
last_updated: 2026-08-06
owner: team
update_trigger: 新增部署坑 / 发现部署问题 / PIT 编号变更
---

# 部署类避坑指南路由索引

> **唯一来源：** `docs/deploy/pitfalls/`（Git 管理，随代码演进）
> **写坑规则：**
> 1. 先在下方"PIT 编号注册表"分配编号（D- 前缀为新编号格式，旧 PIT-xxx 保留）
> 2. 再写入对应分类 file
> 3. 同一 commit/PR 提交，不允许分开提交
>
> **下一个可用编号：D-023**（旧格式延续：PIT-050）

---

## 触发规则（AI Agent 必读）

遇到以下场景，**必须在操作前读取对应文件**，不得跳过：

| 触发场景 | 读取文件 | 当前条目 |
|---------|---------|---------|
| 网络/502/503/超时/SSL/Clash TUN/LLM | [network-service-check.md](network-service-check.md) | §一~十一, PIT-039, PIT-046, D-008 |
| 编写/审查 Shell/Makefile/CI 脚本 / GitHub Actions | [shell.md](shell.md) | PIT-001, PIT-002, D-012, D-016, D-023 |
| K8s/K3s 镜像/Helm/网络/HostPath/DB 迁移/ArgoCD/日志采集器迁移 | [k8s.md](k8s.md) | PIT-014~019, PIT-021, PIT-022, PIT-024, PIT-034, PIT-037, PIT-038, PIT-043, PIT-044, PIT-045, D-001~D-022 |
| ArgoCD 升级/多集群/PreSync SA/Redis EOF/PreSync Hook 镜像/失败 Hook 残留/db-seed Hook 失败 | [k8s.md](k8s.md) | D-001, D-002, D-003, D-004, D-005, D-011, D-014 |
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
| **D-007** | k8s.md | 公网 HTTP 页面访问 localhost 被 PNA 阻止，需启用 HTTPS |
| **D-008** | network-service-check.md | Clash fake-ip-filter 缺失 AI API 域名（coding.dashscope.aliyuncs.com），Pod 内 AI 请求 ConnectTimeout |
| **D-009** | k8s.md | Helm chart 用 emptyDir 覆盖 /etc/nginx/conf.d 导致 nginx 启动无 server block（无 templates 场景） |
| **D-010** | k8s.md | nginx:alpine templates 机制 + emptyDir + fsGroup 三要素缺一导致白屏 |
| **D-011** | k8s.md | ArgoCD PreSync/PostSync Hook Job 失败后长期残留污染 Application Health，hook-delete-policy 必须包含 HookFailed |
| **D-012** | shell.md | GitHub Actions ci.yml 无路径过滤导致局部变更 PR（如仅改 Helm）触发全套 CI（>10min）；uv/pnpm/helm-unittest 三处缓存缺失为主要浪费点；详见 `docs/deploy/events/2026-07-09-CI检查超时分析与优化方案.md` |
| **D-013** | k8s.md | desired_schema.sql 声明式 schema 与 ORM 模型不同步导致 ORM 查询 500（db-migrate.sh 只应用 desired_schema.sql，不应用 atlas-migrations 版本化迁移）|
| **D-014** | k8s.md | db-seed 种子 SQL `$TEMPLATE$` dollar-quote 闭定界符后漏逗号导致 PostSync Hook Job 失败（syntax error at '1.0'）|
| **D-015** | k8s.md | Dockerfile COPY 使用仓库根相对路径但构建脚本传入子目录 context，导致发送超大上下文后 COPY 路径不存在 |
| **D-016** | shell.md | `set -o pipefail` 下 `producer | grep -q` 因 SIGPIPE 返回假失败，镜像导入后验证误报不存在 |
| **D-017** | k8s.md | Alloy/Promtail 切换时首次从文件头回放过期 CRI 日志，导致 Loki 以 timestamp too old 整批拒绝 |
| **D-018** | k8s.md | ConfigMap 通过 subPath 挂载时热 reload 仍读取旧 inode，必须滚动重启工作负载 |
| **D-019** | k8s.md | App-of-Apps 父子 Application 双层 selfHeal 会覆盖本地联调运行态，必须逐层暂停并记录恢复条件 |
| **D-020** | k8s.md | 临时 ConfigMap subPath 覆盖镜像源码，造成新镜像、旧运行代码，必须通过不可变镜像和准入策略治理 |
| **D-021** | k8s.md | Alloy Pod Ready 不等于日志采集和 Loki 写入健康，必须以指标、告警和唯一日志查询验收 |
| **D-022** | k8s.md | `dnsPolicy: None` 缺少 search domain 会破坏集群短服务名；内部依赖使用 ClusterFirst，readiness 必须验证依赖 |
| **D-023** | shell.md | main push 事件缺失时，PR 检查不会发布镜像；CI 必须提供默认不发布、仅 main 可执行的显式 workflow_dispatch 补偿入口，并核验环境仓库远端 tag |

---

## 内容健康状态（季度审计）

| 检查项 | 上次检查 | 状态 |
|--------|---------|------|
| 编号重复检查 | 2026-04-05 | ✅ 迁移自 docs/pitfalls/_index.md |
| 幽灵路径检查 | 2026-04-05 | ✅ 路径已更新 |
| symlink 验证 | — | 运行 `bash scripts/dev/setup-dev-env.sh` |
