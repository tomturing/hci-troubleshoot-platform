---
status: active
category: task
audience: developer
last_updated: 2026-03-28
owner: team
related: 29
---

# Task 29：Prometheus 进 K3s（P1）

```
你是一名负责 hci-troubleshoot-platform K3s 可观测性基础设施的 agent。

【仓库】
git clone https://github.com/tomturing/hci-troubleshoot-platform.git
cd hci-troubleshoot-platform

【背景】
Prometheus 的 Helm Chart 已完整编写（由前序开发完成），包含：
  - deploy/helm/hci-platform/templates/observability/prometheus.yaml
    └─ ConfigMap（含 K8s Pod SD 自动发现）+ Deployment + Service + ServiceAccount + ClusterRole + ClusterRoleBinding
  - 各服务 Helm Deployment 模板已有 prometheus.io/scrape 注解：
    api-gateway / case-service / conversation-service / scheduler-service / kb-service
  - values.yaml 已有 observability.prometheus.enabled: true

本任务目标：**验证代码正确性 → 执行 helm upgrade 部署 → 验收指标采集**。
若发现 Helm 模板有 bug，直接修改后再部署。

【前置检查 - 必读】

Step 0: 检查并理解现有代码
  # 读取 Prometheus Helm 模板
  cat deploy/helm/hci-platform/templates/observability/prometheus.yaml

  # 确认各服务均有 scrape 注解
  grep -rn "prometheus.io/scrape" deploy/helm/hci-platform/templates/ --include="*.yaml"

  # 确认 values.yaml prometheus 配置
  grep -A10 "prometheus:" deploy/helm/hci-platform/values.yaml

【执行步骤】

Step 1: Helm 模板 lint 验证（先 dry-run，不实际部署）
  helm lint deploy/helm/hci-platform/
  helm template hci-platform deploy/helm/hci-platform/ \
    -f deploy/helm/hci-platform/values-dev.yaml \
    | grep -A50 "name: prometheus"

Step 2: 检查 K3s 集群现状
  # 检查 Prometheus 是否已运行（可能前序已部署）
  kubectl get pods -n hci-observability | grep prometheus
  kubectl get pods -n hci-troubleshoot --show-labels | head -20

Step 3: 执行 helm upgrade 部署
  # 注：K3s 环境使用 values-dev.yaml 覆盖
  helm upgrade --install hci-platform deploy/helm/hci-platform/ \
    -n hci-troubleshoot \
    -f deploy/helm/hci-platform/values-dev.yaml \
    --wait --timeout 3m

Step 4: 验证 Prometheus Pod 启动
  kubectl get pods -n hci-observability | grep prometheus
  # 等待 Running 状态（最多 2 分钟）
  kubectl wait --for=condition=ready pod -l app.kubernetes.io/name=prometheus \
    -n hci-observability --timeout=120s

Step 5: 验证服务指标可达
  # conversation-service 是否暴露 /metrics
  CONV_POD=$(kubectl get pod -n hci-troubleshoot -l app.kubernetes.io/name=conversation-service \
    -o jsonpath='{.items[0].metadata.name}')
  kubectl exec -n hci-troubleshoot $CONV_POD -- wget -qO- http://localhost:8002/metrics \
    | grep -E "hci_ai_requests_total|hci_ai_ttft"

Step 6: 验证 Prometheus 成功抓取业务服务
  # 通过 kubectl exec 进入 Prometheus 查询
  PROM_POD=$(kubectl get pod -n hci-observability -l app.kubernetes.io/name=prometheus \
    -o jsonpath='{.items[0].metadata.name}')
  
  # 检查抓取目标状态
  kubectl exec -n hci-observability $PROM_POD -- \
    wget -qO- "http://localhost:9090/api/v1/targets" | python3 -m json.tool | grep -E "health|job|lastError"
  
  # 查询 AI 指标是否已采到
  kubectl exec -n hci-observability $PROM_POD -- \
    wget -qO- "http://localhost:9090/api/v1/query?query=hci_ai_requests_total" \
    | python3 -m json.tool

Step 7: 配置 Grafana 数据源
  # Prometheus Service 地址
  echo "Grafana 数据源 URL: http://prometheus.hci-observability.svc.cluster.local:9090"
  
  # 通过 API 添加（如 Grafana 已启动）
  GRAFANA_POD=$(kubectl get pod -n hci-observability -l app.kubernetes.io/name=grafana \
    -o jsonpath='{.items[0].metadata.name}')
  kubectl exec -n hci-observability $GRAFANA_POD -- \
    wget -qO- --post-data='{"name":"Prometheus","type":"prometheus","url":"http://prometheus:9090","access":"proxy","isDefault":true}' \
    --header="Content-Type: application/json" \
    http://admin:admin@localhost:3000/api/datasources 2>/dev/null || \
    echo "请手动在 Grafana UI 中添加数据源（admin 登录后 → Data Sources → Add Prometheus）"

Step 8: E2E 指标验证（发起一次 AI 对话后验证指标出现）
  # 发起对话（测试用）
  curl -s -X POST "http://192.168.0.4:4888/api/conversations/{conversation_id}/messages" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer hci-dev-internal-token" \
    -d '{"content": "Prometheus 指标验证测试消息"}' > /dev/null

  # 等待 15 秒（Prometheus 抓取间隔）
  sleep 15

  # 验证 TTFT 指标出现
  kubectl exec -n hci-observability $PROM_POD -- \
    wget -qO- "http://localhost:9090/api/v1/query?query=hci_ai_ttft_seconds_bucket" \
    | python3 -m json.tool | grep '"result"' 

【常见问题排查】

若 Prometheus Pod 启动失败：
  kubectl describe pod -n hci-observability -l app.kubernetes.io/name=prometheus
  kubectl logs -n hci-observability -l app.kubernetes.io/name=prometheus

若指标抓取为空（0 targets）：
  # RBAC 问题 - 检查 ClusterRoleBinding
  kubectl get clusterrolebinding prometheus-hci
  kubectl auth can-i list pods --as=system:serviceaccount:hci-observability:prometheus -n hci-troubleshoot

若 scrape_configs 中的 namespace 过滤不生效：
  # 检查 ConfigMap 名称引用是否正确
  kubectl get configmap -n hci-observability | grep prometheus
  kubectl describe configmap prometheus-config -n hci-observability

【约束】
- 不修改非可观测性相关的 Helm 模板
- 若发现 Helm 模板 bug，修改后直接 helm upgrade，记录修改内容在 PR 描述中
- Prometheus 使用 emptyDir 存储（已在模板中配置，适合测试环境）

【验收标准】
- [ ] kubectl get pods -n hci-observability | grep prometheus 显示 Running
- [ ] Prometheus /api/v1/targets 显示 hci-services job 中至少 3 个 target 状态为 up
- [ ] kubectl exec ... wget prometheus /api/v1/query?query=hci_ai_requests_total 有数据（发一次对话后）
- [ ] Grafana → Data Sources → Prometheus 测试连接成功
- [ ] hci_ai_ttft_seconds_bucket 在 Grafana Explore 中可查询到

完成后提交 PR，描述中列出：helm upgrade 执行结果截图（或命令输出）、targets 状态截图、Grafana 查询截图（3 项证据）。
```

---