---
status: active
category: verify
audience: agent
last_updated: 2026-08-04
owner: team
update_trigger: 新增验证坑 / 发现代码/服务类问题 / PIT 编号变更
---

# 验证类避坑指南路由索引

> **唯一来源：** `docs/verify/pitfalls/`（Git 管理，随代码演进）  
> **写坑规则：**  
> 1. 先在下方"PIT 编号注册表"分配编号（V- 前缀为新编号格式，旧 PIT-xxx 保留）  
> 2. 再写入对应分类文件  
> 3. 同一 commit/PR 提交，不允许分开提交  
>
> **下一个可用编号：V-013**（旧格式延续：PIT-042）

---

## 触发规则（AI Agent 必读）

遇到以下场景，**必须在操作/编码前读取对应文件**，不得跳过：

| 触发场景 | 读取文件 | 当前条目 |
|---------|---------|---------|
| 任何涉及进程/状态/外部服务的问题排查 | [debugging.md](debugging.md) | 原则一~六 + 工单500 + V-004、V-006 |
| 编写/审查 Python（ORM/异常/数据类） | [python.md](python.md) | PIT-003, PIT-004, PIT-009, PIT-040, PIT-041 |
| 编写/审查前端（pnpm/Vue/Dockerfile） | [frontend.md](frontend.md) | PIT-005, PIT-023, PIT-025, PIT-028, PIT-029, V-001, V-002, V-003, V-005, V-007 |
| 调试 Dispatcher/状态机/幂等资源 | [dispatcher.md](dispatcher.md) | PIT-006, PIT-007, PIT-008 |
| OpenClaw 401/崩溃/WebSocket/AI 超时 | [openclaw.md](openclaw.md) | PIT-010, PIT-013, PIT-026, PIT-027, PIT-030, PIT-032, PIT-035 |

---

## PIT 编号注册表（验证类）

> 旧版全局编号注册表见 git 历史 `docs/pitfalls/_index.md`。  
> 此处仅登记隶属本目录的 PIT 条目，防止新增编号重复。

| 编号 | 文件 | 描述 |
|------|------|------|
| V-001 | frontend.md | pnpm v9 构建脚本禁止 |
| V-002 | frontend.md | 大命令输出不能在浏览器/HTTP 之后才截断 |
| V-003 | frontend.md | 展示序号不能作为跨层业务身份 |
| V-004 | debugging.md | 命令输出形态变化后不能复用旧行筛选条件 |
| V-005 | frontend.md | 普通 Markdown 代码块不能取得 Agent 自动执行权限 |
| V-006 | debugging.md | QFK 产出变量模式不得被 Matcher 前置校验短路 |
| V-007 | frontend.md | 单对象删除不得携带无关未保存草稿，校验错误不得丢失稳定对象与字段路径 |
| V-008 | frontend.md | 前端恢复/复制 Signal 不得写入 Schema 未声明的 provenance 字段 |
| V-009 | python.md | Signal 图片输入必须按章节白名单且与 Candidate 溯源闭环 |
| V-010 | python.md | 图片来源集合检查不得升级为正文 evidence 与 OCR 的跨来源逐字强绑定 |
| V-011 | debugging.md | 图片级 LLM 统计不得直接当作 KBD 案例级完成状态 |
| V-012 | debugging.md | QKV acquire.keyword 不得复用 QFK 数组语义 |
| PIT-004 | python.md | Pydantic 验证 |
| PIT-005 | frontend.md | pnpm workspace |
| PIT-006 | dispatcher.md | 分布式锁 |
| PIT-007 | dispatcher.md | 幂等键 |
| PIT-008 | dispatcher.md | in-flight 恢复 |
| PIT-009 | python.md | dataclass |
| PIT-010 | openclaw.md | 401 |
| PIT-013 | openclaw.md | JSON parse |
| PIT-023 | frontend.md | SPA 子路径 |
| PIT-025 | frontend.md | nginx no-cache |
| PIT-026 | openclaw.md | device identity |
| PIT-027 | openclaw.md | LLM 超时 |
| PIT-028 | frontend.md | Docker build npm |
| PIT-029 | frontend.md | Dockerfile layer |
| PIT-030 | openclaw.md | token 空白页 |
| PIT-032 | openclaw.md | WS redirect |
| PIT-035 | openclaw.md | AI 响应出错 |
| PIT-040 | python.md | SQLAlchemy 保留属性名冲突（metadata） |
| PIT-041 | python.md | SQLAlchemy 模型重复定义（Table already defined） |

---

## 内容健康状态（季度审计）

| 检查项 | 上次检查 | 状态 |
|--------|---------|------|
| 编号重复检查 | 2026-04-05 | ✅ 迁移自 docs/pitfalls/_index.md |
| 幽灵路径检查 | 2026-04-05 | ✅ 路径已更新 |
| symlink 验证 | — | 运行 `bash scripts/dev/setup-dev-env.sh` |
