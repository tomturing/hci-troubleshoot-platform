# HCI 避坑指南

> 团队共享的问题排查知识库，记录实际踩过的坑和解决方案

## 触发场景

当用户遇到以下情况时自动参考本指南：
- 调试进程/状态问题
- 编写或审查 Shell/Python/前端代码
- 部署 K8s/Helm 服务
- 排查 OpenClaw/Grafana 问题

---

## 目录

| 分类 | 条目数 | 关键词 |
|------|--------|--------|
| [通用调试方法论](#通用调试方法论) | 4原则 | 进程、状态、数据流 |
| [Shell 脚本](#shell-脚本) | PIT-001~015 | set -e、PATH、路径计算 |
| [Python](#python) | PIT-003~016 | SQLAlchemy、dataclass、FastAPI |
| [前端](#前端) | PIT-005 | pnpm workspace |
| [Dispatcher/状态机](#dispatcher状态机) | PIT-006~008 | 内存vs磁盘、幂等检查 |
| [OpenClaw](#openclaw) | PIT-010~014 | 401认证、port-forward |
| [Grafana](#grafana) | PIT-011~012 | localhost重定向 |
| [K8s/K3s 部署](#k8sk3s-部署) | PIT-017~022 | Helm超时、镜像拉取 |

---

## 通用调试方法论

> **适用范围**: 任何涉及进程、状态、外部服务的问题排查

### 原则一：操作进程前，先审计进程的"关联项清单"

每次 kill / restart / 修改一个进程之前，必须先问这 5 个问题：

| 关联项 | 要问的问题 | 常见遗漏 |
|--------|-----------|---------|
| **子进程** | 它 spawn 了哪些子进程？kill 父进程后子进程是否变孤儿？ | pkill 只杀了主进程，子进程继续修改状态 |
| **状态文件** | 它读写哪些文件？同步方向是什么（内存→磁盘 还是 磁盘→内存）？ | 进程还活着时改磁盘，被进程下一轮覆盖 |
| **源数据** | 它从哪里读取初始状态？重启后会重新读取吗？ | 改了文件但进程从内存运行，重启才生效 |
| **错误/临时数据** | 它产生了哪些副作用（DB 记录、API 调用、外部资源）？这些副作用是否可重入？ | 残留的"死" workspace / 重复创建 / 脏数据 |
| **外部依赖状态** | 它依赖的外部服务（DB/GitHub）当前状态是什么？操作后外部状态会变吗？ | 以为"重新创建"，实际外部已有残留记录 |

**正确操作顺序**：
```
1. pgrep -fa <进程名>     → 确认有哪些进程/子进程在运行
2. 理解状态同步方向       → 谁是真相来源？内存还是文件？
3. kill 所有相关进程      → 父进程 + 子进程 + 相关守护进程
4. 验证进程已完全退出     → pgrep 再次确认，等 orphan 子进程退出
5. 修改状态文件/配置      → 此时磁盘才是真相，操作才有效
6. 重启进程              → 进程读取已修改的干净状态
7. 验证生效              → 日志/API 确认新状态被正确加载
```

### 原则二：状态修改要闭环验证，不假设"改了就生效"

```
修改 → 立即验证（读回）→ 确认变化符合预期 → 再继续下一步
```

### 原则三：存在 ≠ 可用，"创建成功"需要区分多个阶段

```
记录创建（DB有记录）
  → Provisioning（正在初始化）
    → Ready（可用）
      → Running（运行中）
        → Completed / Failed
```

**幂等检查必须验证目标阶段，而非仅验证存在性**：
```python
# 错误
if resource_exists(name):
    return reuse(resource)

# 正确
resource = find_resource(name)
if resource and resource.is_ready():   # 区分存在性和有效性
    return reuse(resource)
elif resource:
    log("发现无效资源，忽略并重新创建")
    # 继续创建流程
```

### 原则四：排查问题时先绘制"数据流向图"再动手

```
谁产生数据 → 存在哪里 → 谁读取 → 谁覆盖 → 最终谁是真相
```

---

## Shell 脚本

### PIT-001: `((var++))` 在 `set -e` 下首次调用即退出

**症状**: `set -e` 的 bash 脚本在第一次执行 `((PASS++))` 时静默退出。

**修复**:
```bash
# ❌ 错误
((PASS++))

# ✅ 正确
PASS=$((PASS + 1))
```

### PIT-002: ruff/uvx 等工具在 worktree 中不在 PATH

**修复**: 统一使用 `uvx ruff` 替代裸 `ruff`。

### PIT-015: 脚本中 `PROJECT_ROOT` 路径计算在嵌套目录中出错

**修复**:
```bash
PROJECT_ROOT="$(dirname "$(dirname "$SCRIPT_DIR")")"  # ✅ 两次 dirname
```

---

## Python

### PIT-003: SQLAlchemy Column 的 `default={}` 可变默认值

**修复**:
```python
# ❌ 错误
metadata = Column(JSON, default={})

# ✅ 正确
metadata = Column(JSON, default=dict)
```

### PIT-004: `@dataclass` 继承 `Exception` 时缺少 `super().__init__()`

**修复**:
```python
@dataclass
class AIStreamError(Exception):
    error_type: str
    message: str

    def __post_init__(self):
        super().__init__(self.message)
```

### PIT-009: 用 Python 字符串匹配脚本 patch 源文件时静默遗漏改动

**规则**:
1. **优先级**: `replace_string_in_file` > 行号定位插入 > 字符串匹配脚本
2. 若必须用字符串匹配脚本，必须加硬断言：
   ```python
   assert old in content, f"FATAL: patch failed for: {old[:40]!r}"
   ```

### PIT-016: FastAPI 路由 `status_code=204` 返回类型标注导致启动崩溃

**修复**:
```python
@router.delete("/{atom_id}", status_code=204, response_model=None)  # ✅
async def delete_atom(request: Request, atom_id: str):
    ...
```

---

## 前端

### PIT-005: pnpm workspace 的 node_modules 空壳目录

**修复**: 检查 `.pnpm` 子目录而非 `node_modules` 目录本身。

---

## Dispatcher/状态机

### PIT-006: 进程内存与磁盘状态双轨并存，手动改文件无效

**覆盖方向**: 内存 → 磁盘

**正确操作**: 先 kill 进程 → 改文件 → 重启进程

### PIT-007: 幂等检查只验证"存在性"而非"有效性"

**修复**:
```python
if existing and existing.get("container_ref"):   # ← 验证有效性
    t.review_workspace_id = existing["id"]
    return
```

### PIT-008: VK workspace container_ref=null 的含义

| 字段值 | 含义 | 处置 |
|--------|------|------|
| `container_ref = null` | VK 写入记录但从未启动 agent | 废弃，重新创建 |
| `container_ref = "/var/tmp/..."` | worktree 已创建，agent 已运行 | 可复用 |

---

## OpenClaw

### PIT-010: 排查 401 Unauthorized

**快速诊断**:
```bash
# 确认两侧 token 是否一致
kubectl exec -n hci-troubleshoot deploy/openclaw -- env | grep OPENCLAW_GATEWAY_TOKEN
kubectl exec -n hci-troubleshoot deploy/conversation-service -- env | grep OPENCLAW_GATEWAY_TOKEN

# token 不同时重启 Pod
kubectl rollout restart deploy/openclaw deploy/conversation-service
```

### PIT-014: Pod 重启后需要手动重建 port-forward

```bash
pkill -f "port-forward.*18789"
kubectl -n hci-troubleshoot port-forward deploy/openclaw 18789:18789 &
```

### PIT-013: 端口职责划分

| 端口 | 用途 | 有 Web 界面 |
|------|------|-------------|
| **18789** | Gateway + Web UI | ✅ |
| **18791** | Browser Control API | ❌ |

---

## Grafana

### PIT-011: 登录后重定向到 localhost

**修复**:
```bash
kubectl set env deploy/grafana -n hci-observability \
  GF_SERVER_DOMAIN=grafana.hci.local \
  GF_SERVER_ROOT_URL=http://grafana.hci.local/
```

### PIT-012: Admin UI 内嵌 Grafana 仍跳转 localhost

**修复**: 将判断条件改为通用的 `admin.` 前缀检测：
```ts
if (hostname.startsWith('admin.')) {
  const grafanaHost = hostname.replace('admin.', 'grafana.')
  grafanaUrl.value = `${protocol}//${grafanaHost}`
}
```

---

## K8s/K3s 部署

### PIT-017: Helm upgrade 超时导致 StatefulSet 被删除

**修复**:
```bash
helm rollback hci-platform <REVISION> -n hci-troubleshoot
```

### PIT-018: 服务 startup probe 超时导致无限重启

**修复**:
```bash
kubectl set env deployment/<name> OTEL_EXPORTER_OTLP_ENDPOINT="" -n hci-troubleshoot
```

### PIT-019: 多个 Deployment 副本使用不同镜像源

**修复**:
```bash
kubectl set image deployment/<name> <container>=hci-<service>:latest -n hci-troubleshoot
```

### PIT-020: K3s ctr 导入镜像需要 root 权限

```bash
docker save hci-api-gateway:latest | sudo k3s ctr images import -
```

### PIT-021: Docker Hub 国内访问超时

```bash
sudo tee /etc/docker/daemon.json <<JSON
{"registry-mirrors": ["https://docker.m.daocloud.io", "https://dockerproxy.com"]}
JSON
sudo systemctl restart docker
```

### PIT-022: OTEL/Tempo 不可用时服务启动缓慢

```bash
kubectl set env deployment/case-service OTEL_EXPORTER_OTLP_ENDPOINT="" -n hci-troubleshoot
```

---

## 新增条目模板

发现新问题时，按以下格式追加：

```markdown
## PIT-XXX: <问题标题>

**触发场景**: <什么情况下会遇到>

**症状**: <具体表现>

**根因**: <为什么会这样>

**修复**: <解决方案>

**发现日期**: YYYY-MM-DD
```

条目编号全局递增，追加到对应分类文件末尾。