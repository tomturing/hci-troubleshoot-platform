---
status: active
category: task
audience: developer
last_updated: 2026-03-28
owner: team
related: 34
---

# Task 34：Customer UI 聊天消息渲染改造（Markdown 友好呈现 + 结构化输出）（P1）

```
你是一名负责 hci-troubleshoot-platform 前端 customer 应用（custom-ui）体验改造的 agent。

【仓库】
git clone https://github.com/tomturing/hci-troubleshoot-platform.git
cd hci-troubleshoot-platform/frontend

【背景】
当前 Customer UI 的聊天消息呈现存在明显体验问题（见需求截图）：
1) AI 输出经常“整段打包”展示，缺少分段与结构化排版，用户难以阅读
2) AI 原输出为 Markdown，但 UI 未做友好渲染（代码块/列表/标题/引用均未正确呈现）

这些问题会导致排障流程的可读性显著下降，命令与风险提示也难以被准确识别。

【任务目标】
1. 将 AI 消息内容按 Markdown 规范进行渲染，至少支持：标题、列表、引用、粗体、链接、代码块（含复制按钮）
2. 解决“整体打包输出”导致的杂乱问题：对长输出进行视觉分段（段落间距、分隔线、可折叠区域等）
3. 为后续命令卡片（Task 35）预留结构化容器：能够在 Markdown 渲染结果中识别并挂载「命令块」组件

【涉及服务 / 文件范围】
- frontend/customer/src/**（聊天消息渲染相关组件与样式）
- frontend/shared/**（如存在共享 Markdown 渲染工具可复用）

【详细实现步骤】

Step 1：定位当前消息渲染实现
- 在 customer 应用中找到聊天消息展示组件（如 ChatWindow / MessageItem / Markdown 相关组件）
- 确认目前是否是简单的纯文本渲染（v-html / innerText / pre 标签等）

Step 2：引入 Markdown 渲染能力（不得破坏安全性）
- 选择一种稳定的 Markdown 渲染方案（优先：现有依赖；否则引入轻量渲染库）
- 要求：
  - 支持 fenced code block（```）
  - 支持语法高亮（可选，但推荐）
  - 必须做 XSS 安全处理（禁止任意 HTML 注入；或白名单 sanitize）

Step 3：改造 AI 消息排版与分段
- 对长文本增加更清晰的排版层级：
  - 段落间距、列表缩进、引用样式
  - 代码块独立卡片化展示
  - 可选：对“假设 / 风险提示 / 命令清单 / 预期输出”这类小节自动加视觉强调（仅样式，不改动内容）

Step 4：为命令块挂载预留扩展点
- 在 Markdown 渲染阶段识别“命令块”的统一格式（与 Task 35 对齐）：
  - 例如：代码块语言为 bash/sh/shell 的 fenced block 作为候选命令块
  - 或：约定三段式结构：命令、命令说明、风险提示（仅识别，不强制）
- 在渲染结果中将命令块替换为 `<CommandBlock />` 组件（组件先占位即可）

Step 5：测试与验收
- 用截图中的典型 AI 输出（含标题、列表、代码块）做本地验收
- 重点检查：
  - 代码块换行、缩进正确
  - 中文标点与空格不被异常折叠
  - 滚动与复制体验不退化

【约束】
- 不允许将 AI 输出当作 HTML 直接渲染导致 XSS 风险
- 不引入体积过大的 Markdown 渲染依赖（如确需新增依赖，解释体积与收益）
- 代码注释使用中文
- 前端包管理使用 pnpm

【验收标准】
- [ ] 同一条 AI 消息包含标题/列表/引用/代码块时，均能正确渲染
- [ ] 代码块具备复制按钮（复制结果与原始命令一致）
- [ ] 长输出阅读体验明显提升（段落层级清晰，不再“糊成一坨”）
- [ ] 无明显 XSS 风险（不得支持任意 HTML 注入执行）
```

---