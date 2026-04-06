---
status: active
category: task
audience: team
created: 2026-03-28
owner: team
---

# 避坑指南优化升级计划

> 本文档是一次完整的避坑指南体系审计与整改方案，经过现状全景扫描、内容汇总去重梳理后输出。  
> 目标：从"工具专属、机器级、内容漂移"升级为"工具无关、仓库级、自动投递、单一权威来源"。

---

## 一、现状诊断：已发现的结构性问题

### 1.1 内容分布现状

| 位置 | 文件数 | 总行数 | 归属 | 问题 |
|------|-------|-------|------|------|
| `~/.claude/pitfalls/` | 9 个 .md | 1168 行 | 机器级（不进 Git） | ⚠️ 无版本历史，跨环境不同步 |
| `.claude/skills/hci-pitfalls-guide/skill.md` | 1 个 | ~200 行 | 项目级（进 Git） | ⚠️ 过期快照，PIT 只到 022，缺 PIT-023~036 |
| `docs/guides/` | 6 个 | ~1200 行 | 项目级（进 Git） | 部分含可抽取的坑点知识（如运维指南中的 K8s 操作） |
| `~/.claude/CLAUDE.md` 引用索引 | — | — | 机器级 | ✅ 结构清晰，但只有 Claude 能自动加载 |
| `AGENTS.md` 引用索引 | — | — | 项目级 | ❌ 路径引用 `~/Workflow/.../agent-global/pitfalls/`，此路径不存在 |

### 1.2 五大结构性问题

**问题 P1：幽灵路径（高危，现在就生效的错误）**

`AGENTS.md` § 7 "工作前必读" 引用路径：
```
~/Workflow/multi-agent-workflow/agent-global/pitfalls/
```
此路径在所有环境（staging、prod、CI、新开发者机器）上均 **不存在**。后果：
- Codex CLI、OpenCode、Gemini CLI 等 Agent 读取 AGENTS.md 后按此路径找不到任何文件
- CI 环境中的 Agent 完全没有避坑指南保护
- 新开发者按文档操作，原样报错，误以为是环境配置问题

**问题 P2：PIT 编号重复（内容漂移的证据）**

| 编号 | 在哪个文件 | 内容 |
|------|-----------|------|
| **PIT-023（重复！）** | `frontend.md` | SPA 部署在子路径时 vite base 和 Vue Router base 同步配置 |
| **PIT-023（重复！）** | `k8s.md` | Docker 容器端口映射外网 ERR_EMPTY_RESPONSE（Clash TUN 劫持 172.16/12） |
| **PIT-028（重复！）** | `frontend.md` | Clash TUN 下 Docker build npm install 超时 |
| **PIT-028（重复！）** | `k8s.md` | Clash TUN 宿主机 Docker build 容器无法访问网络（npm/apt 超时） |

两个 PIT-023 内容完全不同，两个 PIT-028 内容高度重叠。全局编号体系已损坏。

**问题 P3：PIT-031 空洞**

已分配编号序列中，PIT-031 不存在（030 → 032），可能是被删除或遗漏录入。

**问题 P4：~/.claude/pitfalls/ 是真实目录，不是 symlink**

说明从未真正纳入 Git 管理。每台机器独立维护，内容各自演进，无 review 流程，无变更历史。

**问题 P5：Copilot 完全看不见避坑指南**

项目内没有 `.github/copilot-instructions.md`，GitHub Copilot 完全不感知本项目的避坑体系。

---

## 二、内容汇总、去重与梳理（核心审计）

### 2.1 全局 PIT 编号注册表（现状）

| 编号 | 文件 | 标题摘要 | 问题标记 |
|------|------|---------|---------|
| PIT-001 | shell.md | here-doc 在函数内失效 | — |
| PIT-002 | shell.md | nohup 输出重定向 | — |
| PIT-003 | python.md | SQLAlchemy 懒加载 async 报错 | — |
| PIT-004 | python.md | Pydantic v2 validator 写法不兼容 | — |
| PIT-005 | frontend.md | pnpm workspace 子包未声明依赖 | — |
| PIT-006 | dispatcher.md | 状态机转换未加分布式锁 | — |
| PIT-007 | dispatcher.md | 幂等键未覆盖所有入口 | — |
| PIT-008 | dispatcher.md | Dispatcher 重启未恢复 in-flight 任务 | — |
| PIT-009 | python.md | dataclass 默认值使用可变对象 | — |
| PIT-010 | openclaw.md | OpenClaw 401 / token 不匹配 | — |
| PIT-011 | grafana.md | Grafana 登录后重定向 localhost | — |
| PIT-012 | grafana.md | 无域名部署 Ingress 渲染空 rules | — |
| PIT-013 | openclaw.md | JSON Parse Error / non-loopback 绑定失败 | ⚠️ 与 PIT-018 内容重叠 |
| PIT-014 | k8s.md | Clash TUN 劫持 K8s ClusterIP | ⚠️ Clash 主题分散 |
| PIT-015 | k8s.md | Helm release 卡 pending-upgrade | — |
| PIT-016 | k8s.md | K3s 镜像必须手动导入 | — |
| PIT-017 | k8s.md | scheduler-service RESTARTS 虚高 | — |
| PIT-018 | k8s.md | HostPath 挂载文件被截断 | ⚠️ 与 PIT-013 内容重叠 |
| PIT-019 | k8s.md | HostPath 挂载 UID 不匹配 | — |
| PIT-020 | grafana.md | admin-ui 监控 Grafana URL 指向 localhost | — |
| PIT-021 | k8s.md | K3s Traefik 宿主机端口修改 | — |
| PIT-022 | k8s.md | Helm DATABASE_URL 密码含特殊字符 | — |
| **PIT-023** | **frontend.md** | **SPA 子路径 vite base + Router base 同步** | **❌ 编号重复** |
| **PIT-023** | **k8s.md** | **Docker 端口映射 ERR_EMPTY_RESPONSE（Clash TUN 172.16）** | **❌ 编号重复** |
| PIT-024 | k8s.md | Traefik Ingress 无法跨命名空间引用 Service | ⚠️ 与 PIT-036（grafana）强关联 |
| PIT-025 | frontend.md | nginx 未设 HTML no-cache | — |
| PIT-026 | openclaw.md | Control UI 报 requires device identity | — |
| PIT-027 | openclaw.md | LLM request timed out（Clash TUN 劫持 API 域名） | ⚠️ Clash 主题分散 |
| **PIT-028** | **frontend.md** | **Clash TUN 下 Docker build npm install 超时** | **❌ 编号重复** |
| **PIT-028** | **k8s.md** | **Clash TUN Docker build 容器无法访问网络** | **❌ 编号重复** |
| PIT-029 | frontend.md | 前端 Dockerfile layer 顺序错误致全量安装 | — |
| PIT-030 | openclaw.md | Control UI 空白页（未携带 token） | — |
| **PIT-031** | **（缺失）** | **未录入或已删除** | **❌ 编号空洞** |
| PIT-032 | openclaw.md | WebSocket 服务加 HTTP redirect 导致断连 | — |
| PIT-033 | k8s.md | HCI K3s 环境一键服务检查 | ⚠️ 属 Runbook，非坑点 |
| PIT-034 | k8s.md | K3s Pod 访问外网被 Clash fake-ip 劫持 | ⚠️ Clash 主题分散 |
| PIT-035 | openclaw.md | AI 响应出错，优先容器侧修复 | — |
| PIT-036 | grafana.md | /grafana 被主站回退路由吞掉 | ⚠️ 与 PIT-024 强关联 |

**下一个可用编号：PIT-037**

---

### 2.2 需要去重的内容（4 组）

#### 去重 D1：PIT-028 重复（两个文件描述同一根因）

| 位置 | 内容侧重 | 处理方案 |
|------|---------|---------|
| `frontend.md` PIT-028 | npm install 的具体报错现象、注意 macOS 差异 | **保留为权威**（原编号 PIT-028） |
| `k8s.md` PIT-028 | 同一根因，追加 apt-get 场景，有参考 frontend.md 的注释 | **重新编号为 PIT-037**，保留 apt-get/pip 补充内容，主体改为 `参见 PIT-028` |

#### 去重 D2：PIT-023 重复（两个不相关的内容强占同一编号）

| 位置 | 内容 | 处理方案 |
|------|------|---------|
| `frontend.md` PIT-023 | SPA 子路径部署 | **保留为权威**（原编号 PIT-023） |
| `k8s.md` PIT-023 | Docker 172.16 端口映射被 Clash TUN 劫持 | **重新编号为 PIT-038** |

#### 去重 D3：PIT-013 与 PIT-018 内容高度重叠

| 条目 | 内容 | 关系 |
|------|------|------|
| openclaw.md PIT-013 | openclaw.json 被截断，JSON parse error，Pod CrashLoop | 上层**症状**（OpenClaw 专属） |
| k8s.md PIT-018 | HostPath 挂载文件被截断，通用排查模式 | 下层**根因**（通用） |

**处理方案**：PIT-018 在 k8s.md 中作为通用原则保留；PIT-013 在 openclaw.md 中保留但追加 `根因见 PIT-018`，避免重复描述排查步骤。

#### 去重 D4：network-service-check.md 内容越界

`network-service-check.md` 第九节"OpenClaw 快速恢复"和第十一节"工单创建 500"属于 **具体操作指南**，不是网络排查方法，和文件主题不符：

| 内容 | 当前位置 | 应迁移到 |
|------|---------|---------|
| OpenClaw 快速恢复配置清单 | network-service-check.md §九 | openclaw.md（合并到 PIT-013 修复步骤） |
| 工单创建 500 / close_reason 缺失 | network-service-check.md §十一 | k8s.md 或 debugging.md（已在 user memory 中记录） |

---

### 2.3 需要分类优化的内容（4 组）

#### 分类 C1：Clash TUN 主题严重分散

同一根因（Clash TUN 拦截流量）的坑点现分布在 4 个文件：

| 条目 | 当前位置 | 根因主题 |
|------|---------|---------|
| PIT-014 | k8s.md | Clash TUN 劫持 K8s ClusterIP |
| PIT-023（重编 PIT-038） | k8s.md | Clash TUN 劫持 Docker 172.16 端口映射 |
| PIT-027 | openclaw.md | Clash TUN 劫持 LLM API 域名 |
| PIT-028 | frontend.md | Clash TUN 导致 Docker build 失败 |
| PIT-034 | k8s.md | Clash TUN fake-ip 导致 K3s Pod 无法出网 |
| §二 | network-service-check.md | Clash TUN 全景诊断（最完整） |

**处理方案**：各条目原地保留，但在 `network-service-check.md` §二 顶部追加"Clash TUN 影响全表（所有相关 PIT）"的交叉索引表，形成知识枢纽；各条目末尾加 `参见 network-service-check.md §二`。

#### 分类 C2：PIT-033 属 Runbook，不是坑点

`k8s.md` PIT-033 "HCI K3s 环境一键服务检查"是 **操作核查清单**，不符合 PIT 格式（没有"坑"和"修复"，只有验证步骤）。

**处理方案**：在 k8s.md 中去掉 PIT 编号，改为独立的 `## HCI 环境健康检查清单` 小节；同时将 `scripts/k3s-verify.sh` 的使用说明引用进来。

#### 分类 C3：debugging.md 原则五位置不当

`debugging.md` 原则五"前端报 internal error 的三步定位法"包含大量前端+后端调试细节，放在"通用调试原则"文件中显得臃肿。

**处理方案**：保持现有引用（debugging.md 是最先被加载的），但提炼精简为 4 句核心步骤，把具体 kubectl 命令移到 frontend.md 末尾的"调试附录"。

#### 分类 C4：PIT-036 与 PIT-020 顺序颠倒（应先记录根因，再记录现象）

grafana.md 中 PIT-020（admin-ui iframe 指向 localhost）和 PIT-036（/grafana 被主站吞掉）是同一类问题的不同阶段，但 PIT-020 在前而 PIT-036 在后，顺序与时间线不符，阅读时逻辑不连贯。

**处理方案**：在 grafana.md 中将 PIT-020 和 PIT-036 合并到同一段落，按"现象→路由根因（PIT-024/036）→UI 根因（PIT-020）→完整修复步骤"的逻辑重组，而不是按时间先后分散。

---

### 2.4 编号空洞修复

| 空洞编号 | 说明 | 处理方案 |
|---------|------|---------|
| PIT-031 | PIT-030 与 PIT-032 之间缺失，可能被删除 | 查 git log 确认是否曾有记录；若无，预留占位，注释"曾用于 XX（已废弃/合并至 YY）" |

---

## 三、目标架构

### 3.1 整改全景图

```
                    ┌─────────────────────────────────────────────┐
                    │  docs/pitfalls/  ← 唯一权威来源（Git 管理）  │
                    │  _index.md（路由索引 + PIT 编号注册表）       │
                    │  + 9 个按技术域分类的条目文件                 │
                    └─────────────────┬───────────────────────────┘
                                      │ 唯一来源，向下投递
              ┌───────────────────────┼──────────────────────────────┐
              │                       │                              │
              ▼                       ▼                              ▼
   ┌──────────────────┐  ┌────────────────────────┐  ┌──────────────────────────┐
   │ ~/.claude/pitfalls│  │  AGENTS.md / CLAUDE.md │  │ .github/copilot-         │
   │ (symlink)         │  │  § 7 工作前必读         │  │  instructions.md         │
   │ 一次性执行        │  │  (Codex/OpenCode/       │  │  (GitHub Copilot         │
   │ setup-dev-env.sh  │  │   Gemini/Claude 项目级) │  │   自动加载)              │
   └────────┬─────────┘  └────────────────────────┘  └──────────────────────────┘
            │
            ▼
   ┌──────────────────┐
   │ ~/.claude/CLAUDE.md│
   │ (Claude 全局级)   │
   │ 有效范围：        │
   │ 所有工作区        │
   └──────────────────┘

                    ┌─────────────────────────────────────────────┐
                    │ .claude/skills/hci-pitfalls-guide/skill.md  │
                    │ （纯路由触发器，32行，不含条目内容）           │
                    │  → 指向 docs/pitfalls/_index.md             │
                    └─────────────────────────────────────────────┘

   CI 门禁（防止内容再次漂移）：
   .github/workflows/ci.yml
   └── docs-governance job：改 pitfalls 条目文件必须同步 _index.md，否则拦截
```

**各工具加载路径汇总：**

| 工具 | 加载路径 | 触发方式 |
|------|---------|---------|
| Claude Code（全局） | `~/.claude/CLAUDE.md` → `~/.claude/pitfalls`（symlink） | 启动时自动 |
| Claude Code（项目级） | `AGENTS.md` § 7 → `docs/pitfalls/` | 启动时自动 |
| GitHub Copilot | `.github/copilot-instructions.md` → `docs/pitfalls/` | 每次请求自动 |
| Codex CLI | `AGENTS.md` § 7 → `docs/pitfalls/` | 启动时自动 |
| OpenCode | `AGENTS.md` § 7 → `docs/pitfalls/` | 启动时自动 |
| CI/staging/prod Agent | `AGENTS.md`（随代码库存在）→ `docs/pitfalls/` | 无需额外配置 |

### 3.2 目录结构

### 3.2 目录结构

```
docs/pitfalls/                    ← 唯一权威来源（Git 管理，随代码演进）
├── _index.md                     ← 触发路由索引 + PIT 全局编号注册表
├── debugging.md                  ← 通用调试原则（6 条）
├── network-service-check.md      ← 网络/服务异常排查 + Clash TUN 全景索引
├── shell.md                      ← Shell/Makefile/CI（PIT-001~002）
├── python.md                     ← Python/ORM/异常（PIT-003~004,009）
├── frontend.md                   ← 前端/pnpm/Vue/Dockerfile（PIT-005,023,025,028,029）
├── dispatcher.md                 ← 状态机/幂等（PIT-006~008）
├── k8s.md                        ← K8s/K3s/Helm/镜像（PIT-014~019,021,022,024,034,037,038）
├── openclaw.md                   ← OpenClaw/认证/WS（PIT-010,013,026,027,030,032,035）
└── grafana.md                    ← Grafana/路由/iframe（PIT-011,012,020,036）
```

---

## 四、完整整改方案

### Phase 0：修复幽灵路径（立即，5 分钟）

**改动文件：** `AGENTS.md` § 7 "工作前必读"  
**改动内容：** 将所有 `~/Workflow/multi-agent-workflow/agent-global/pitfalls/` 改为 `docs/pitfalls/`  
**为何优先：** 这是现在就生效的错误——所有非 Claude 工具在任何非本地环境中运行时都没有避坑保护。

---

### Phase 1：建立单一权威来源

**1.1 迁移内容**

```bash
mkdir -p /aihci/hci-troubleshoot-platform/docs/pitfalls
cp ~/.claude/pitfalls/*.md /aihci/hci-troubleshoot-platform/docs/pitfalls/
```

**1.2 内容清理（在新目录中执行）**

按 §二 的汇总结果，对 `docs/pitfalls/` 中的文件做以下修改：

| 操作 | 文件 | 具体内容 |
|------|------|---------|
| 重新编号 | k8s.md | PIT-028 → PIT-037（追加"参见 frontend.md PIT-028"） |
| 重新编号 | k8s.md | PIT-023 → PIT-038（内容不变，追加说明） |
| 追加交叉引用 | openclaw.md PIT-013 | 末尾加"根因通用模式见 k8s.md PIT-018" |
| 追加交叉引用 | k8s.md PIT-018 | 末尾加"OpenClaw 专属症状见 openclaw.md PIT-013" |
| 迁移内容 | network-service-check.md §九 | 将 OpenClaw 快速恢复内容移到 openclaw.md |
| 迁移内容 | network-service-check.md §十一 | 将工单创建 500 内容移到 debugging.md |
| Runbook 化 | k8s.md PIT-033 | 去掉 PIT 编号，改为"## HCI 环境健康检查清单"节 |
| 占位 | k8s.md 或 debugging.md | 补 PIT-031 占位注释 |
| 追加 Clash 交叉索引 | network-service-check.md §二顶部 | 新增"Clash TUN 影响全表"（列出 PIT-014/023/027/028/034/038） |

**1.3 新建 `docs/pitfalls/_index.md`**

作为双重用途：AI 工具的"第一跳触发路由" + 团队的"PIT 全局编号注册表"。

```markdown
# 避坑指南路由索引

> 唯一来源：`docs/pitfalls/`（Git 管理）
> **写坑规则：新坑必须先在此文件分配编号，再写入对应分类文件**
> 下一个可用编号：**PIT-039**

## 触发规则（AI Agent 必读）

遇到以下场景，**必须在操作前读取对应文件**：

| 触发场景 | 读取文件 | 当前条目 |
|---------|---------|---------|
| 任何涉及进程/状态/外部服务的问题排查 | debugging.md | 原则一~六 |
| 网络/502/503/超时/SSL/Clash TUN/LLM | network-service-check.md | §一~十一 |
| 编写/审查 Shell/Makefile/CI 脚本 | shell.md | PIT-001,002 |
| 编写/审查 Python（ORM/异常/数据类） | python.md | PIT-003,004,009 |
| 编写/审查前端（pnpm/Vue/Dockerfile） | frontend.md | PIT-005,023,025,028,029 |
| 调试 Dispatcher/状态机/幂等资源 | dispatcher.md | PIT-006,007,008 |
| K8s/K3s 镜像/Helm/网络/HostPath | k8s.md | PIT-014~019,021,022,024,034,037,038 |
| OpenClaw 401/崩溃/WS/AI超时 | openclaw.md | PIT-010,013,026,027,030,032,035 |
| Grafana 重定向/Ingress/iframe | grafana.md | PIT-011,012,020,036 |

## PIT 全局编号注册表

（用于防止重复分配）

001-009 已用：shell(1,2) python(3,4,9) frontend(5) dispatcher(6,7,8)
010-019 已用：openclaw(10,13) grafana(11,12) k8s(14-19)
020-029 已用：grafana(20) k8s(21,22,24) frontend(23,25,28,29) openclaw(26,27) k8s(28→已重编为037)
030-038 已用：openclaw(30,32,35) k8s(33,34,037,038) grafana(36)
031 预留（曾分配，已废弃/合并，详见 git log）
039 下一个可用
```

---

### Phase 2：多工具自动加载机制

#### 2a. 更新 AGENTS.md（Codex/OpenCode/Gemini/Claude 项目级）

§ 7 "工作前必读"：
- 将所有路径改为 `docs/pitfalls/`
- 将 `_index.md` 设为"首先读取"的文件

#### 2b. 更新 `~/.claude/CLAUDE.md`（Claude 全局级）

- 引用路径更新为 `{project}/docs/pitfalls/`
- 添加注释说明 `~/.claude/pitfalls/` 是 symlink，指向项目 `docs/pitfalls/`

#### 2c. 新建 `.github/copilot-instructions.md`（Copilot）

```markdown
# GitHub Copilot 项目指令 — HCI 智能排障平台

## 避坑指南（生成代码或辅助调试前必读）

遇到以下场景时，参考 `docs/pitfalls/` 对应文件，避免已知问题：

| 场景关键词 | 必读文件 |
|-----------|---------|
| 调试/进程/状态/日志定位 | docs/pitfalls/debugging.md |
| 网络异常/502/超时/Clash | docs/pitfalls/network-service-check.md |
| Shell/Makefile/bash 脚本 | docs/pitfalls/shell.md |
| Python/FastAPI/SQLAlchemy | docs/pitfalls/python.md |
| Vue/TypeScript/pnpm/Docker build | docs/pitfalls/frontend.md |
| K8s/K3s/Helm/镜像导入 | docs/pitfalls/k8s.md |
| OpenClaw/认证/WebSocket | docs/pitfalls/openclaw.md |
| Grafana/Ingress/iframe | docs/pitfalls/grafana.md |

## 编码约定

- 代码注释和 Git commit 消息使用**中文**
- Python 包管理：`uv`；前端包管理：`pnpm`；禁止使用 pip/npm 直接安装
- 所有请求日志必须携带 `trace_id`（W3C traceparent）
- 禁止硬编码 API Key / Token
- Python lint/format：`ruff`，行长 120，target py312
- 前端：ESLint + Prettier + TypeScript strict mode
```

#### 2d. 精简 `.claude/skills/hci-pitfalls-guide/skill.md`

删除所有复制的 PIT 条目内容，仅保留触发规则和对 `docs/pitfalls/_index.md` 的引用，文件从 ~200 行压缩到 ~30 行。防止再次变成过期快照。

---

### Phase 3：本机环境同步

**新建 `scripts/dev/setup-dev-env.sh`：**

```bash
#!/usr/bin/env bash
# 建立 pitfalls symlink，让 ~/.claude/pitfalls/ 指向项目 docs/pitfalls/
# 新开发者在克隆仓库后执行一次：bash scripts/dev/setup-dev-env.sh
set -euo pipefail

REPO_ROOT="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
PITFALLS_SRC="$REPO_ROOT/docs/pitfalls"
TARGET="$HOME/.claude/pitfalls"

if [ -L "$TARGET" ]; then
  CURRENT_LINK="$(readlink "$TARGET")"
  if [ "$CURRENT_LINK" = "$PITFALLS_SRC" ]; then
    echo "symlink 已正确指向 $PITFALLS_SRC，跳过"
    exit 0
  fi
  echo "更新 symlink：$CURRENT_LINK → $PITFALLS_SRC"
  rm "$TARGET"
  ln -sf "$PITFALLS_SRC" "$TARGET"
elif [ -d "$TARGET" ]; then
  echo "检测到旧版真实目录，备份到 $TARGET.bak"
  mv "$TARGET" "$TARGET.bak"
  ln -sf "$PITFALLS_SRC" "$TARGET"
  echo "✅ symlink 已建立（旧内容备份至 ~/.claude/pitfalls.bak，请人工确认内容已全部迁移到 docs/pitfalls/）"
else
  ln -sf "$PITFALLS_SRC" "$TARGET"
  echo "✅ symlink 已建立：$TARGET → $PITFALLS_SRC"
fi
```

**同步 `docs/guides/本地开发指南.md` 的"首次配置"章节，增加一步：**

```bash
# 5. 建立避坑指南 symlink（一次性）
bash scripts/dev/setup-dev-env.sh
```

---

### Phase 4：CI 门禁扩展

**改动 `.github/workflows/ci.yml` 的 docs-governance job，追加规则：**

当有人修改 `docs/pitfalls/` 下的条目文件但未同步更新 `_index.md` 时，CI 报错：

```yaml
# 检查 pitfalls 条目文件的改动是否同步了 _index.md（编号注册表）
PITFALL_ENTRY=$(echo "$CHANGED_FILES" | grep -E "^docs/pitfalls/[^_]" || true)
if [[ -n "$PITFALL_ENTRY" ]]; then
  if ! echo "$CHANGED_FILES" | grep -qE "^docs/pitfalls/_index\.md"; then
    echo "⚠️  修改了 pitfalls 条目文件，但未同步更新 docs/pitfalls/_index.md"
    echo "   请更新 _index.md 中的条目计数或编号注册表后重新提交"
    exit 1
  fi
fi
```

---

### Phase 5：文档管理规范更新

**改动 `docs/文档管理规范.md`：**

1. 在目录结构图中补充 `pitfalls/` 分支：
   ```
   └── pitfalls/               ← 避坑指南（唯一权威来源）
       ├── _index.md           ← 触发路由索引 + PIT 编号注册表
       └── *.md                ← 按技术域分类的坑点文件
   ```

2. 新增边界规则：
   - **坑点 vs. 指南**：可复现的技术坑 → `pitfalls/`；操作流程 → `guides/`；技术选型依据 → `adr/`
   - **新坑录入流程**：先在 `_index.md` 分配编号 → 再写入对应分类文件 → 同一 commit/PR
   - **编号规则**：PIT 编号全局唯一，顺序递增，禁止删除已用编号（只能追加废弃注释）

---

## 五、执行顺序与工作量评估

| 优先级 | Phase | 预计改动文件 | 工作量 | 说明 |
|--------|-------|------------|--------|------|
| **P0 立即** | Phase 0 | AGENTS.md（2 行） | ✅ 已完成 | 路径已修复为 `docs/pitfalls/` |
| **P1 核心** | Phase 1.1 迁移 | 新建 docs/pitfalls/ + 9 文件 | ✅ 已完成 | 9 个文件全部迁入 |
| **P1 核心** | Phase 1.2 内容清理 | docs/pitfalls/ 下各文件 | ✅ 已完成 | 去重/交叉引用/重编号/内容迁移全部到位 |
| **P1 核心** | Phase 1.3 _index.md | 新建 1 文件 | ✅ 已完成 | 触发路由 + PIT 全局编号注册表 |
| **P1 核心** | Phase 2c | 新建 .github/copilot-instructions.md | ✅ 已完成 | Copilot 已接入 |
| **P2 支撑** | Phase 2a/2b | AGENTS.md + ~/.claude/CLAUDE.md | ✅ 已完成 | 路径全部更新为 docs/pitfalls/ |
| **P2 支撑** | Phase 2d | .claude/skills/.../skill.md 精简 | ✅ 已完成 | 32 行，纯路由触发器 |
| **P3 工程化** | Phase 3 | 新建 setup-dev-env.sh + 本地开发指南更新 | ✅ 已完成 | symlink 已建立并验证 |
| **P3 工程化** | Phase 4 | ci.yml 追加 pitfalls 门禁 | ✅ 已完成 | 新增 `_index.md` 同步检查 |
| **P4 收尾** | Phase 5 | docs/文档管理规范.md 更新 | ✅ 已完成 | 新增 pitfalls 目录 + 规则六 |

**完成时间：2026-03-28**

---

## 六、完整内容对应关系（迁移检查表）

> 用于执行时对照，确保迁移后无内容丢失

### `~/.claude/pitfalls/` → `docs/pitfalls/` 迁移清单

| 源文件 | 目标文件 | 状态 | 实际处理 |
|--------|---------|------|---------|
| debugging.md | docs/pitfalls/debugging.md | ✅ | 迁移完成，追加工单500内容 |
| dispatcher.md | docs/pitfalls/dispatcher.md | ✅ | 迁移完成，无改动 |
| frontend.md | docs/pitfalls/frontend.md | ✅ | 迁移完成，PIT-028 保留为权威版本 |
| grafana.md | docs/pitfalls/grafana.md | ✅ | 迁移完成，PIT-020/036 追加三层递进关联说明 |
| k8s.md | docs/pitfalls/k8s.md | ✅ | PIT-023→PIT-038，PIT-028→PIT-037，PIT-033 去编号改为 Runbook 节 |
| network-service-check.md | docs/pitfalls/network-service-check.md | ✅ | §二追加 Clash TUN 影响全表，§九§十一 追加迁移声明 |
| openclaw.md | docs/pitfalls/openclaw.md | ✅ | PIT-013 追加交叉引用，末尾追加"快速恢复"节（迁自§九） |
| python.md | docs/pitfalls/python.md | ✅ | 迁移完成，无改动 |
| shell.md | docs/pitfalls/shell.md | ✅ | 迁移完成，无改动 |

### 新增/改动文件清单

| 文件 | 操作 | 状态 |
|------|------|------|
| docs/pitfalls/_index.md | 新建 | ✅ 完成 |
| .github/copilot-instructions.md | 新建 | ✅ 完成 |
| scripts/dev/setup-dev-env.sh | 新建 | ✅ 完成 |
| AGENTS.md § 7 | 修改路径 | ✅ 完成 |
| ~/.claude/CLAUDE.md | 修改路径 | ✅ 完成 |
| .claude/skills/.../skill.md | 精简内容 | ✅ 完成（32行，纯路由触发器） |
| .github/workflows/ci.yml | 追加门禁 | ✅ 完成 |
| docs/guides/本地开发指南.md | 追加 setup-dev-env 步骤 | ✅ 完成 |
| docs/文档管理规范.md | 追加 pitfalls 目录和规则 | ✅ 完成 |

---

## 七、防回归规则（长期）

1. **写坑先分配编号**：先在 `_index.md` 登记编号，再写入对应文件，同一 commit
2. **CI 门禁兜底**：改 pitfalls 条目必须动 `_index.md`，否则 CI 拦截
3. **Symlink 验证**：`make dev-up` 或 `make install` 时自动检查 `~/.claude/pitfalls` 是否为 symlink
4. **季度内容审计**：每季度（每 3 个月）对 pitfalls/ 执行一次内容健康检查，内容包括：
   - 有无内容重叠未交叉引用
   - 有无"坑"已被代码修复但条目未标记 resolved
   - 有无新的高频问题未录入
