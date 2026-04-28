# K3s环境部署Ops-Agent指南

## 前提条件

- k3s集群已运行
- HCI-TP已部署在 `hci-dev` 命名空间
- 两个项目的feature分支已更新

---

## 部署步骤

### 第一步：构建Ops-Agent镜像

```bash
cd ops-agent

# 构建镜像
docker build -f Dockerfile.ops-server -t ops-agent:latest .

# 检查镜像
docker images | grep ops-agent
```

### 第二步：将镜像导入到k3s

```bash
# 如果用k3d
# k3d image import ops-agent:latest --cluster your-cluster-name

# 如果用标准k3s（用containerd）
# 需要先保存为tar，再导入
docker save ops-agent:latest -o ops-agent.tar

# 复制到k3s节点并导入
# scp ops-agent.tar user@k3s-node:/tmp/
# ssh user@k3s-node "sudo k3s ctr images import /tmp/ops-agent.tar"

# 或者如果你在k3s节点上直接操作
sudo k3s ctr images import ops-agent.tar

# 验证镜像
sudo k3s ctr images ls | grep ops-agent
```

### 第三步：部署Ops-Agent服务

```bash
cd hci-troubleshoot-platform

# 方式1: 直接应用yaml文件
kubectl apply -f deploy/helm/hci-platform/templates/ops-agent-service/ -n hci-dev

# 方式2: 如果已集成到Helm chart，直接升级
cd deploy/helm
helm upgrade hci-platform ./hci-platform \
    -f ./hci-platform/values.yaml \
    -f ./hci-platform/values-opsagent-dev.yaml \
    -n hci-dev \
    --create-namespace
```

### 第四步：更新Conversation-Service配置

```bash
# 更新环境变量
kubectl set env deployment/conversation-service \
    OPS_AGENT_ENABLED=true \
    OPS_AGENT_BASE_URL=http://ops-agent-service:8006 \
    -n hci-dev

# 或者通过Helm升级（推荐）
cd deploy/helm
helm upgrade hci-platform ./hci-platform \
    -f ./hci-platform/values.yaml \
    -f ./hci-platform/values-opsagent-dev.yaml \
    -n hci-dev
```

### 第五步：验证部署

```bash
# 检查Pod状态
kubectl get pods -n hci-dev

# 查看日志
kubectl logs -f deployment/ops-agent-service -n hci-dev
kubectl logs -f deployment/conversation-service -n hci-dev

# 测试OA服务健康检查
kubectl port-forward service/ops-agent-service 8006:8006 -n hci-dev
# 在另一个终端
curl http://localhost:8006/health
```

---

## 快速部署脚本

如果想快速尝试，可以用这个简化流程：

```bash
# 1. 创建一个简单的deployment（临时测试用）
cat > /tmp/ops-agent-deployment.yaml << 'EOF'
apiVersion: v1
kind: ConfigMap
metadata:
  name: ops-agent-config
  namespace: hci-dev
data:
  ops_config.yaml: |
    agents:
      enable_lakeview: false
      ops_agent:
        model: glm47-openrouter
        max_steps: 500
        sop_catalog_path: data/case_sop_data/af/sop/node_sops.jsonl
        query_sub_agent:
          model: glm47-openrouter
          max_steps: 80
          tool_call_budget: 40
          budget_warning_threshold: 10
    model_providers:
      openrouter-example:
        api_key: ""
        base_url: https://openrouter.ai/api/v1
        provider: openrouter
    models:
      glm47-openrouter:
        model_provider: openrouter-example
        model: z-ai/glm-4.7
        max_tokens: 8192
        temperature: 0.8
        top_p: 0.95
        top_k: 0
        max_retries: 3
        parallel_tool_calls: false
        enable_thinking: false
---
apiVersion: v1
kind: Service
metadata:
  name: ops-agent-service
  namespace: hci-dev
spec:
  type: ClusterIP
  ports:
  - port: 8006
    targetPort: 8006
    protocol: TCP
    name: http
  selector:
    app: ops-agent-service
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ops-agent-service
  namespace: hci-dev
spec:
  replicas: 1
  selector:
    matchLabels:
      app: ops-agent-service
  template:
    metadata:
      labels:
        app: ops-agent-service
    spec:
      containers:
      - name: ops-agent
        image: ops-agent:latest  # 确保这个镜像已导入
        imagePullPolicy: IfNotPresent
        ports:
        - name: http
          containerPort: 8006
          protocol: TCP
        env:
        - name: OPS_CONFIG_PATH
          value: /app/ops_config.yaml
        - name: HCI_TP_ENABLED
          value: "true"
        - name: OTEL_EXPORTER_OTLP_ENDPOINT
          value: http://tempo.hci-observability.svc.cluster.local:4317
        volumeMounts:
        - name: ops-config
          mountPath: /app/ops_config.yaml
          subPath: ops_config.yaml
        resources:
          requests:
            cpu: "100m"
            memory: "128Mi"
          limits:
            cpu: "500m"
            memory: "512Mi"
        livenessProbe:
          httpGet:
            path: /health/live
            port: http
          initialDelaySeconds: 10
          periodSeconds: 30
        readinessProbe:
          httpGet:
            path: /health/ready
            port: http
          initialDelaySeconds: 5
          periodSeconds: 10
      volumes:
      - name: ops-config
        configMap:
          name: ops-agent-config
EOF

# 2. 应用配置
kubectl apply -f /tmp/ops-agent-deployment.yaml

# 3. 更新conversation-service
kubectl set env deployment/conversation-service \
    OPS_AGENT_ENABLED=true \
    OPS_AGENT_BASE_URL=http://ops-agent-service:8006 \
    -n hci-dev
```

---

## 验证功能

部署完成后：

1. 访问HCI-TP前端
2. 创建新工单
3. 在助手选择中，应该能看到 "ops-agent"
4. 选择 "ops-agent" 并发送测试消息
5. 验证能收到响应

---

## 回滚

如果需要回滚：

```bash
# 方式1: 只禁用OA助手（快速）
kubectl set env deployment/conversation-service \
    OPS_AGENT_ENABLED=false \
    -n hci-dev

# 方式2: 完整回滚
kubectl delete -f /tmp/ops-agent-deployment.yaml 2>/dev/null || true
kubectl delete -f deploy/helm/hci-platform/templates/ops-agent-service/ -n hci-dev 2>/dev/null || true

# 方式3: 通过Helm回滚
helm rollback hci-platform -n hci-dev
```

---

## 故障排查

### OA服务无法启动
```bash
# 查看描述
kubectl describe deployment/ops-agent-service -n hci-dev

# 查看日志
kubectl logs deployment/ops-agent-service -n hci-dev
```

### conversation-service无法连接OA
```bash
# 检查服务是否可访问
kubectl run -it --rm --restart=Never debug --image=curlimages/curl:latest -- sh -n hci-dev
# 在容器中
curl http://ops-agent-service:8006/health

# 检查DNS解析
nslookup ops-agent-service.hci-dev.svc.cluster.local
```
