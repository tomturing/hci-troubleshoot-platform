---
status: planned
category: task
audience: developer, operator, release, tester
last_updated: 2026-08-10
owner: team
---

# K3s 受管 terminal_bridge 启用任务

对应方案：[K3s 受管 terminal_bridge 启用方案](../../solution/events/2026-08-10-K3s受管terminal_bridge启用方案.md)

## 1. 任务状态定义

```text
planned → in_progress → blocked → done
```

`done` 只能在代码、环境仓库、Argo 同步、端到端证据和回滚演练全部通过后使用。仅 Helm template PASS 不得标记完成。

## 2. P0：基线与配置权威源

- [ ] 记录当前 dev Argo Application、source、targetRevision、valuesFiles 和实际 Pod 镜像 digest。
- [ ] 确认外部环境仓库 `hci-platform-env/environments/dev/values.yaml` 是实际生效配置。
- [ ] Inventory 当前 Linux Docker Bridge、Windows Bridge、K3s Service/Ingress 的监听地址和用途。
- [ ] 为 dev 选择唯一执行轨：K3s cluster Bridge；记录旧 Bridge 作为回滚目标。
- [ ] 清理或标记所有临时 `kubectl patch`，避免误将现场状态当作 GitOps 事实。
- [ ] 评审 `replicaCount=1` 约束，并为副本数大于 1 增加 Helm/CI fail gate。

## 3. P0：Helm、Ingress 和安全策略

- [ ] 校验 terminal-bridge Deployment/Service/PVC 模板与命名空间、selector、端口一致。
- [ ] 增加/校验 `/terminal-bridge` WebSocket Ingress 路由和 Upgrade/timeout 配置。
- [ ] 校验 Customer UI `terminalBridgeUrl` 优先级及 runtime-config 实际输出。
- [ ] 增加 cluster 模式 same-origin Origin 白名单的默认值和非法 `*` 门禁。
- [ ] 为 Bridge 增加 NetworkPolicy：允许 UI ingress、hci-sim SSH egress、DNS/OTel，拒绝 DB/K8s API/真实 HCI 默认出站。
- [ ] 校验非 root、drop capabilities、seccomp、无 hostNetwork/privileged/docker.sock。
- [ ] 校验 PVC/known_hosts 权限和容器 UID 一致性。
- [ ] Helm lint、template、unittest、NetworkPolicy 静态检查全部通过。

## 4. P0：镜像与 GitOps 发布

- [ ] 构建 `hci-terminal-bridge` 不可变 tag/digest。
- [ ] 将镜像同步到 K3s 可拉取位置，确认 imagePullSecret/离线导入策略。
- [ ] 在环境仓库提交 `terminalBridge.enabled=true`、`replicaCount=1`、allowedOrigins 和 image tag。
- [ ] 创建外部环境仓库 PR，带 `bug`/`enhancement`/`documentation` 及环境标签（按仓库约定）。
- [ ] 合并后等待 Argo 同步，禁止只做现场 patch。
- [ ] 保留一个只关闭 cluster Bridge、恢复桌面 Bridge URL 的回滚提交。

## 5. P1：协议与运行时回归

- [ ] `ssh_connect` 成功/失败契约测试。
- [ ] `ssh_exec_process` stdout/stderr/exit code/timeout/cancel/truncation 契约测试。
- [ ] Origin 拒绝、Lease audience/issuer/TTL/jti/max command 检查。
- [ ] session 与 `case_id/test_run_id/execution_mode` 绑定测试。
- [ ] Pod 重启后旧 session 明确失败；新 Lease 可建立新 session。
- [ ] 多副本配置拒绝测试。
- [ ] 不允许普通 Markdown/用户输入旁路自动执行测试。

## 6. P1：可观测性与运行手册

- [ ] `/health/live`、`/health/ready`、`/status`、`/metrics` 端到端可访问。
- [ ] Prometheus scrape、Loki 结构化日志和 Alert rule 配置。
- [ ] 统一 `trace_id/test_run_id/exec_id/case_id` 关联字段；验证无 secret。
- [ ] 编写 Pod Pending、ImagePullBackOff、Origin 403、SSH timeout、Lease reject、exec timeout Runbook。
- [ ] 更新 `docs/deploy/terminal-bridge-k3s.md`、部署指南、发布指南和可观测性设计。

## 7. P1：真实 dev 验收

- [ ] 通过 Admin/现有 smoke 创建一个 sim-ssh Lease。
- [ ] 浏览器 Network 证据为同源 `/terminal-bridge`，无 `localhost` 请求。
- [ ] 记录同一 TestRun 的 Ingress 101、Bridge `ssh.connected`/`exec.done`、hci-sim route/exec。
- [ ] 验证错误 Origin、错误 Lease、不可达端口、非零退出码和输出截断。
- [ ] 观察窗口内确认无旧 Docker/Windows Bridge 混流。
- [ ] 完成回滚演练，再恢复 cluster 模式并记录结果。

## 8. P2：后续高可用（不阻断本阶段）

- [ ] 设计按 TestRun owner 的一致性路由。
- [ ] 评估会话元数据外置、连接 owner 迁移和 drain 机制。
- [ ] 进行双副本故障注入和容量基准；未完成前禁止提高副本数。

## 9. 完成定义

- 所有 P0/P1 任务勾选；
- 代码和环境仓库 PR 均合并；
- Argo 健康、Bridge smoke 和负向测试通过；
- 验证文档有真实命令/日志/指标/时间戳证据；
- 现行全量文档与 README 第一屏已更新；
- 无遗留临时 patch、未关闭旧 Bridge 或未记录的回滚分支。
