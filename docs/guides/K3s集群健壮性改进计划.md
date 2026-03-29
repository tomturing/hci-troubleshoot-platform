---
status: active
category: guide
audience: developer
last_updated: 2026-03-28
owner: team
related: guides/K3s集群运维复盘.md
---

# K3s 集群健壮性改进计划

> **用途**：针对复盘报告（[K3s集群运维复盘.md](K3s集群运维复盘.md)）识别的 10 类共 50 个问题，
> 参考业界最佳实践制定全面且可落地的改进方案。  
> **阅读前置**：请先阅读复盘报告，了解每类问题的根因。

---

## 改进总览

| 类别 | 改进方向 | 实施阶段 | 预估工作量 |
|------|---------|---------|----------|
| A 镜像版本不一致 | 构建-导入-校验原子化 + 不可变 tag | Sprint 1 | 0.5d |
| B 配置漂移/热重载 | Reloader Operator + DB 迁移自动化 | Sprint 1 | 1d |
| C 资源生命周期 | OwnerReference + 部署前 Guard | Sprint 2 | 0.5d |
| D 网络/DNS 污染 | DNS 配置代码化 + 代理配置持久化 | Sprint 2 | 0.5d |
| E Helm 配置规范 | values.schema.json + Helm Unit Test | Sprint 2 | 1d |
| F 代码数据一致性 | Unit of Work 规范 + 契约注释 | Sprint 1 | 1d |
| G 接口契约不对齐 | OpenAPI 契约优先 + Consumer Contract Testing | Sprint 3 | 2d |
| H 运行时静默失效 | 快速失败原则 + 结构化错误传播 | Sprint 1 | 1d |
| I 工程架构缺陷 | 六边形架构 + 标准化 DI 规范 | Sprint 2 | 1.5d |
| J 容器环境适配 | 容器优先开发规范 + 探针标准化 | Sprint 2 | 1d |
| 横向：静默失效 | 错误传播规范 + Alerting SLO | Sprint 3 | 1d |
| 横向：跨版本漂移 | Schema 共享锁定 + 版本协商协议 | Sprint 3 | 2d |

> Sprint 时长建议：2 周一个 Sprint；上述估算均为额外改造工时，不含现有功能开发。

---

## 第一部分：基础设施层改进（A/B/C/D/E）

---

### A 类改进：镜像版本不一致

**目标状态**：build → import → deploy 全链路可验证，零人工确认步骤。

#### A-1 构建-导入-校验原子化脚本

**业界实践**：Google SRE 工程规范要求部署流程是原子操作——要么全部成功，要么明确失败并回滚，不允许"部分成功"状态。

当前 `k3s-build.sh` 只管构建，导入和校验完全依赖手工。改造思路：

```bash
# scripts/ops/k3s-build.sh 末尾追加以下逻辑（伪代码展示改造范围）
build_and_import() {
  local svc="$1"; local tag="$2"

  # Step 1: 构建
  docker build -t "hci-${svc}:${tag}" "backend/${svc}"

  # Step 2: 导入 k3s containerd（失败立即退出）
  docker save "hci-${svc}:${tag}" | sudo -n k3s ctr images import - || {
    echo "[ERROR] import 失败: hci-${svc}:${tag}" >&2; return 1
  }

  # Step 3: 校验镜像确实存在（防止 import 静默失败）
  sudo -n k3s ctr images ls | grep -q "hci-${svc}:${tag}" || {
    echo "[ERROR] 校验失败: hci-${svc}:${tag} 未出现在 containerd 中" >&2; return 1
  }

  # Step 4: 双仓模式：打 ghcr.io 路径 tag，与 env repo 对齐
  sudo -n k3s ctr images tag \
    "docker.io/library/hci-${svc}:${tag}" \
    "ghcr.io/tomturing/hci-troubleshoot-platform/hci-${svc}:${tag}"

  echo "[OK] hci-${svc}:${tag} 导入并校验通过"
}
```

**改造文件**：`scripts/ops/k3s-build.sh`

#### A-2 不可变镜像 tag 规范

**业界实践**：Netflix、Airbnb 等都强制禁止在生产流程中使用 `latest` 或 `main` 这类可变 tag，改用每次构建唯一的内容寻址 tag（`<date>-<short-sha>`）。

已有的 tag 格式 `2026.03.28-1200-abc1234` 符合规范，需要补充的是：

```yaml
# deploy/helm/hci-platform/values.yaml — 补充注释说明规范
global:
  imagePullPolicy: IfNotPresent  # 永远不要改成 Always（Always 会绕过本地镜像）
  # tag 格式规范：<YYYY.MM.DD>-<HHMM>-<git-short-sha>，由 k3s-build.sh 自动生成
  # 禁止使用 latest / main / dev 这类可变 tag
```

#### A-3 孤立 Pod 清理 pre-hook

scheduler-service 动态创建的 Pool Pod 没有 OwnerReference，Helm 无法追踪。在每次部署前自动清理：

```bash
# scripts/ops/k3s-deploy-dualrepo.sh 的 pre-deploy 步骤中追加
pre_deploy_cleanup() {
  local ns="$1"
  echo "[PRE-DEPLOY] 清理 scheduler 动态创建的孤立 Pod..."
  kubectl delete pod \
    -l managed-by=scheduler-service \
    -n "${ns}" \
    --ignore-not-found \
    --grace-period=10
}
```

同时要求 scheduler-service 在创建 Pool Pod 时统一打 label：

```python
# backend/scheduler-service/app/services/k8s_client.py
pod_manifest = {
    "metadata": {
        "labels": {
            "app": f"{assistant_type}-pool",
            "managed-by": "scheduler-service",   # ← 新增，供清理时 selector 使用
            "assistant-type": assistant_type,
        }
    }
}
```

**改造文件**：`scripts/ops/k3s-build.sh`、`scripts/ops/k3s-deploy-dualrepo.sh`、`backend/scheduler-service/app/services/k8s_client.py`

---

### B 类改进：配置漂移 / 热重载失效

**目标状态**：ConfigMap/Secret 变更自动触发应用重载，DB schema 随服务启动自动对齐，不漂移。

#### B-1 Stakater Reloader — ConfigMap/Secret 变更自动 Rollout

**业界实践**：[Stakater Reloader](https://github.com/stakater/Reloader) 是 CNCF 景观中广泛使用的 K8s Operator，监听 ConfigMap/Secret 变化并自动触发关联 Deployment/StatefulSet 的 rollout restart，无需手动操作。

**方案一（推荐）：部署 Reloader Operator**

```yaml
# deploy/helm/hci-platform-infra/values.yaml 新增 reloader 配置
reloader:
  enabled: true
  # 通过 Helm dependency 引入
  # https://github.com/stakater/Reloader/tree/master/deployments/kubernetes/chart

# Deployment 上打 annotation 声明监听哪个 ConfigMap
# deploy/helm/hci-platform/templates/conversation-service/deployment.yaml
metadata:
  annotations:
    reloader.stakater.com/auto: "true"
    # 或精确指定：
    # configmap.reloader.stakater.com/reload: "hci-platform-config"
    # secret.reloader.stakater.com/reload: "hci-platform-secrets"
```

**方案二（轻量）：Helm post-upgrade hook 触发 rollout**

```yaml
# deploy/helm/hci-platform/templates/hooks/post-upgrade-restart.yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: post-upgrade-rollout-restart
  annotations:
    "helm.sh/hook": post-upgrade
    "helm.sh/hook-weight": "10"
    "helm.sh/hook-delete-policy": hook-succeeded
spec:
  template:
    spec:
      serviceAccountName: helm-hook-sa  # 需要 rollout restart 权限
      containers:
      - name: restart
        image: bitnami/kubectl:latest
        command:
        - /bin/sh
        - -c
        - |
          kubectl rollout restart deployment -n {{ .Release.Namespace }}
          kubectl rollout status deployment -n {{ .Release.Namespace }} --timeout=5m
```

**Prometheus 专项热重载**（已有 `--web.enable-lifecycle`，仅需集成到 hook）：

```bash
# 追加到 Helm post-upgrade hook 脚本中
kubectl exec -n hci-observability \
  $(kubectl get pods -n hci-observability -l app=prometheus -o jsonpath='{.items[0].metadata.name}') -- \
  wget -qO- --post-data='' http://localhost:9090/-/reload
```

#### B-2 DB 迁移：Helm pre-upgrade Job（Alembic）

**业界实践**：Shopify、GitHub 均采用"迁移在应用启动前完成"的原则，常见实现是 Kubernetes Init Container 或 Helm pre-upgrade/pre-install Job。相比手动执行 SQL，优点是：
- **幂等性**：迁移工具（Alembic/Flyway）追踪已执行版本，同一脚本不会执行两次
- **原子性**：Job 失败则 helm upgrade 中止，不会出现"数据库升级了但应用未升级"的半漂移状态
- **可审计**：迁移历史记录在 `alembic_version` 表，可随时查询

```yaml
# deploy/helm/hci-platform-data/templates/hooks/db-migrate.yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: db-migrate-{{ .Release.Revision }}
  annotations:
    "helm.sh/hook": pre-upgrade,pre-install
    "helm.sh/hook-weight": "-5"
    "helm.sh/hook-delete-policy": before-hook-creation,hook-succeeded
spec:
  backoffLimit: 3
  template:
    spec:
      restartPolicy: OnFailure
      containers:
      - name: alembic
        image: {{ .Values.global.imageRegistry }}hci-case-service:{{ .Values.global.tag }}
        command: ["alembic", "upgrade", "head"]
        env:
        - name: DATABASE_URL
          valueFrom:
            secretKeyRef:
              name: hci-platform-secrets
              key: database-url
```

**当前过渡方案**（alembic 未完全集成前）：将现有 SQL 迁移脚本封装成幂等 Job：

```yaml
# deploy/helm/hci-platform-data/templates/hooks/sql-migrate.yaml
containers:
- name: sql-migrate
  image: postgres:15
  command:
  - /bin/sh
  - -c
  - |
    set -e
    for sql in /migrations/*.sql; do
      echo "执行迁移: $sql"
      psql "$DATABASE_URL" -f "$sql"
    done
  volumeMounts:
  - name: migrations
    mountPath: /migrations
  volumes:
  - name: migrations
    configMap:
      name: hci-sql-migrations
```

#### B-3 Secret vs DB 密码同步检测

**业界实践**：Netflix Chaos Engineering 中有一个原则——"假设一切都会漂移，在启动时验证"。

在 db-migrate Job 之前增加密码预检 Job：

```yaml
# deploy/helm/hci-platform-data/templates/hooks/db-password-check.yaml
# 在 db-migrate Job 之前执行（hook-weight: -10）
containers:
- name: password-check
  image: postgres:15
  command:
  - /bin/sh
  - -c
  - |
    # 用 K8s Secret 中的密码尝试连接，失败则自动同步
    if ! psql "$DATABASE_URL" -c "SELECT 1;" > /dev/null 2>&1; then
      echo "[WARNING] 密码漂移检测：K8s Secret 与 DB 密码不一致，执行 ALTER USER..."
      # 使用超级用户密码连接并修改
      PGPASSWORD="$POSTGRES_SUPERUSER_PASSWORD" \
        psql "postgresql://postgres@postgres:5432/hci_db" \
        -c "ALTER USER hci_admin PASSWORD '${NEW_PASSWORD}';"
      echo "[OK] 密码已同步"
    fi
```

#### B-4 运行时自检清单脚本

**改造文件**：新增 `scripts/ops/post-wsl-restart-check.sh`

```bash
#!/bin/bash
# WSL 重启后自动恢复关键运行时配置

set -euo pipefail

echo "=== WSL 重启后自检 ==="

# 1. 验证 CoreDNS GitHub IP 是否保留
echo "[1/4] 检查 CoreDNS NodeHosts..."
if ! kubectl get cm coredns -n kube-system -o yaml | grep -q "github.com"; then
  echo "  [WARN] CoreDNS NodeHosts 已丢失，重新应用..."
  # 应用预置 patch
  kubectl patch configmap coredns -n kube-system --patch-file scripts/ops/coredns-patch.yaml
  kubectl rollout restart deployment coredns -n kube-system
else
  echo "  [OK] CoreDNS NodeHosts 正常"
fi

# 2. 验证 ArgoCD repo-server 代理设置
echo "[2/4] 检查 ArgoCD 代理..."
if ! kubectl get deploy argocd-repo-server -n argocd -o yaml | grep -q "HTTPS_PROXY"; then
  echo "  [WARN] ArgoCD 代理未设置，请手动应用 scripts/ops/argocd-proxy-patch.sh"
else
  echo "  [OK] ArgoCD 代理正常"
fi

# 3. 验证 K3s Pod 状态
echo "[3/4] 检查核心 Pod 状态..."
kubectl get pods -n hci-dev --no-headers | grep -v "Running\|Completed" && \
  echo "  [WARN] 存在非 Running Pod，请检查" || echo "  [OK] 全部 Running"

# 4. 验证数据库连接
echo "[4/4] 验证数据库连接..."
kubectl exec -n hci-dev postgres-0 -- \
  psql -U hci_admin -d hci_db -c "SELECT 1;" > /dev/null 2>&1 && \
  echo "  [OK] 数据库连接正常" || \
  echo "  [WARN] 数据库连接失败，可能存在密码漂移"

echo "=== 自检完成 ==="
```

**改造文件**：`deploy/helm/hci-platform-data/templates/hooks/`（新建 Job）、`scripts/ops/post-wsl-restart-check.sh`（新建）、`backend/scheduler-service/app/services/k8s_client.py`

---

### C 类改进：资源生命周期失控

**目标状态**：Helm 管理边界清晰，危险操作有防护，动态资源有归属。

#### C-1 StatefulSet 保护：Helm 默认值防护 + 运行时 Guard

**业界实践**：Helm 的 `lookup` 函数可在渲染时检查集群现有资源，用于实现"已存在则保护"的逻辑。

```yaml
# deploy/helm/hci-platform/templates/_helpers.tpl — 新增 guard helper
{{- define "hci.dataLayerGuard" -}}
{{- if not .Values.dataLayer.manage -}}
  {{- $existing := lookup "apps/v1" "StatefulSet" .Release.Namespace "postgres" -}}
  {{- if $existing -}}
    {{- fail "危险操作：dataLayer.manage=false 但 postgres StatefulSet 已存在。若要卸载数据层，请先备份数据并确认后设置 dataLayer.forceDelete=true" -}}
  {{- end -}}
{{- end -}}
{{- end -}}
```

在 `values.yaml` 补充双重确认开关：

```yaml
dataLayer:
  manage: true      # 主开关，默认必须为 true（DUAL-009 教训）
  forceDelete: false # 仅在明确知道要删除数据层时设为 true，需与 manage=false 同时设置
```

#### C-2 OwnerReference — 动态 Pod 归属管理

**业界实践**：Kubernetes 垃圾回收机制（Garbage Collection）依赖 OwnerReference。为 scheduler 动态创建的 Pod 设置 OwnerReference，指向对应的 StatefulSet 或自定义 CRD，Pod 随 owner 删除自动清理。

由于 scheduler-service 通过 API 创建 Pod（非 K8s Controller），最实用的方案是用 **Job** 替代裸 Pod：

```python
# backend/scheduler-service/app/services/k8s_client.py — 改用 Job 创建池 Pod
# Job 天然带生命周期管理，完成/失败后可被清理

job_manifest = {
    "apiVersion": "batch/v1",
    "kind": "Job",
    "metadata": {
        "name": f"{assistant_type}-pool-{pod_id}",
        "namespace": self.namespace,
        "labels": {
            "managed-by": "scheduler-service",
            "assistant-type": assistant_type,
        },
        "ownerReferences": [
            {
                "apiVersion": "v1",
                "kind": "Pod",       # 将 scheduler Pod 自身设为 owner
                "name": os.environ.get("POD_NAME", "scheduler"),
                "uid": os.environ.get("POD_UID", ""),
                "controller": False,
                "blockOwnerDeletion": False,
            }
        ],
    },
    "spec": {
        "ttlSecondsAfterFinished": 300,  # 完成后 5 分钟自动清理
        # ...
    },
}
```

#### C-3 NodePort 冲突检测

**改造思路**：在部署脚本 pre-check 阶段检测端口占用：

```bash
# scripts/ops/k3s-deploy-dualrepo.sh — pre-check 函数追加
check_nodeport_conflicts() {
  local ns="$1"
  echo "[PRE-CHECK] 检测 NodePort 冲突..."
  # 获取本次 Helm release 将使用的 NodePort 列表
  TARGET_PORTS=$(helm template "${RELEASE_NAME}" "${CHART_PATH}" \
    -f "${VALUES_FILES}" --namespace "${ns}" \
    | grep 'nodePort' | grep -oE '[0-9]{5}')

  for port in $TARGET_PORTS; do
    CONFLICT=$(kubectl get svc -A --no-headers \
      | grep -v "${ns}" \
      | awk '{print $6}' | grep ":${port}/" || true)
    if [ -n "$CONFLICT" ]; then
      echo "[ERROR] NodePort ${port} 已被其他 namespace 占用：$CONFLICT"
      echo "        请先执行：helm uninstall -n <冲突namespace>"
      exit 1
    fi
  done
  echo "[OK] NodePort 无冲突"
}
```

**改造文件**：`deploy/helm/hci-platform/templates/_helpers.tpl`、`deploy/helm/hci-platform/values.yaml`、`backend/scheduler-service/app/services/k8s_client.py`、`scripts/ops/k3s-deploy-dualrepo.sh`

---

### D 类改进：网络/DNS 环境污染

**目标状态**：DNS 配置代码化，代理设置持久化，不依赖手工 patch。

#### D-1 CoreDNS 配置代码化（持久化到 Chart）

**业界实践**：基础设施即代码（IaC）要求所有运行时配置都有对应的代码表示，`kubectl edit` 等手工操作是反模式。

```yaml
# deploy/helm/hci-platform-infra/templates/coredns-patch.yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: coredns-custom      # 追加到 coredns ConfigMap，不覆盖原有配置
  namespace: kube-system
data:
  github.override: |
    # GitHub IP 直接解析，绕过 Clash fake-IP（NET-001 修复）
    hosts github-hosts {
      140.82.112.4 github.com
      140.82.114.4 github.com
      185.199.108.133 raw.githubusercontent.com
      fallthrough
    }
```

K3s 支持通过 `/var/lib/rancher/k3s/server/manifests/` 目录的自动同步机制，可将此 ConfigMap 放入 infra Chart 中随 ArgoCD 管理。

#### D-2 ArgoCD 代理配置持久化（Helm values）

```yaml
# hci-platform-env/environments/dev/values.yaml（环境仓库）
argocd:
  repoServer:
    env:
      httpsProxy: "http://172.26.96.1:7897"
      noProxy: "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,localhost,127.0.0.1"
    dnsConfig:
      options:
        - name: ndots
          value: "1"
```

#### D-3 命名空间模板化全量检查

确保所有涉及命名空间名称的 Helm 模板都使用 `{{ include "hci.namespace" . }}`，加入 CI lint 检查：

```bash
# scripts/ci/check-hardcoded-namespace.sh
# 检查 templates/ 下是否存在硬编码命名空间字符串
HARDCODED=$(grep -r "hci-troubleshoot\|hci-dev\|hci-staging" \
  deploy/helm/hci-platform/templates/ \
  --include="*.yaml" \
  | grep -v "_helpers.tpl" \
  | grep -v "# ")  # 排除注释行

if [ -n "$HARDCODED" ]; then
  echo "[ERROR] 发现硬编码命名空间，请改用 {{ include \"hci.namespace\" . }}："
  echo "$HARDCODED"
  exit 1
fi
```

**改造文件**：`deploy/helm/hci-platform-infra/templates/coredns-patch.yaml`（新建）、`hci-platform-env/environments/dev/values.yaml`、`scripts/ops/post-wsl-restart-check.sh`、`scripts/ci/check-hardcoded-namespace.sh`（新建）

---

### E 类改进：Helm/应用配置不规范

**目标状态**：非法配置在 `helm lint` / `helm template` 阶段失败，不留到运行时排查。

#### E-1 values.schema.json — JSON Schema 校验

**业界实践**：Helm 3.0+ 支持 `values.schema.json`，在 `helm install/upgrade/lint` 时自动校验 values 类型和约束，是 Helm 官方推荐的配置合规门禁。

```json
// deploy/helm/hci-platform/values.schema.json
{
  "$schema": "https://json-schema.org/draft-07/schema#",
  "title": "HCI Platform Helm Values",
  "type": "object",
  "required": ["global"],
  "properties": {
    "global": {
      "type": "object",
      "required": ["domain"],
      "properties": {
        "domain": {
          "type": "string",
          "minLength": 1,
          "description": "平台根域名，不允许为空字符串（DEPLOY-001 修复）"
        },
        "publicUrl": {
          "type": "string",
          "pattern": "^https?://[a-zA-Z]",
          "description": "必须是 DNS 名，不允许裸 IP（DEPLOY-002 修复）"
        },
        "imagePullPolicy": {
          "type": "string",
          "enum": ["Always", "IfNotPresent", "Never"]
        }
      }
    },
    "dataLayer": {
      "type": "object",
      "properties": {
        "manage": {
          "type": "boolean",
          "description": "StatefulSet 管理开关，生产环境必须为 true"
        },
        "forceDelete": {
          "type": "boolean",
          "default": false,
          "description": "与 manage=false 配合使用，明确确认删除数据层"
        }
      }
    },
    "config": {
      "type": "object",
      "properties": {
        "assistantRegistryJson": {
          "type": "string",
          "description": "JSON 字符串，禁止通过 --set 传递（DUAL-002 修复）"
        }
      }
    }
  }
}
```

#### E-2 Helm Unit Test（helm-unittest）

**业界实践**：[helm-unittest](https://github.com/helm-unittest/helm-unittest) 是 Helm 插件，支持对 template 渲染结果写断言，能在 CI 阶段捕获 DEPLOY-002 这类配置渲染错误。

```yaml
# deploy/helm/hci-platform/tests/ingress_test.yaml
suite: Ingress 配置测试
tests:
- it: publicUrl 应渲染为合法 DNS 名（而非裸 IP）
  set:
    global.publicUrl: "http://172.26.101.255.nip.io"
  asserts:
  - isKind:
      of: Ingress
  - matchRegex:
      path: spec.rules[0].host
      pattern: "^[a-z0-9.-]+$"   # DNS 名，不含纯数字段

- it: dataLayer.manage=false 应触发 fail guard
  set:
    dataLayer.manage: false
    dataLayer.forceDelete: false
  asserts:
  - failedTemplate:
      errorMessage: "危险操作"
```

在 CI 中加入：

```yaml
# .github/workflows/ci.yml — helm-unittest job 追加
- name: Helm Unit Test
  run: |
    helm plugin install https://github.com/helm-unittest/helm-unittest --version v0.4.4 || true
    helm unittest deploy/helm/hci-platform
    helm unittest deploy/helm/hci-platform-data
    helm unittest deploy/helm/hci-platform-obs
```

**改造文件**：`deploy/helm/hci-platform/values.schema.json`（新建）、`deploy/helm/hci-platform/tests/`（新建测试文件）、`.github/workflows/ci.yml`

---

## 第二部分：应用层改进（F/G/H/I/J）

---

### F 类改进：代码层数据一致性

**目标状态**：每个字段有且只有一个权威维护者，代码层明确标注，无隐性双写。

#### F-1 Unit of Work 规范化

**业界实践**：Martin Fowler《企业应用架构模式》中的 [Unit of Work](https://martinfowler.com/eaaCatalog/unitOfWork.html) 模式要求：业务操作中所有 DB 变更由单一事务上下文统一提交，不允许中间手动 commit。SQLAlchemy AsyncSession 已内置此模式，只需统一使用约定。

制定并强制执行以下编码规范（写入 CLAUDE.md）：

```python
# 规范：Repository 层只允许 flush()，禁止 commit()
# backend/shared/database/session.py 中 get_session() 是唯一合法的 commit 出口

# ✅ 正确
async def add_message(self, message: Message) -> Message:
    self.session.add(message)
    await self.session.flush()           # 发送到 DB，触发触发器，获取生成值
    await self.session.refresh(message)  # 取触发器/序列生成的字段值
    return message

# ❌ 错误（违反 Unit of Work）
async def add_message(self, message: Message) -> Message:
    self.session.add(message)
    await self.session.commit()  # 禁止：切断了 caller 的事务边界
    return message
```

在 CI 中加入静态分析检查：

```python
# scripts/ci/check_session_commit.py — 检查 repo 层是否有 session.commit()
import ast, sys, pathlib

violations = []
for path in pathlib.Path("backend").rglob("*_repo.py"):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Await):
            call = node.value
            if isinstance(call, ast.Call):
                attr = getattr(call.func, 'attr', '')
                if attr == 'commit':
                    violations.append(f"{path}:{node.lineno} — repo 层禁止调用 session.commit()")

if violations:
    print("\n".join(violations)); sys.exit(1)
```

#### F-2 DB 触发器行为契约注释规范

所有由 DB 触发器/序列/函数维护的字段，必须在 ORM Model 上加注释：

```python
# backend/conversation-service/app/models/conversation.py
class Message(Base):
    __tablename__ = "message"

    id = Column(Integer, primary_key=True)
    content = Column(Text, nullable=False)

    # [DB-TRIGGER] 由触发器 tg_update_message_count 维护
    # 禁止在代码层手动递增，任何写入将导致双重计数（D-1 根因）
    # 只读：通过 session.refresh(conversation) 获取最新值
    message_count = Column(Integer, server_default="0")
```

#### F-3 Case→KB Payload 类型安全

将 KB ingest 的 payload 结构用 Pydantic 定义在 `backend/shared/models/kb.py`，强制调用方使用：

```python
# backend/shared/models/kb.py — 新增（或已有，扩充）
from pydantic import BaseModel, Field
from typing import Literal

class KBIngestPayload(BaseModel):
    """KB Service ingest 接口入参约定（P0-3 修复：统一字段名）"""
    content_md: str = Field(..., description="Markdown 格式内容（必须用 content_md，不是 content）")
    source_type: Literal["kb", "sop", "realtime"] = Field(
        ..., description="数据来源类型，仅允许三个枚举值"
    )
    yaml_meta: dict = Field(default_factory=dict, description="结构化元数据，如 case_id")
```

```python
# backend/case-service/app/services/kb_pusher.py
from shared.models.kb import KBIngestPayload  # ← 强制使用共享类型

payload = KBIngestPayload(
    content_md=summary,
    source_type="realtime",
    yaml_meta={"case_id": str(case.id)},
)
```

**改造文件**：`backend/shared/models/kb.py`、`backend/case-service/app/services/kb_pusher.py`、`backend/conversation-service/app/repositories/conversation_repo.py`、`scripts/ci/check_session_commit.py`（新建）

---

### G 类改进：服务接口契约不对齐

**目标状态**：接口变更有传播机制，字段名由共享类型库强制约束，关键链路有契约测试覆盖。

#### G-1 共享 Pydantic Models 作为唯一数据契约

**业界实践**：微服务团队在 Monorepo 下通常共享一个 SDK/types 包（如 Netflix 的 Conductor Schema Registry），确保调用方和提供方使用的是同一份类型定义。

在 `backend/shared/models/` 下建立各服务的响应类型：

```python
# backend/shared/models/kb.py — 统一 KB 服务的请求/响应契约

class KBSearchResponse(BaseModel):
    """kb-service /retrieve 响应结构（G-1 修复：统一 chunks 字段名）"""
    chunks: list[KBChunk]           # 注意：是 chunks，不是 results
    total: int
    query_time_ms: float

class KBSOPMatchResponse(BaseModel):
    """kb-service /sop-match 响应结构（G-3 修复：扁平结构）"""
    matched: bool
    title: str | None = None
    content: str | None = None
    node_id: str | None = None
```

```python
# backend/conversation-service/app/services/kb_client.py
from shared.models.kb import KBSearchResponse, KBSOPMatchResponse

async def search(self, query: str) -> list[KBChunk]:
    resp = await self.client.get("/retrieve", params={"q": query})
    # 使用 Pydantic 解析，字段名错误立即抛出 ValidationError（可见错误）
    data = KBSearchResponse.model_validate(resp.json())
    return data.chunks  # 强类型访问，拼错字段名 IDE 立即报错
```

#### G-2 Consumer-Driven Contract Testing（消费者驱动契约测试）

**业界实践**：[Pact](https://pact.io/) 是微服务领域最广泛使用的契约测试框架，由调用方（Consumer）定义对提供方（Provider）的期望，提供方在 CI 中验证自己的实现满足所有 Consumer 的期望。

适合当前规模的轻量实现：在每个 service 的集成测试中加入"接口响应结构断言"：

```python
# backend/conversation-service/tests/integration/test_kb_client_contract.py
"""
KB Client 契约测试
目的：确保 conversation-service 对 kb-service 的响应结构假设是正确的
每次 kb-service 修改响应结构时，此测试必须同步更新
"""
import pytest
from shared.models.kb import KBSearchResponse

class TestKBServiceContract:
    """验证 kb-service 实际响应符合 conversation-service 的期望"""

    async def test_search_response_has_chunks_field(self, kb_client, mock_kb_server):
        """kb-service /retrieve 必须返回 chunks 字段（G-1 固化）"""
        mock_kb_server.get("/retrieve").respond(200, json={
            "chunks": [{"content": "test", "score": 0.9}],
            "total": 1,
            "query_time_ms": 12.5
        })
        result = await kb_client.search("test query")
        assert isinstance(result, list)  # chunks 字段存在且可解析

    async def test_sop_match_response_is_flat(self, kb_client, mock_kb_server):
        """kb-service /sop-match 返回扁平结构，不是嵌套 node 对象（G-3 固化）"""
        mock_kb_server.post("/sop-match").respond(200, json={
            "matched": True,
            "title": "存储故障排障",
            "content": "步骤一..."
        })
        result = await kb_client.sop_match("存储报错")
        assert result.matched is True
        assert result.title is not None  # 直接顶层字段，不是 result["node"]["title"]
```

#### G-3 内部服务认证传播规范

**业界实践**：Google BeyondCorp 的零信任模型要求服务间调用也必须携带身份凭证，即使在内网。

统一规范：所有服务间 HTTP 调用通过一个 `InternalHTTPClient` 基类实现，自动注入认证头：

```python
# backend/shared/utils/internal_http.py — 新建
import httpx
import os
from typing import Any

class InternalHTTPClient:
    """
    服务间内部调用 HTTP 客户端基类。
    自动注入 INTERNAL_API_TOKEN，避免逐调用点重复实现（G-2 修复）。
    """
    def __init__(self, base_url: str, timeout: float | None = 30.0):
        self._token = os.environ.get("INTERNAL_API_TOKEN", "")
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {self._token}",
                "X-Service-Name": os.environ.get("SERVICE_NAME", "unknown"),
            }
        )

    async def get(self, path: str, **kwargs) -> httpx.Response:
        return await self._client.get(path, **kwargs)

    async def post(self, path: str, **kwargs) -> httpx.Response:
        return await self._client.post(path, **kwargs)

    async def aclose(self):
        await self._client.aclose()
```

```python
# backend/conversation-service/app/services/kb_client.py — 改用基类
from shared.utils.internal_http import InternalHTTPClient
from shared.models.kb import KBSearchResponse

class KBClient(InternalHTTPClient):
    def __init__(self, base_url: str):
        super().__init__(base_url, timeout=10.0)  # KB 检索超时 10s

    async def search(self, query: str) -> list:
        resp = await self.get("/retrieve", params={"q": query, "top_k": 5})
        resp.raise_for_status()  # 401/422 立即抛出，不静默
        return KBSearchResponse.model_validate(resp.json()).chunks
```

#### G-4 API 变更传播规范（写入 CLAUDE.md）

制定服务间 API 变更的强制流程：

```
API 变更三步法：
1. 先更新 backend/shared/models/ 的共享类型定义并提交
2. 更新提供方实现（新字段兼容旧字段，不破坏已有调用方）
3. 更新所有调用方代码，移除对旧字段的依赖
4. 提交 PR，CI 契约测试校验所有调用方通过

禁止：直接修改返回字段名（破坏性变更），必须先增后删，过渡期至少一个 Release。
```

**改造文件**：`backend/shared/models/kb.py`、`backend/shared/utils/internal_http.py`（新建）、`backend/conversation-service/app/services/kb_client.py`、`backend/conversation-service/tests/integration/test_kb_client_contract.py`（新建）

---

### H 类改进：运行时静默失效

**目标状态**：失败快速可见，错误有结构，告警及时到达，不依赖查日志发现问题。

#### H-1 快速失败（Fail Fast）原则

**业界实践**：《Release It!》（Michael Nygard）中明确提出，系统应在异常路径上快速失败并输出清晰错误，而不是带着错误状态继续运行。

在 `backend/shared/utils/exceptions.py` 建立统一错误类型体系：

```python
# backend/shared/utils/exceptions.py — 扩充
from fastapi import HTTPException

class HCIException(Exception):
    """所有业务异常的基类，携带错误码和上下文"""
    def __init__(self, code: str, message: str, context: dict | None = None):
        self.code = code
        self.message = message
        self.context = context or {}
        super().__init__(message)

class ExternalServiceError(HCIException):
    """外部服务调用失败（KB Service、Scheduler 等）"""
    pass

class ResourceNotFoundError(HCIException):
    """资源不存在"""
    pass

class ConfigurationError(HCIException):
    """配置错误，通常在启动时抛出"""
    pass
```

统一异常处理器（注册到所有服务的 FastAPI app）：

```python
# backend/shared/utils/exception_handlers.py — 新建
from fastapi import Request
from fastapi.responses import JSONResponse
from .exceptions import HCIException
import logging

logger = logging.getLogger(__name__)

async def hci_exception_handler(request: Request, exc: HCIException) -> JSONResponse:
    logger.error(
        "业务异常",
        extra={
            "error_code": exc.code,
            "error_context": exc.context,
            "path": request.url.path,
        }
    )
    status_code = 500
    if "not_found" in exc.code:
        status_code = 404
    elif "invalid" in exc.code:
        status_code = 400
    return JSONResponse(
        status_code=status_code,
        content={"code": exc.code, "message": exc.message}
    )

async def http_exception_passthrough(request: Request, exc) -> JSONResponse:
    """确保 HTTPException 不被通用 except 吞掉（H-1 修复）"""
    raise exc  # 直接重抛，不包装成 500
```

修复通用 `except Exception` 吞异常的模式：

```python
# backend/api-gateway/app/routes/assistants.py — 修复前
try:
    result = await some_service()
except Exception as e:
    raise HTTPException(500, str(e))

# 修复后（正确模式）
try:
    result = await some_service()
except HTTPException:
    raise                           # HTTPException 直接透传
except HCIException as e:
    raise                           # 业务异常由统一 handler 处理
except Exception as e:
    logger.exception("未预期异常", exc_info=e)
    raise HTTPException(500, "服务内部错误")
```

#### H-2 SSE 错误事件标准化

```python
# backend/conversation-service/app/services/conversation_service.py
async def send_message_stream_only(self, ...):
    try:
        async for chunk in ai_client.stream():
            yield f"data: {json.dumps({'type': 'content', 'text': chunk})}\n\n"
    except asyncio.CancelledError:
        # 客户端主动断开，正常情况，不报错
        return
    except ExternalServiceError as e:
        # 明确的业务错误，回传给前端（H-4 修复）
        yield f"data: {json.dumps({'type': 'error', 'code': e.code, 'message': e.message})}\n\n"
    except Exception as e:
        logger.exception("AI 流式请求异常")
        yield f"data: {json.dumps({'type': 'error', 'code': 'stream_error', 'message': '服务异常，请重试'})}\n\n"
    finally:
        yield "data: [DONE]\n\n"  # 无论正常还是异常，总是发送结束标志
```

前端 `chat.ts` 对应处理：

```typescript
// frontend/customer/src/stores/chat.ts
eventSource.onmessage = (event) => {
  const data = JSON.parse(event.data)
  if (data === '[DONE]') { /* 正常结束 */ }
  else if (data.type === 'error') {
    showError(data.message)   // 替代"AI 思考中...然后什么都没有"
  } else {
    appendContent(data.text)
  }
}
```

#### H-3 Pod 资源泄漏：幂等释放 + 启动时扫描

```python
# backend/api-gateway/app/routes/cases.py — 工单关闭逻辑
async def close_case(case_id: str, ...):
    # 先释放 Pod（幂等操作，Pod 已不存在时不报错）
    try:
        await scheduler_client.release_pod(case_id)
    except Exception as e:
        # Pod 释放失败不阻断工单关闭，但必须记录告警（不能静默！）
        logger.warning("Pod 释放失败，case_id=%s，原因：%s", case_id, e)
        # 推送到 Prometheus 告警指标，非静默
        pod_release_failures_total.labels(reason=type(e).__name__).inc()

    # 再关闭工单
    await case_service.close_case(case_id, close_reason)
```

scheduler-service 重启时扫描 K8s 中的实际 Pod 与 Redis 中记录的分配关系，清理僵尸分配（B-1 已实现 pod_pool.initialize()，需确保 Redis 状态同步）：

```python
# backend/scheduler-service/app/services/scheduler_service.py — startup 增加
async def reconcile_allocations(self):
    """
    重启后对账：将 Redis 分配记录与 K8s 实际 Pod 状态对齐（H-5 加固）
    孤立分配（Redis 有记录但 Pod 不存在）→ 清理
    孤立 Pod（Pod 存在但 Redis 无记录）→ 加入 idle 队列
    """
    all_allocations = await self._get_all_allocations()
    for case_id, allocation in all_allocations.items():
        pod_exists = await self.k8s_client.pod_exists(allocation["pod_name"])
        if not pod_exists:
            logger.warning("发现孤立分配，清理: case_id=%s pod=%s",
                           case_id, allocation["pod_name"])
            await self._del_allocation(case_id)
```

#### H-4 Prometheus Alerting Rules（不只是 Metrics，要有 Alert）

**业界实践**：Metrics 只是原材料，AlertRule 才是交付物。Google SRE 的黄金信号（延迟、流量、错误率、饱和度）都应该有对应的告警规则。

```yaml
# deploy/helm/hci-platform-obs/templates/prometheus-rules.yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule   # 或直接写入 prometheus.yml 的 rule_files
metadata:
  name: hci-platform-alerts
spec:
  groups:
  - name: hci.critical
    rules:
    # KB 服务不可用（G-2 场景）
    - alert: KBServiceDown
      expr: up{job="kb-service"} == 0
      for: 1m
      labels:
        severity: critical
      annotations:
        summary: "KB Service 不可用，RAG 功能已降级"

    # Pod 资源泄漏（H-3 场景）
    - alert: PodPoolExhausted
      expr: hci_pod_pool_idle{} == 0 AND hci_pod_pool_active{} > 0
      for: 5m
      labels:
        severity: warning
      annotations:
        summary: "Pod 池已耗尽，可能存在资源泄漏"

    # SSE 流式错误率（H-4 场景）
    - alert: AIStreamErrorRateHigh
      expr: rate(hci_ai_requests_total{status="error"}[5m]) > 0.1
      for: 2m
      labels:
        severity: warning
      annotations:
        summary: "AI 流式请求错误率超过 10%"
```

**改造文件**：`backend/shared/utils/exceptions.py`、`backend/shared/utils/exception_handlers.py`（新建）、`backend/conversation-service/app/services/conversation_service.py`、`backend/api-gateway/app/routes/cases.py`、`backend/scheduler-service/app/services/scheduler_service.py`、`deploy/helm/hci-platform-obs/templates/prometheus-rules.yaml`（新建）、`frontend/customer/src/stores/chat.ts`

---

### I 类改进：工程架构缺陷

**目标状态**：依赖关系清晰显式，状态有持久化保障，模块边界有测试保护。

#### I-1 六边形架构（Ports & Adapters）规范化

**业界实践**：Alistair Cockburn 的 [Hexagonal Architecture](https://alistair.cockburn.us/hexagonal-architecture/) 要求业务核心（Domain）不依赖任何基础设施细节，通过端口（Port）抽象隔离。在 Python 微服务中的体现：

```
backend/<service>/app/
├── domain/          # 业务实体 + 业务规则（不依赖 FastAPI/SQLAlchemy/Redis）
│   ├── models.py    # Pydantic BaseModel（纯数据结构）
│   └── services.py  # 业务逻辑（接受 Port 接口，不直接依赖 DB）
├── ports/           # 抽象接口（Protocol）
│   ├── repository.py   # 数据存储端口
│   └── messenger.py    # 外部消息端口
├── adapters/        # 具体实现（可替换）
│   ├── postgres_repo.py  # PostgreSQL 实现
│   └── redis_cache.py    # Redis 实现
└── api/             # FastAPI 路由（基础设施层）
    └── routes.py
```

对 scheduler-service 的具体改造——将 Redis 访问抽象为 Port：

```python
# backend/scheduler-service/app/ports/allocation_store.py — 新建
from typing import Protocol
from typing import Optional

class AllocationStore(Protocol):
    """分配关系存储端口（I-1 修复：解耦 Redis 依赖）"""

    async def set(self, case_id: str, pod_name: str, assistant_type: str) -> None: ...
    async def get(self, case_id: str) -> Optional[dict]: ...
    async def delete(self, case_id: str) -> None: ...
    async def list_all(self) -> dict[str, dict]: ...


# backend/scheduler-service/app/adapters/redis_allocation_store.py — 现有 Redis 逻辑迁移到此
class RedisAllocationStore:
    def __init__(self, redis_client): ...
    # 实现 AllocationStore Protocol


# backend/scheduler-service/app/adapters/memory_allocation_store.py — 用于单测
class MemoryAllocationStore:
    def __init__(self): self._store: dict = {}
    # 纯内存实现，无需 Redis，单测不依赖外部服务
```

#### I-2 全局 DI → FastAPI 依赖注入标准化

虽然 `app.state` 已替换全局变量，但进一步推荐使用 FastAPI 的 `Depends` 机制，让依赖关系在请求级别明确：

```python
# backend/conversation-service/app/dependencies.py — 新建
from fastapi import Depends, Request
from .services.kb_client import KBClient

def get_kb_client(request: Request) -> KBClient:
    """从 app.state 获取 KBClient（类型安全的依赖注入）"""
    return request.app.state.kb_client

def get_conversation_service(
    kb_client: KBClient = Depends(get_kb_client),
    # ... 其他依赖
) -> ConversationService:
    return ConversationService(kb_client=kb_client)

# 路由中使用
@router.post("/conversations/{id}/message")
async def send_message(
    id: str,
    service: ConversationService = Depends(get_conversation_service)
):
    ...
```

#### I-3 Python 包管理标准化

将各服务改为规范的 Python 包结构（src layout），彻底解决 `sys.path.insert` 遗留根因：

```
backend/conversation-service/
├── pyproject.toml         # 声明包名 = "hci_conversation_service"
├── src/
│   └── hci_conversation_service/
│       ├── __init__.py
│       ├── main.py
│       └── ...
└── tests/
    └── ...
```

```toml
# backend/conversation-service/pyproject.toml
[project]
name = "hci-conversation-service"
version = "0.1.0"
dependencies = [
    "hci-shared",  # 本地依赖，引用 backend/shared
]

[tool.uv.workspace]
members = ["backend/*"]
```

```toml
# pyproject.toml（根目录）— uv workspace 统一管理
[tool.uv.workspace]
members = [
    "backend/api-gateway",
    "backend/case-service",
    "backend/conversation-service",
    "backend/scheduler-service",
    "backend/kb-service",
    "backend/shared",
]
```

**改造文件**：`backend/scheduler-service/app/ports/allocation_store.py`（新建）、`backend/scheduler-service/app/adapters/redis_allocation_store.py`（新建）、`backend/conversation-service/app/dependencies.py`（新建）、`pyproject.toml`（根目录，调整 workspace）

---

### J 类改进：容器/运行时环境适配

**目标状态**：以 K8s 容器运行为第一设计目标，本地开发通过容器环境模拟，不依赖宿主机特性。

#### J-1 十二因素（12-Factor App）规范落地

**业界实践**：[12-Factor App](https://12factor.net/) 是 Heroku 定义的云原生应用标准，确保应用在任何容器/集群环境中行为一致。当前需要补强的因素：

| 因素 | 当前状态 | 改进 |
|------|---------|------|
| III 配置（Config） | 部分硬编码 | 所有环境差异通过环境变量注入，无 `if env == 'dev':` |
| VI 进程（Processes） | Scheduler 状态在内存 | ✅ 已迁 Redis（I-1 已完成） |
| VIII 并发（Concurrency） | 单 worker 占用 | 通过 K8s HPA 水平扩展，不依赖 async 拼命压榨单实例 |
| IX 可丢弃性（Disposability） | 重启状态丢失 | ✅ 已迁 Redis |
| XI 日志（Logs）| stdout JSON | ✅ 已实现 |

特别针对`crypto.randomUUID` 白屏问题（J 类），建立"容器优先"原则：

```typescript
// frontend/shared/src/utils/crypto.ts — 统一工具函数
/**
 * 生成 UUID。
 * 优先使用 Web Crypto API（HTTPS 环境），
 * 降级到时间戳+随机数（HTTP/开发环境）。
 * 不允许在各组件中重复实现降级逻辑（DRY）。
 */
export function generateUUID(): string {
  if (typeof crypto !== 'undefined' && crypto.randomUUID) {
    return crypto.randomUUID()
  }
  // 降级方案：时间戳 + 随机数（非安全上下文，如 HTTP Ingress 访问）
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`
}
```

#### J-2 K8s Probe 标准化规范

**业界实践**：K8s 提供三种探针（Liveness、Readiness、Startup），正确使用能避免探针触发意外重启或流量误路由：

| 探针类型 | 用途 | 失败后果 |
|---------|------|---------|
| `startupProbe` | 服务初始化（DB 连接/热备池扫描）完成前屏蔽其他探针 | Pod 重启 |
| `livenessProbe` | 检测死锁/僵尸状态 | Pod 重启 |
| `readinessProbe` | 检测是否可以接收流量（依赖服务可达） | 从 Service 摘除 |

```yaml
# deploy/helm/hci-platform/templates/conversation-service/deployment.yaml
# 标准探针配置模板（J 类改进：三探针分级）
startupProbe:
  httpGet:
    path: /health/startup     # 只检查进程级别存活
    port: 8002
  failureThreshold: 30        # 允许最多 30*10s=5min 启动
  periodSeconds: 10

livenessProbe:
  httpGet:
    path: /health/live        # 检查死锁（不检查外部依赖）
    port: 8002
  initialDelaySeconds: 5
  periodSeconds: 30
  failureThreshold: 3

readinessProbe:
  httpGet:
    path: /health/ready       # 检查 DB + KB + Scheduler 可达
    port: 8002
  periodSeconds: 10
  failureThreshold: 3
```

对应的健康检查端点分级实现：

```python
# backend/conversation-service/app/routes/health.py
@router.get("/health/live")
async def liveness():
    """只检查进程是否存活，不检查外部依赖"""
    return {"status": "alive"}

@router.get("/health/ready")
async def readiness(request: Request):
    """检查所有依赖是否就绪，任一不可达则返回 503"""
    checks = {}
    # DB 检查
    try:
        await request.app.state.db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception:
        checks["database"] = "unavailable"
    # KB Service 检查
    try:
        await request.app.state.kb_client.health()
        checks["kb_service"] = "ok"
    except Exception:
        checks["kb_service"] = "unavailable"

    all_ok = all(v == "ok" for v in checks.values())
    return JSONResponse(
        status_code=200 if all_ok else 503,
        content={"status": "ready" if all_ok else "degraded", "checks": checks}
    )

@router.get("/health/startup")
async def startup():
    """只在完成初始化后返回 200"""
    if not request.app.state.initialized:
        raise HTTPException(503, "still initializing")
    return {"status": "started"}
```

#### J-3 容器安全基线（OWASP K8s Top 10）

**业界实践**：OWASP K8s Security Top 10 中排名最高的风险之一是容器以 root 运行。

```yaml
# deploy/helm/hci-platform/templates/_pod-security-context.tpl — 新建通用 helper
{{- define "hci.podSecurityContext" -}}
securityContext:
  runAsNonRoot: true          # 禁止以 root 运行
  runAsUser: 1000
  runAsGroup: 1000
  fsGroup: 1000
  seccompProfile:
    type: RuntimeDefault
{{- end -}}

{{- define "hci.containerSecurityContext" -}}
securityContext:
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true   # 根文件系统只读（防止容器内写攻击）
  capabilities:
    drop: ["ALL"]               # 删除所有 Linux capabilities
{{- end -}}
```

**改造文件**：`frontend/shared/src/utils/crypto.ts`（新建）、`deploy/helm/hci-platform/templates/conversation-service/deployment.yaml`、所有服务的 `routes/health.py`、`deploy/helm/hci-platform/templates/_pod-security-context.tpl`（新建）

---

## 第三部分：横向改进（跨类核心模式）

---

### 横向改进一：消灭静默失效

**设计范式**：[明确优于隐式（Explicit is better than implicit）](https://peps.python.org/pep-0020/) — Python 之禅第二条。

#### 建立静默失效检测清单（纳入 Code Review 门禁）

```markdown
# .github/PULL_REQUEST_TEMPLATE.md — 追加 Code Review 清单

## 静默失效检查（每个 PR 必答）

- [ ] 每个 `except Exception` 之前是否有 `except HTTPException: raise` / `except HCIException: raise`？
- [ ] 外部 HTTP 调用是否调用了 `response.raise_for_status()`？
- [ ] 后台 `asyncio.create_task()` 是否添加了 `done_callback`？
- [ ] 业务关键操作失败（如 Pod 释放、KB ingest）是否记录了 WARNING 和 Metric？
- [ ] 共享 bytes/状态（如 Pod 池、分配关系）是否在服务重启后能重建？
```

#### 关键链路存活断言（Startup Assertion）

```python
# backend/conversation-service/app/main.py — lifespan 启动时断言
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动阶段：验证关键依赖，有问题快速崩溃（Fail Fast 原则）
    try:
        # 验证 DB 连接
        async with app.state.db.connect() as conn:
            await conn.execute(text("SELECT 1"))

        # 验证 KB Client 认证（确保 INTERNAL_API_TOKEN 正确）
        await app.state.kb_client.health()

    except Exception as e:
        logger.critical("启动失败：关键依赖不可用，拒绝启动: %s", e)
        raise SystemExit(1)  # 明确拒绝启动，而非带着错误运行

    app.state.initialized = True
    yield
    # 关闭阶段清理...
```

---

### 横向改进二：消灭跨版本漂移

**设计范式**：[Conway's Law](https://en.wikipedia.org/wiki/Conway%27s_law) 反推：代码结构应该反映组织的协作边界。多服务共用的类型应该有统一的"真理来源"（Single Source of Truth）。

#### 版本协商协议（API Versioning + Compatibility Matrix）

```python
# backend/shared/models/__init__.py — 声明 schema 版本
__schema_version__ = "2.1.0"

# 每次有破坏性变更（字段重命名/删除）时：
# 1. 升级 schema_version minor 版本
# 2. 旧字段用 deprecated=True 标注，保留至少一个 Release
# 3. 在 CHANGELOG 记录变更
```

#### 环境一致性验证（staging-diff 工具）

```bash
# scripts/ops/staging-diff.sh — 新建：对比 dev/staging 环境差异
#!/bin/bash
# 对比 dev 和 staging 的以下维度：
# 1. DB Schema 差异（通过 pg_dump --schema-only 对比）
# 2. 镜像 tag 差异
# 3. ConfigMap key 差异

echo "=== DB Schema 差异 ==="
diff \
  <(kubectl exec -n hci-dev postgres-0 -- pg_dump -s -U hci_admin hci_db 2>/dev/null) \
  <(kubectl exec -n hci-staging postgres-0 -- pg_dump -s -U hci_admin hci_db 2>/dev/null) \
  || echo "[WARN] 存在 Schema 差异，请检查是否需要执行迁移"

echo "=== 镜像 Tag 差异 ==="
kubectl get deployments -n hci-dev -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.template.spec.containers[0].image}{"\n"}{end}' > /tmp/dev-images.txt
kubectl get deployments -n hci-staging -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.template.spec.containers[0].image}{"\n"}{end}' > /tmp/staging-images.txt
diff /tmp/dev-images.txt /tmp/staging-images.txt || echo "[WARN] staging 与 dev 镜像版本不一致"
```

---

## 第四部分：实施路线图

```
Sprint 1（Week 1-2）：消灭高危问题
────────────────────────────────────
优先级 P0，影响核心链路或数据安全

✅ A-1  k3s-build.sh 原子化校验
✅ B-1  Prometheus 热重载 → Helm post-upgrade hook
✅ B-3  DB 密码漂移预检 Job
✅ B-4  post-wsl-restart-check.sh 脚本
✅ F-1  Unit of Work 规范 + CI 静态检查
✅ F-2  ORM Model DB 触发器注释
✅ F-3  KB Ingest 共享类型 KBIngestPayload
✅ H-1  统一异常处理器 + 快速失败模式
✅ H-2  SSE 错误事件标准化回传
✅ H-3  Pod 资源泄漏修复 + startup reconcile

Sprint 2（Week 3-4）：架构加固
────────────────────────────────
优先级 P1，防止问题再生的根本性改造

✅ A-3  scheduler Pod 创建时打 label + 部署前清理
✅ B-2  DB 迁移 Helm pre-upgrade Job
✅ C-1  StatefulSet 保护双重开关 + Helm guard
□ C-2  OwnerReference / Job 替换裸 Pod（架构重构，延后）
✅ C-3  NodePort 冲突预检
✅ D-1  CoreDNS 配置代码化（infra Chart）
✅ D-2  ArgoCD 代理配置持久化到 env repo
✅ D-3  命名空间硬编码 CI 检查（check-hardcoded-namespace.sh）
✅ E-1  values.schema.json 校验（forceDelete + global.domain + publicUrl）
✅ I-2  FastAPI Depends 标准化 DI
✅ J-2  三级探针（startup/liveness/readiness）标准化
✅ J-3  Pod 安全上下文基线（runAsNonRoot + capabilities drop ALL）
        ⚠️  已知后效：admin-ui / customer-ui（标准 nginx 镜像）以 uid=1000 运行时
            无权写 /var/cache/nginx 和 /var/run，导致 CrashLoopBackOff。
            修复：为两个 nginx deployment 挂载 emptyDir 卷覆盖这两个目录（hotfix PR #63）。
            这是运行非 root nginx 的标准做法，无需修改镜像。
        ⚠️  并发修复冲突：PR #63 和 PR #64 同期修复同一问题，合并后产生重复的
            volumeMounts / volumes 块，且路径不一致（/run vs /var/run）。
            PR #66 清理了重复配置，统一路径为 /var/run，并调整 probe 路径为 /。

Sprint 3（Week 5-6）：质量体系完善
────────────────────────────────────
优先级 P2，防止同类问题在新功能中复发

✅ E-2  helm-unittest 覆盖关键配置渲染场景
✅ G-1  共享 Pydantic Models（KB/Scheduler 响应类型）
✅ G-2  消费者契约测试（KB Client Contract Test）
✅ G-3  InternalHTTPClient 基类（统一内部服务认证）
✅ G-4  API 变更传播规范（写入 CLAUDE.md）
✅ H-4  Prometheus AlertRule（KBDown/PodPoolExhausted/StreamError/PodReleaseFailing）
□ I-1  六边形架构（AllocationStore Port + Redis Adapter）（架构重构，延后）
□ I-3  uv workspace + src layout（包管理标准化，延后）
✅ J-1  前端 generateUUID 工具函数统一
✅ 横向 PR Template 静默失效检查清单
✅ 横向 staging-diff.sh 环境一致性工具
```

---

## 验收标准

每个 Sprint 完成后，满足以下标准方可认为该阶段改进落地：

### Sprint 1 验收
- `k3s-build.sh` import 失败时 CI pipeline 退出，不继续部署
- Helm upgrade 后 Prometheus 自动热重载（`curl hci.local/grafana/api/datasources` 数据正确）
- 关闭工单后 `kubectl get pods -n hci-dev -l managed-by=scheduler-service` 数量正确减少
- 所有服务 `grep -r "except Exception" backend/ | grep -v "except HTTPException"` 结果为零

### Sprint 2 验收
- `helm upgrade` 漏传 `dataLayer.manage=true` 时，Helm 渲染失败并输出明确提示
- `helm lint deploy/helm/hci-platform/` 对 `publicUrl: "1.2.3.4"` 返回 schema 校验错误
- WSL 重启后执行 `bash scripts/ops/post-wsl-restart-check.sh`，所有检查项自动通过
- CoreDNS NodeHosts 配置通过 ArgoCD sync 持久化，无需手工恢复

### Sprint 3 验收
- 修改 `kb-service` 响应字段名后，`conversation-service` 契约测试在 CI 中失败并给出明确错误
- `helm unittest deploy/helm/hci-platform/` 通过所有测试用例
- Grafana 存在 `KBServiceDown`、`PodPoolExhausted`、`AIStreamErrorRateHigh` 三条告警规则
- 前端任何组件中不再包含 `crypto.randomUUID` 的直接调用（统一通过 `generateUUID()` 工具函数）
