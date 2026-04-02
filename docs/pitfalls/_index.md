# 避坑指南路由索引

> **唯一来源：** `docs/pitfalls/`（Git 管理，随代码演进）  
> **写坑规则：**  
> 1. 先在下方"PIT 全局编号注册表"分配编号  
> 2. 再写入对应分类文件  
> 3. 同一 commit/PR 提交，不允许分开提交  
>
> **下一个可用编号：PIT-039**

---

## 触发规则（所有 AI Agent 必读）

遇到以下场景，**必须在操作/编码前读取对应文件**，不得跳过：

| 触发场景 | 读取文件 | 当前条目 |
|---------|---------|---------|
| 任何涉及进程/状态/外部服务的问题排查 | [debugging.md](debugging.md) | 原则一~六 + 工单500 |
| 网络/502/503/超时/SSL/Clash TUN/LLM | [network-service-check.md](network-service-check.md) | §一~十一, PIT-039 |
| 编写/审查 Shell/Makefile/CI 脚本 | [shell.md](shell.md) | PIT-001, PIT-002 |
| 编写/审查 Python（ORM/异常/数据类） | [python.md](python.md) | PIT-003, PIT-004, PIT-009, PIT-040, PIT-041 |
| 编写/审查前端（pnpm/Vue/Dockerfile） | [frontend.md](frontend.md) | PIT-005, PIT-023, PIT-025, PIT-028, PIT-029 |
| 调试 Dispatcher/状态机/幂等资源 | [dispatcher.md](dispatcher.md) | PIT-006, PIT-007, PIT-008 |
| K8s/K3s 镜像/Helm/网络/HostPath | [k8s.md](k8s.md) | PIT-014~019, PIT-021, PIT-022, PIT-024, PIT-034, PIT-037, PIT-038 |
| OpenClaw 401/崩溃/WebSocket/AI 超时 | [openclaw.md](openclaw.md) | PIT-010, PIT-013, PIT-026, PIT-027, PIT-030, PIT-032, PIT-035 |
| Grafana 重定向/Ingress/iframe 白屏 | [grafana.md](grafana.md) | PIT-011, PIT-012, PIT-020, PIT-036 |

---

## PIT 全局编号注册表

> 用于防止重复分配，所有编号在此统一登记。  
> 规则：编号只增不减；废弃的编号保留占位注释，禁止复用。

| 编号范围 | 分配情况 |
|---------|---------|
| PIT-001~002 | shell.md：here-doc(001), nohup(002) |
| PIT-003~005 | python.md：SQLAlchemy(003), Pydantic(004), dataclass(009), 保留属性(040), 模型重复定义(041)；frontend.md：pnpm workspace(005) |
| PIT-006~009 | dispatcher.md：分布式锁(006), 幂等键(007), in-flight恢复(008)；python.md：dataclass(009) |
| PIT-010~013 | openclaw.md：401(010), JSON parse(013)；grafana.md：localhost重定向(011), 空rules(012) |
| PIT-014~019 | k8s.md：Clash ClusterIP(014), Helm pending(015), K3s镜像(016), RESTARTS虚高(017), HostPath截断(018), UID不匹配(019) |
| PIT-020~022 | grafana.md：IP访问iframe(020)；k8s.md：Traefik端口(021), DB密码特殊字符(022) |
| PIT-023~025 | frontend.md：SPA子路径(023)；k8s.md：Traefik跨NS(024)；frontend.md：nginx no-cache(025) |
| PIT-026~030 | openclaw.md：device identity(026), LLM超时(027), token空白页(030)；frontend.md：Docker build npm(028)；frontend.md：Dockerfile layer(029) |
| **PIT-031** | **预留（曾分配，内容已删除/合并，禁止复用；详见 git log）** |
| PIT-032~036 | openclaw.md：WS redirect(032), AI响应出错(035)；k8s.md：K3s服务检查RunBook(033-已去编号), fake-ip(034)；grafana.md：/grafana路由(036) |
| PIT-037~038 | k8s.md：Docker build apt/pip(037), Docker 172.16端口映射(038) |
| PIT-039 | network-service-check.md：CoreDNS hosts插件冲突(039) |
| PIT-040 | python.md：SQLAlchemy 保留属性名冲突(metadata) |
| PIT-041 | python.md：SQLAlchemy 模型重复定义(Table already defined) |
| **PIT-042** | **下一个可用编号** |

---

## 内容健康状态（季度审计）

| 检查项 | 上次检查 | 状态 |
|--------|---------|------|
| 编号重复检查 | 2026-03-28 | ✅ 已修复 PIT-023/028 重复 |
| 幽灵路径检查 | 2026-03-28 | ✅ 已修复 AGENTS.md 引用 |
| 内容越界清理 | 2026-03-28 | ✅ §九§十一 已迁移 |
| symlink 验证 | — | 运行 `bash scripts/dev/setup-dev-env.sh` |
| 编号空洞 PIT-031 | 2026-03-28 | ⚠️ 待查 git log 确认历史 |
