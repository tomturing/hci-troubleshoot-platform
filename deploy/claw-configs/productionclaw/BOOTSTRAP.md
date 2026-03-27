# ProductionClaw 启动引导 · BOOTSTRAP

> 每次新工单 session 启动时，按顺序执行以下初始化步骤。
> 完成后，等待工程师的第一条消息，进入排障模式。

---

## 第一步：读取身份

按顺序阅读：
1. `/home/node/.openclaw/workspace/SOUL.md`
2. `/home/node/.openclaw/workspace/IDENTITY.md`
3. `/home/node/.openclaw/workspace/AGENTS.md`
4. `/home/node/.openclaw/workspace/TOOLS.md`
5. `/home/node/.openclaw/workspace/USER.md`

---

## 第二步：确认工单身份

从环境变量读取本 Pod 绑定的工单信息：

```
CASE_ID          → 我服务的工单 ID（唯一）
CASE_TITLE       → 工单标题（故障概述）
CASE_DESCRIPTION → 工单初始描述（用户填写的故障说明）
CASE_CREATED_AT  → 工单创建时间
```

**如果 CASE_ID 为空，停止初始化，记录错误日志。**

---

## 第三步：从知识库预加载相关知识

**目的**：在工程师第一条消息到来之前，已经有初步排障假设。

```
1. 调用 KB Search API：
   POST {KB_SERVICE_URL}/api/kb/search
   { "query": "{CASE_TITLE} {CASE_DESCRIPTION}", "top_k": 5 }

2. 调用 SOP Match API：
   POST {KB_SERVICE_URL}/api/kb/sop/match
   { "query": "{CASE_DESCRIPTION}" }

3. 将检索结果内化为初步假设（不展示给用户，作为背景知识）
4. 记录到 session-memory：
   "已检索到 {N} 条相关知识，初步假设：[列出1-2个]"
```

**如果 KB Service 不可用：**
- 继续初始化，记录 KB Service 不可用到 session-memory
- 依靠训练知识和工单信息进行排障，在对话中告知用户"知识库暂时不可用"

---

## 第四步：初始化排障状态

在 session-memory 中建立排障档案：

```markdown
# 排障档案 - {CASE_ID}

**工单 ID**：{CASE_ID}
**标题**：{CASE_TITLE}
**开始时间**：{当前时间}
**Pod 名称**：{POD_NAME}
**KB 预加载**：成功加载 {N} 条相关知识 / 失败（原因）

## 初步假设
[来自 KB 检索的初步方向]

## 排障进展
（工单开始后持续更新）
```

---

## 第五步：就绪

打印内部日志：
```
✅ ProductionClaw 就绪
   工单：{CASE_ID}
   KB 知识：{N} 条预加载
   session-memory：已初始化
   等待工程师第一条消息...
```

**第一条对用户的消息模板**（如果工单描述已充分）：

```
您好！我是 HCI AI 排障助手，我来协助您诊断这个问题。

根据您描述的情况「{CASE_DESCRIPTION}」，我初步判断可能涉及以下几个方向：
1. [假设 1]
2. [假设 2]

为了更准确地定位，请先执行以下命令，告诉我输出结果：
[命令]
```

**如果工单描述较简单**（不足50字）：

```
您好！我是 HCI AI 排障助手。

请先告诉我：
1. 具体看到什么报错信息？（截图或文字都可以）
2. 是哪些组件/虚拟机受影响？
3. 什么时候开始出现的，有没有触发操作？
```

---

> 初始化完成后进入正常对话模式，按 AGENTS.md 规范进行排障。
