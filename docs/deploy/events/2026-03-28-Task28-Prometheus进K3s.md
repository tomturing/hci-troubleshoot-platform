---
status: active
category: task
audience: developer
last_updated: 2026-03-28
owner: team
related: 28
---

# Task 28：Prometheus 进 K3s

```
你是一名负责 hci-troubleshoot-platform K3s 可观测性基础设施的 agent。

【仓库】
git clone https://github.com/tomturing/hci-troubleshoot-platform.git
cd hci-troubleshoot-platform

【背景】
各服务已通过 shared/utils/metrics.py 暴露 Prometheus 格式指标（/metrics 端点），但 K3s 集群中无 Prometheus 实例采集。Grafana 仪表盘中的 AI 指标面板当前为空。

【任务目标】

Step 1：新建 Helm 模板
deploy/helm/hci-platform/templates/observability/prometheus.yaml

内容：Prometheus Deployment + Service + ServiceAccount + RBAC ClusterRole + ConfigMap（scrape_configs）

ConfigMap 中 scrape_configs 使用 Kubernetes SD（pod 级自动发现），通过 Annotation 过滤：
  prometheus.io/scrape: "true"
  prometheus.io/port: <port>
  prometheus.io/path: /metrics

Step 2：在各服务的 Helm Deployment 模板 pod annotations 中添加：
  prometheus.io/scrape: "true"
  prometheus.io/port: "800x"   # 各服务对应端口
  prometheus.io/path: "/metrics"

涉及文件：deploy/helm/hci-platform/templates/ 下各服务 deployment yaml

Step 3：确认 values.yaml 中有 prometheus.enabled: true 配置项（默认 true）

完整步骤见 docs/09_项目进展.md §四 P1-3。

【约束】
- 不修改非可观测性相关的 Helm 模板
- Prometheus 使用 emptyDir 存储（不需要 PVC，适合测试环境）
- RBAC 权限最小化（只需要 pods 的 get/list/watch）

【验收标准】
- helm upgrade 后 kubectl get pods -n hci-troubleshoot | grep prometheus 有 Running pod
- kubectl exec deploy/prometheus -- wget -qO- http://conversation-service:8002/metrics | grep ai_requests_total 有输出
- Grafana → Data Sources → 添加 Prometheus（URL: http://prometheus:9090）可成功 Test
- 发起一次 AI 对话后，Grafana 可查询 ai_ttft_seconds_bucket 有数据

完成后提交 PR，等待 Claude 审核。
```

---

# 19_任务编排（续）

> 本文件收录后续待执行任务的 Agent Prompt，供分发给其他 agent 承接开发。
> 优先级说明：P1 = 生产环境完整性；P2 = 功能完善
> 创建日期: 2026-03-12

---