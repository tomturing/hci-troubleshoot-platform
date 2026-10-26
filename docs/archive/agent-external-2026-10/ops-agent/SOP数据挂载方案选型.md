---
status: active
category: deploy
audience: architect, developer
last_updated: 2026-05-07
owner: team
---

# ops-agent SOP 数据挂载方案选型

> **决策**：采用 HostPath 动态挂载方案
> **原因**：单节点环境最优解，支持 SOP 数据独立更新，ops-agent 镜像与 SOP 数据完全解耦

---

## 问题背景

### 1. ops-agent 镜像设计

ops-agent Dockerfile **故意不内置 SOP 数据**：

```dockerfile
COPY pyproject.toml README.md ./
COPY ops_agent ops_agent
COPY ops_web ops_web
COPY eval eval
# ❌ 缺少: COPY data data
```

**设计原因**：
| 因素 | 说明 |
|------|------|
| **数据体积大** | `af` 目录 11MB，`hci` 目录 28MB，显著增加镜像体积 |
| **动态更新需求** | SOP 知识库需要持续迭代，内置导致每次更新都要重建镜像 |
| **场景差异化** | 不同客户/环境有不同的 SOP 数据需求 |

### 2. SOP 数据目录结构

ops-agent 仓库结构：
```
data/case_sop_data/
  ├── af/              # AF 场景 SOP 数据（11MB）
  │   └── sop/
  │       └── node_sops.jsonl
  └── hci/             # HCI 场景 SOP 数据（28MB）
      └── sop/
          └── node_sops.jsonl
```

### 3. 环境约束

| 因素 | 状态 |
|------|------|
| 部署模式 | **单节点**（K3s 单机） |
| SOP 数据大小 | `hci` 目录 28MB |
| Helm 限制 | **文件最大 5MB** |
| ConfigMap 限制 | **ETCD 最大 1MB** |

---

## 第一性原理分析

### 核心需求拆解

| 需求 | 本质目标 |
|------|---------|
| 手动更新 SOP 数据 | **数据可控**：用户能直接修改数据文件 |
| ops-agent 与 SOP 解耦 | **独立迭代**：镜像升级不强制同步数据，数据更新不强制重建镜像 |
| 尽量自动化 | **低运维成本**：减少手动步骤，但不排斥必要的干预 |

---

## 方案对比矩阵

### 方案 A：ConfigMap 内嵌 Helm Chart

| 维度 | 分析 |
|------|------|
| **实现方式** | SOP 文件放入 `deploy/helm/hci-platform/files/`，通过 ConfigMap 挂载 |
| **数据更新流程** | ① 修改本地文件 → ② 重建 ConfigMap → ③ git push → ④ ArgoCD 同步 → ⑤ Pod 重启 |
| **ops-agent 升级流程** | 独立，不受 SOP 影响 ✅ |
| **自动化程度** | 5 步，需 Git 提交 |
| **大文件支持** | ❌ Helm 限制 5MB，ETCD 限制 1MB |
| **多环境路径差异** | 需维护多个 ConfigMap 版本 |
| **总评** | ❌ **不可行**（28MB 超出限制） |

---

### 方案 B：PVC + 手动填充

| 维度 | 分析 |
|------|------|
| **实现方式** | 创建 PVC，通过 `kubectl cp` 或 initContainer 填充数据 |
| **数据更新流程** | ① 修改本地文件 → ② `kubectl cp` 到 PVC → ③ Pod 重启 |
| **ops-agent 升级流程** | 独立，不受 SOP 影响 ✅ |
| **自动化程度** | 3 步，需手动 `kubectl cp` |
| **大文件支持** | ✅ PVC 无大小限制 |
| **多节点支持** | ✅ 支持 |
| **单节点适配** | ❌ **过度设计**（StorageClass、PV 增加复杂度） |
| **总评** | ❌ **过度设计**（单节点场景不需要 PVC） |

---

### 方案 C：HostPath 动态挂载

| 维度 | 分析 |
|------|------|
| **实现方式** | 直接挂载宿主机目录，通过 Helm values 配置路径 |
| **数据更新流程** | ① 修改宿主机文件 → ② 重启 Pod |
| **ops-agent 升级流程** | 独立，镜像变更不触及宿主机目录 ✅ |
| **自动化程度** | **2 步，最简洁** ✅ |
| **大文件支持** | ✅ 无限制 |
| **单节点适配** | ✅ **完美适配** |
| **多环境路径差异** | ✅ Helm values 配置，一次设置 |
| **数据解耦** | ✅ SOP 文件在宿主机独立目录，ops-agent 镜像无 SOP |
| **总评** | ✅ **最优解** |

---

## 方案对比总结表

| 方案 | 数据更新步骤 | ops-agent 独立 | 大文件支持 | 单节点适配 | 复杂度 | 总评 |
|------|------------|---------------|----------|-----------|--------|------|
| **ConfigMap** | 5 步 | ✅ | ❌ 1MB 限制 | ✅ | 中 | ❌ 不可行 |
| **PVC** | 3 步 | ✅ | ✅ | 过度设计 | 高 | ❌ 过度设计 |
| **HostPath** | **2 步** | ✅ | ✅ | ✅ 完美 | **最低** | **✅ 最优** |

---

## 最终方案：HostPath 动态挂载

### 架构图

```
宿主机目录                           K8s Pod
┌─────────────────────┐              ┌─────────────────────┐
│ /aihci/             │              │ ops-agent-service   │
│   ops-agent/        │              │                     │
│     data/           │─ HostPath ──▶│ /app/data/          │
│       case_sop_data │   volume     │   case_sop_data/    │
│         hci/sop/    │              │     hci/sop/        │
│           node_sops │              │       node_sops.    │
│           .jsonl    │              │       jsonl         │
└─────────────────────┘              └─────────────────────┘

更新 SOP 数据：
  ① 编辑宿主机文件（任意方式）
  ② kubectl rollout restart deployment/ops-agent-service

更新 ops-agent 镜像：
  ① ops-agent CI 构建 → ArgoCD 自动同步 → Pod 重启
  ② SOP 数据不受影响（仍在宿主机）
```

### 实现细节

**Helm values 配置**：

```yaml
# environments/dev/values.yaml
opsAgent:
  enabled: true
  sopDataHostPath: /mnt/d/aihci/ops-agent/data/case_sop_data/hci/sop

# environments/staging/values.yaml
opsAgent:
  sopDataHostPath: /aihci/ops-agent/data/case_sop_data/hci/sop

# environments/prod/values.yaml
opsAgent:
  sopDataHostPath: /aihci/ops-agent/data/case_sop_data/hci/sop
```

**Deployment volume 配置**：

```yaml
volumes:
  - name: ops-config
    configMap:
      name: ops-agent-config
  - name: sop-data
    hostPath:
      path: {{ .Values.opsAgent.sopDataHostPath | default "/aihci/ops-agent/data/case_sop_data/hci/sop" }}
      type: DirectoryOrCreate  # 自动创建目录
  - name: trajectories
    emptyDir: {}  # 解决权限问题

volumeMounts:
  - name: ops-config
    mountPath: /app/ops_config.yaml
    subPath: ops_config.yaml
  - name: sop-data
    mountPath: /app/data/case_sop_data/hci/sop
  - name: trajectories
    mountPath: /app/.trajectories
```

### SOP 数据更新流程

1. **获取 SOP 数据**：
   ```bash
   # 从 ops-agent 仓库下载 hci 目录 SOP 数据（28MB）
   gh api repos/P3n9W31/ops-agent/git/blobs/<blob-sha> --jq '.content' | base64 -d > node_sops.jsonl
   
   # 或使用项目已有的 SOP 数据（需确认数据完整性）
   ```

2. **放置到宿主机**：
   ```bash
   # dev 环境
   mkdir -p /mnt/d/aihci/ops-agent/data/case_sop_data/hci/sop
   cp node_sops.jsonl /mnt/d/aihci/ops-agent/data/case_sop_data/hci/sop/
   
   # staging/prod 环境
   mkdir -p /aihci/ops-agent/data/case_sop_data/hci/sop
   cp node_sops.jsonl /aihci/ops-agent/data/case_sop_data/hci/sop/
   ```

3. **重启 ops-agent Pod**：
   ```bash
   kubectl rollout restart deployment/ops-agent-service -n hci-<env>
   ```

---

## 决策依据

### 为什么选择 HostPath？

**第一性原理分析**：

1. **单节点环境** → PVC 是过度设计（引入 StorageClass、PV 概念）
2. **数据独立迭代** → 内嵌 ConfigMap 会导致每次更新都要重建 Helm chart
3. **大文件支持** → ConfigMap 不可行（ETCD 1MB 限制）
4. **运维成本最低** → HostPath 仅需 2 步（修改文件 + 重启 Pod）

---

## 变更历史

| 日期 | 版本 | 变更内容 |
|------|------|---------|
| 2026-05-07 | v1.0 | 初版：方案对比分析与 HostPath 最优解选择 |