# HCI 智能排障平台 — 项目规范

> **本文件是所有 AI Agent（Claude Code / Codex CLI / Gemini CLI / Antigravity IDE）的项目层规范文件。**
> `./CLAUDE.md` 是本文件的符号链接，确保 Claude Code / Antigravity IDE 读到相同内容。
> `./CLAUDE.local.md` 存放个人本地配置（不提交 git）。
> 全局编码规范见 `~/.claude/CLAUDE.md`，全局避坑指南见 `~/.claude/pitfalls/`。

---

## 启动时必读文件

**在开始任何工作前，请优先读取本仓库内的规范文件：**

1. `AGENTS.md` — 项目层规范。本文件是 Claude Code / Codex / Gemini / Copilot 等 Agent 的统一入口。
2. `docs/deploy/pitfalls/_index.md` 或 `docs/verify/pitfalls/_index.md` — 按任务场景读取对应避坑指南索引，再读取具体指南。

---

## 1. 项目概述

**HCI 智能排障平台** — AI 驱动的超融合基础设施运维故障诊断系统。

- 用户创建工单描述故障 → AI 助手多轮对话引导排障 → 建议命令和操作步骤 → 形成可复用知识库
- 当前版本：v2.16.0（以 `pyproject.toml` 为准）
- **Admin UI 侧边栏菜单图标缺失深度修复**：
  - **根因**：`App.vue` 侧边栏菜单使用字符串 `:is="item.icon"` 动态渲染组件，由于未在 `App.vue` 中显式导入 `Setting` 图标，在 Vite 代码分割模式下未访问对应 View 前该组件未被打包与全局解析。此外，Vue 3 动态组件 `:is` 接收普通 Component 定义对象存入 reactive/ref 响应式上下文时会被包装为 Proxy，可能导致渲染挂载失效。
  - **修复**：在 `App.vue` 中建立静态 Component Icon 字典 `menuIconMap`，使用 `markRaw()` 包裹所有 Element Plus 图标（含 `Setting`, `Tools`, `DataAnalysis` 等），并在 `main.ts` 中补充全小写 key 全局注册容错，消除 Vue 动态响应式开销并确保首屏 stable 挂载。
- **KBD 关键信号保存 SQL 语法错误修复**（PR #599）：
  - 修复 `backend/kb-service/app/routes/admin.py` 中 `signals_json` PATCH 更新 SQL 的拼接，将 `:signals_json::jsonb` 替换为标准 `CAST(:signals_json AS jsonb)` 语法，解决 SQLAlchemy `text()` 冒号解析歧义导致的 500 语法错误。
- **Admin UI 内网隔离方案**（2026-07-01）：
  - **背景**：admin-ui 和 customer-ui 通过同一公网入口暴露（`acli.sangfor.com.cn:4443`），admin-ui 仅依赖 IP 白名单保护，仍附着在公网入口上。
  - **方案**：采用端口隔离 + 云厂商 IP 白名单。admin-ui 使用独立端口（Traefik `web` entrypoint，端口 4888 + TLS），customer-ui 继续使用 `websecure` entrypoint（端口 4443）。
  - **实施**：创建 `admin-ui-ingress` 独立 Ingress；从主 Ingress 移除 `/admin` 路径；启用 Traefik `web` entrypoint TLS；云厂商 LB 新增 8443 端口映射 + 管理员 IP 白名单。
  - **访问变更**：管理员访问 `https://acli.sangfor.com.cn:8443/`（仅管理员 IP 可达），客户访问无变化。
  - **文档**：`docs/deploy/admin-ui-internal-isolation.md`
- **工单 Q2026061002370 诊断执行失败修复**：
  - 数据库：`database/desired_schema.sql` 补齐 `fact.trace_id` 字段及索引，解决 `FactStore` 写入时因字段缺失导致 SQL 报错
  - Helm：`agent-service` 注入 `SCP_BASE_URL` 与 `SCP_API_KEY` 环境变量；`secret.yaml` 中渲染 `SCP_API_KEY`；在 `values.yaml` 中定义二者默认值为空以保证环境兼容性
  - 环境：开启 `agentService.externalDns` 以解决大模型调用时的 DNS 解析超时问题
- **工单 Q2026061511158 自动诊断停顿与推理步数限制优化**：
  - 对话历史对齐：在 `conversation-service` 拦截用户 S0 选择时，将产生的确认话术同步追加至 `history_messages` 列表中，避免大模型重复回复文本导致 ReAct 循环终止停顿，实现 100% 自动导航推进。
  - 推理步骤上限提升：在 `agent-service` 将 ReAct 推理循环的硬性最高步骤数限制 `MAX_STEPS` 和 `InvestigationAgent` 调用时传入的 `max_iterations` 从 `15` 步调高至 `40` 步，解决复杂排障或重连重新执行命令时步骤极易超限问题。
- **ReAct 工具调用历史跨轮次持久化**（第一性原理方案）：
  - **根因**：LLM 的上下文窗口是其唯一工作内存。工具调用轮次（`tool_calls` + `tool_result` 消息对）是 ReAct 推理链的关键 Observation 节点，但原有实现中这些消息仅存在于 `react_engine` 的内存 `work_messages` 中，会话结束即丢失，用户点击"继续"时大模型从零开始推理，重复执行已完成的工具。
  - **方案**：遵循 OpenAI Function Calling 规范和 LangGraph Checkpointer 范式，将工具调用轮次作为一等公民持久化到 `message` 表：
    - DB Schema：`message_role` ENUM 增加 `tool_call` 和 `tool_result` 两个角色；`message` 表增加 `tool_call_id` 字段关联 tool result 到对应 tool call。
    - agent-service：`react_engine.py` 新增 `_persist_tool_turn()` 方法，每次工具执行完毕后 fire-and-forget 调用 `conversation-service` 的 `/tool-turn` 接口写入记录。
    - conversation-service：新增 `POST /api/conversations/{id}/tool-turn` 接口；修改 `history_messages` 构建逻辑，加载时包含 `tool_call/tool_result` 角色消息并正确还原为 OpenAI messages 格式；实现**滑动窗口压缩**策略（最近 10 步完整保留，更早的工具输出截断为 200 字符摘要，防止 token 爆炸）。
    - 存量环境：`desired_extras.sql` 幂等 `ALTER TYPE` 和 `ALTER TABLE` 自动补齐旧部署。

- 前端工具栏优化：工单信息 Popover（含 ID/工单号）→ 关闭工单 → SSH终端（Monitor 图标），终端历史按钮移入 TerminalPanel header-actions
- 环境采集命令更新：`task get -s failed -l 10`（仅失败任务）；后端字段映射已支持整数 status/urgent_type 与 Unix 时间戳（PR #285）
- **KBD 管理页面搜索增强**（PR #328）：
  - 支持按案例 ID 精准搜索、标题关键字模糊搜索
  - 显示顺序按导入时间倒序（新数据在前）
  - 前端通过 `support_id` 本地拼接 URL，替代已删除的 `support_url` 字段
- **数据库字段清理**（PR #328）：
  - `kbd_entry` 删除 `support_url`、`archived_at`（冗余字段）
  - `sop_document` 删除 `tree_scenario_name`、`tree_total_node_count`、`tree_generated_at`（已合并入 `tree_json`）
- **KBD 分类下拉框修复**（PR #329）：
  - 前端 `fetchCategories()` 解析 API 响应结构修正，增加空值兜底
  - Vision LLM prompt 抽离为独立文件，增加文件存在性检查
- **KBD 管理页面 UI 优化**（PR #330）：
  - 导入时间改用 `updated_at`（反映 override 重跑后的更新时间）
  - 分类筛选改为下拉选择框，支持搜索/清空，复用 `categoryOptions`
- **KBD 管理页面排序优化**（PR #331）：
  - KBD 列表排序改为 `updated_at DESC, id DESC`，最新更新的数据排在前面
  - Vision LLM prompt 强化表格/告警截图规则，增加正确/错误示例
  - 修复 `image_proc.py` 解析正则，支持带装饰线的新输出格式，告警截图每行合并为 `值1 | 值2 | ...`
- **SOP 管理页面决策树可视化**（PR #350）：
  - 点击「查看」按钮后，弹窗中渲染多叉决策树结构
  - 使用 `el-tree` 组件展示节点层级、前置条件、诊断方法、解决方案
  - 字段名统一：`chunk_count` → `tree_leaf_count`（节点数）
  - 决策树"有警告"状态时提供查看告警详情入口（Warning 图标）
  - 删除所有分块/chunk/embedding 相关的过期概念和文案
- **SOP 查看弹窗优化**（PR #351）：
  - 基础信息表格中"决策树"字段改为"节点数"，显示 `tree_leaf_count` 数值
  - 有警告时在节点数旁显示告警标签和查看按钮，点击可查看告警详情
  - 后端详情接口添加 `tree_validation_status` 和 `tree_validation_issues` 字段返回
- **SOP 决策树前置条件显示优化**（PR #352）：
  - 前置条件改为块状布局，每个前置条件单独一个白色卡片
  - 内容使用 `pre-wrap` 样式，保留换行和空格，正确显示多行内容（包括代码块）
- **AI 空响应兜底修复**：
  - TriageAgent 增加 LLM 空内容检测，返回友好提示而非空白气泡
  - agent-service `_event_stream` 增加空流检测，无内容时返回结构化 error 事件
  - conversation-service 增加空响应兜底，避免前端渲染空白消息
- **KBD 图片列表渲染优化**（PR #364）：
  - 前端从 `images_json`（权威数据源）渲染图片列表，而非从 `content_md` 解析
  - 添加 `parseImagesJson` 函数解析 desc.txt v3 格式（BACKGROUND/TYPE/FULL_TEXT/DESCRIPTION）
  - 图片按序号排序，accordion 卡片展示，解决部分案例多张截图只显示一张的问题
  - 后端 API 返回 `images_json` 字段，包含每张图片的完整描述信息
- **KBD 单张图片重新识图**（PR #517）：
  - 新增 `/api/v1/kbd/{kbd_id}/reanalyze-image/{seq}` 端点支持单张图片识图
  - 前端图片列表刷新按钮改为单张图片识图
  - `ElMessage` 设置 `duration: 0` 不自动关闭，用户手动关闭
  - API Gateway 新增代理路由透传到 kb-service
- **Vision LLM 并发与数据库连接修复**（PR #519）：
  - `VISION_CONCURRENCY` 从 3 降为 1，避免 DashScope 429 Rate Limit
  - 增加数据库更新重试逻辑，处理长耗时操作导致的连接超时
- **工单管理分类选择框与 S0 Triage 4+1 交互体验优化** (PR #370):
  - 前端工单编辑「工单分类」由输入框升级为 `el-select` 下拉检索框，实现与 SOP 分类编辑的一致样式与体验
  - 提取公共 `useCategories` composable，在工单管理、KBD 审查、SOP 管理页面中统一引用该公共分类函数加载逻辑
  - 修复 S0 阶段流式推理和 ACP 卡片的重复显示（Bug a）和由于落库时差导致的页面刷新乱序（Bug b）
  - 统一 S0 交互样式为 `4+1` 模式，提供 4 个高置信度选项与一个带症状补全的“以上都不是”选项，彻底解决点击“以上都不是”时的 LLM 异常报错（Bug c）
- **S0 Triage 4+1 按钮引导词正则判定范围扩容** (PR #371):
  - 扩展前端 `MessageBubble.vue` 对 AI 选项引导语的匹配范围（添加“请补充”、“请问”等高频提问前缀），彻底解决部分 4+1 选项文本未能渲染为可点击按钮的问题，实现与历史交互一致的“禁用/不可选”和补充描述（+1 模式）特性
- **KBD 溢出 DOM 兼容与前端贪婪解析修复** (PR #375):
  - 升级 `data-pipeline/kbd/converter.py` 板块解析器，使用平铺子节点动态遍历合并机制，自动提取并合入溢出在容器外的排障正文和截图，完美向后兼容。
  - 修复 `KbdReviewView.vue` 中由 `inDescription` 状态导致的 Markdown “贪婪吸入”解析 Bug，严格限定非空且非 `>` 开头的行为卡片结束标志，防止游离排障步骤被误吞进折叠面板内。
  - 修复 `KbdReviewView.vue` 内 `renderMarkdown` 转义星号渲染冲突，使用 DOMPurify + marked AST 解析替代手写正则，完美修复分割线及排障步骤中的反斜杠裂变与星号吞噬 Bug。
- **Prompt/工具/技能管理页面 Dialog 和布局样式优化** (PR #391):
  - 修复 Prompt/工具/技能 Dialog 中 teleported 弹窗 scoped 样式失效及 full screen 模式下按钮定位异常的 Bug
  - 优化 Prompt 管理页面主卡片最小高度及 Prompt 预览最大高度，杜绝列表下方的无效留白
  - 将工具管理中”功能描述”字段调整到右栏上方，均衡左右高度并拓宽编辑体验
- **技能管理列表排版优化** (PR #392):
  - 优化技能管理列表中的“Skill 标识”和“操作”按钮排版，防止折行，保持单行显示
- **技能编辑 Markdown 预览修复** (PR #393):
  - 修复技能编辑弹框中右侧“预览”由于未引入 marked 和 dompurify 模块导致的 Markdown 原文未解析 Bug
- **技能预览列表缩进与 SOP 环境变量自动注入优化**:
  - 修复技能管理页面 Markdown 预览中由于全局样式 reset 导致的列表（ul/ol/li）无前缀符号及缩进丢失问题。
  - 优化 SOP 执行实例创建接口 `sop_create_execution` 响应，使其包含并返回已解析的环境变量。
  - 优化排障 Agent `investigation_agent` 的系统提示词构建，在新执行实例的系统提示词中注入 `【已知变量】`，避免 AI 仍向用户手动询问已收集的信息。
  - 修复变量池 `sop_request_variable` 获取策略判定逻辑，支持并正确识别 `env:xxx` 格式的环境变量注入策略。
- **SOP 工具执行器参数适配修复**：
  - 修复 `SopToolExecutor.execute` 签名缺少 `**kwargs` 导致在 ReAct 循环中被调用时抛出 `TypeError: got an unexpected keyword argument 'conversation_id'`，彻底解决工具调用通道报错阻断的问题。
- **SOP 交互变量值提交失效问题修复**：
  - 修复前端 `MessageBubble.vue` 和 `InteractiveRequestCard.vue` 提交 `interactive-response` 时缺失 `kind` 和 `metadata` 导致路由错误的问题。
  - 在后端 `submit_interactive_response` 增加针对 `variable_input`/`variable_confirm` 的处理，直接将变量写入 SOP 变量库中，并恢复执行状态为 `active`。
  - 前端接收到变量提交流程成功后，自动调用 `sendMessage` 重新发送变量值，触发后端 HTP Agent 的 ReAct 推理循环从中断位置恢复继续运行。
- **SOP 执行路由漂移与恢复稳定性修复**：
  - 修复排障 Agent 在多轮对话中，由于用户发送的“继续”或命令回显数据与 SOP 文档内容在语义匹配上发生偏差，导致 `route_by_category` 三轨路由发生漂移、误判为非 SOP 轨道而回退到 fallback 推理模式的缺陷。
  - 优化：当检测到活跃的 `sop_resume_context`（正在执行的 SOP）时，直接绕过三轨路由匹配，通过 document_id 获取 SOP 详情，确保 Agent 在会话周期内牢牢锁定在 SOP 导航模式中。
- **工具调用可视化、变量交互重构与原始环境注入**：
  - **工具调用可视化与自动执行**：后端 `react_engine.py` 在工具执行生命周期中（执行前中后）广播 `tool_call` 与 `tool_result` 阶段事件并透传统一 `exec_id`。前端支持全局“自动执行”模式选择（Off / Safe-only / Aggressive），在满足风险级别时自动回复确认。对话流中新增工具卡片，以黑底 Terminal Console 折叠渲染命令执行日志与耗时。
  - **变量输入/确认交互重构**：将 `variable_input` 升级为行内表单，对 `validation_pattern` 正则及必填项进行失焦与实时校验，不合法时红框报错并禁用提交按钮。将 `variable_confirm` 升级为左右双栏对比，左栏一键快捷确认系统推荐值，右栏支持微调修改与实时校验。
  - **原始环境注入与向下兼容**：后端在开启 `USE_RAW_ENVIRONMENT_CONTEXT` 时直接将数据库的原始字典/JSON 喂给 LLM 提升推理准确率。在 `sop_execution.py` 中增加 is_raw 兼容层，自动将 Unix 时间戳、状态整型、紧急度等映射为 SOP 规则可识别的语义值。
- **SOP 工具执行器参数冲突与布尔变量归一化修复**：
  - 修复 `SopToolExecutor.execute` 在委派调用默认执行器时，由于 `**kwargs` 携带 `conversation_id` 造成的多值传递错误 `got multiple values for keyword argument 'conversation_id'`。
  - 在 `submit_interactive_response` 接口与 `sop_variable_response` 路由中，对 `boolean` 类型的变量提交值进行强制归一化（Truthful 词汇转为 `"true"`，Falsy 词汇转为 `"false"`），彻底解决大模型由于布尔值字符串（如“是”/“否”）不符合条件表达式规则而无法推进决策树节点的缺陷。
- **废弃技能数据与临时文件清理**（PR #411）：
  - 物理删除 `backend/kb-service/data/` 技能文件目录和 `sop_matcher` 废弃匹配器逻辑。
  - 修复 `health.py` 中数据库检查的探针变量拼写 Bug（修正 `db` 为 `database_manager`）。
  - 在 `kb-service` 和 `conversation-service` 契约验证中彻底下线 `/sop/match` 废弃路由。
  - 移除 Docker Compose 和 Helm 模板中对技能数据卷挂载及 `SOP_SKILLS_DIR` 环境变量的声明。
  - 清理误提交的 `.kb-service-portforward.pid` 与 Word 临时所有者文件，并在 `data-pipeline/kbd/.gitignore` 中新增 `*.pid` 过滤规则。
- **部署策略优化**：
  - 恢复 staging 自动化部署和自动同步，GitHub 提交 PR 合并至 main 后将自动同步镜像 tag 至 staging 环境，确保 schema 更新及时覆盖，同时保持 prod 环境为手动同步。
- **db-seed PostSync Hook UNIQUE 约束修复**（hotfix/db-seed-system-prompt-unique-constraint）：
  - `desired_schema.sql` 补齐 `system_prompt.name` 字段的 `CONSTRAINT system_prompt_name_key UNIQUE (name)` 声明，使 `ON CONFLICT (name)` 种子 SQL 可正常执行。
  - `desired_extras.sql` 新增幂等 `DO $$` 块，存量环境（未包含该约束的旧部署）在下次 ArgoCD deploy 时自动补齐约束，无需人工干预。
  - staging 环境已直接热修复数据库约束并重命名为 `system_prompt_name_key`，db-seed Job 下次重建后可正常完成。
- **API 网关命令反馈 `exec-result` 路由与鉴权修复**：
  - 修复 API 网关 (`api-gateway`) 缺少 `/api/conversations/{conversation_id}/exec-result` 代理路由，导致前端命令执行结果无法回传的问题。
  - 支持对不携带 Bearer 鉴权头的客户侧匿名请求，在网关层自动填充 `Bearer client-session-placeholder-token` 进行安全绕过，契合 `conversation-service` 端 MVP 阶段的临时鉴权需求。
- **IP 格式校验放宽与工具执行结果截断修复**：
  - 修复 `react_engine.py` 参数前置校验，对包含 `ip` 的参数（如 `node_ip`）在没有显式声明 `format: ipv4` 时同时兼容主机名/节点名（如 `SVR_aCloud_670`），解决部分命令因主机名校验失败而报错的问题。
  - 修复 `react_engine.py` 在参数校验失败时未向前端发送 `tool_result` 事件导致控制台悬挂卡在“正在等待输出...”的 Bug。
  - 修复 `terminal_bridge` 命令行输出裁剪逻辑，使用正则 `(-?\d+)` 精确匹配最终数字退出码标记，并剥离 SSH PTY 命令行回显前缀，彻底解决命令结果被提前截断的缺陷；同时新增了完整的可观测性调试日志系统，极大方便后续的问题排查。
- **SOP 变量合并逻辑与 `node_ip` 提取优先级修复**：
  - 修复 `merge_variable_schema` 中的三路合并逻辑，当 Markdown 中声明了明确的新获取策略时，不再被数据库中的旧策略强行覆盖，使得 Markdown 更新可以正确同步到 `variable_schema`。
  - 优化 `node_ip` 环境变量的提取逻辑，对 IP/主机名类变量，在告警上下文提取中优先匹配 `target`（实际发生故障的节点）而非 `host`（发起告警的监控节点）。
  - 修复 `merge_variable_schema` 中 `description` 等其他人工编辑字段的三路合并逻辑，只有当新值为空或该字段为系统默认自动推断（非 Markdown 明确指定）时才使用旧值覆盖，防止在 Markdown 中显式修改的描述由于三路合并而被旧值强制保护覆盖而失效的问题。
- **SOP 变量依赖关系（Depends On）与内置判定技能回归 LLM 优化**：
  - 升级 Markdown 变量解析与三路合并算法，全面支持提取、维护与合并 `depends_on` 依赖关系列表。
  - 在 `sop_request_variable` JIT 懒加载流程中增加 `depends_on` 前置校验，在依赖前置变量未就绪时拦截报错，规避 AI 无数据源瞎猜的问题。
  - 彻底移除了原先硬编码在内置技能库中的 `is_sys_disk` 技能，通过将其获取策略配置为 `llm_inference` 且 `depends_on = ["alert_type"]`，完全回归大模型通用推理自推导以保证方案通用性，解决业务逻辑污染微服务微内核的问题。
- **shared/models/__init__.py ORM 注册副作用消除**：
  - 根因：SQLAlchemy ORM 类定义会在 import 时把表注册到 `Base.metadata`，kb-service 导入 `shared.models.dynamic_resource` 时触发了 KB ORM 副作用，与 kb-service 内部 `KBChunk` 定义冲突导致 CrashLoopBackOff。
  - 修复：使用 Python 3.7+ `__getattr__` 实现延迟导入，包根导入 `from shared.models import X` 只加载对应模块，不再自动触发所有子模块的 ORM 注册。
  - 影响：kb-service 导入 `dynamic_resource` 不触发 KB ORM 副作用；agent-service 包根导入 `ClaimVerification`/`ReasoningOutput` 正常工作；其他服务直接导入模式不受影响。
- **LLM 推理参数可配置 + 工具调用可靠性修复**：
  - `OpenClawAssistant` 支持 temperature / top_p / logprobs 构造参数，S0/ReAct 场景分设温度
  - `invoke()` 增加瞬态网络错误重试（5xx、超时、peer closed connection）
  - `_sanitize_tool_messages` 清理对话历史中不完整的 tool_calls/tool 配对
- **docker-compose 优化 + Helm Langfuse 配置 + SOP 导入幂等修复**：
  - docker-compose.yml：移除暴露端口、统一网络配置
  - Helm agent-service deployment：新增 LANGFUSE_SECRET_KEY / PUBLIC_KEY / HOST 环境变量
  - kb-service admin.py：.md 文件导入同 .docx 生成 docx_hash 支持幂等去重
- **KBD 审核页面 Tab 分类展示与置信度筛选**：
  - 分类统计接口：`GET /api/admin/kbd/pending/stats` 关联 `kb_category` 返回分类名称+数量
  - 前端用下拉框按分类过滤，每分类独立分页；支持置信度范围筛选（高/中/低）
  - 分类筛选支持"未分类"（`ai_category_id IS NULL`）
  - api-gateway 新增 `/pending/stats` 代理路由
- **KBD 有效排查步骤解析修复**：
  - `_parse_sections`：保留 mceNonEditable 容器内所有直接子 div（修复仅保留最后一个导致子步骤丢失）
  - `_html_to_semantic_text`：修复嵌套 div 包裹的 ul/ol 被丢弃（Sangfor KB 的 `<li><div><ul>` 结构）
- **Vision 超时重试**：openai SDK 的 `APITimeoutError` / `APIConnectionError` 纳入重试条件
- **docker-compose db-migrate 声明式数据库迁移**（PR #569）：
  - 新增 `db-migrate` 服务，与 Helm `db-migrate` Job 使用相同镜像和脚本，本地修改 Schema 后通过 volume 挂载实时生效
  - entrypoint 串联 atlas_dev 初始化 → 数据迁移 → 函数 → Atlas schema apply → 触发器 → 种子数据加载全流程
  - 新增 `make db-sync` 目标：修改 `desired_schema.sql` 后一键同步，无需重启其他服务
  - 优化 `make dev-up` 分步执行：先迁移后启动应用服务，确保 Schema 就绪后再启动后端
  - 移除 postgres 的 `init.sql` 挂载（仅首次创建生效，已由 db-migrate 替代）；清理已废弃的 dbmate `db-sync`/`db-check` 目标

  - `DynamicSkillRunner` 嵌入 `observe_skill` Langfuse observation，skill 执行可独立观测
  - 诊断报告模板精简为「故障摘要 / 根因 / 修复方案」三章
  - solution 格式合并【快速恢复】【彻底解决】为统一列表
- **SOP 变量管道修复**：
  - `sop_request_variable` 支持递归依赖解析 + 变量值写回 conversation-service
  - `_find_similar_variables` 提示 LLM 正确变量名
  - `ConversationSopClient` 新增 `set_variable` JIT 变量写回
- **terminal_bridge 多节点 SSH 路由**：
  - InMessage 新增 NodeIP / Container 字段
  - sessionKey 改为 caseID@nodeIP，支持多节点自动连接
  - 前端 buildAgentExecProcessMessage 传递 nodeIp/container 到 WebSocket
- **terminal_bridge Windows/K3s 双运行形态与端到端可观测性**：
  - 同一套 Go 代码支持 `desktop`（Windows localhost）与 `cluster`（WSL K3s Pod）模式，生产默认保持 desktop 拓扑
  - Helm 新增可选 terminal-bridge Deployment/Service/同源 Ingress，customer-ui 通过运行时配置自动选择 Bridge URL
  - 新增 health/ready/status/Prometheus 端点，Pod stdout 接入 Loki，保留按工单 bridge_log 回采
  - cluster 模式默认执行 same-origin 校验，单副本运行，禁止暴露为任意网页可调用的内网 SSH 跳板
  - P0 使用完整 W3C `traceparent` 和 OTel Go SDK，将 Bridge WebSocket/SSH/结果回传真实 Span 导出到 Tempo，禁止固定 Span ID
  - stdout/stderr 分流并有界捕获，记录总字节、截断、SHA-256、超时和错误分类；受控完整内容进入 `bridge_execution_artifacts`
  - Bridge 日志使用 event_id 与 instance+seq 幂等落库；Langfuse、tool_result、Tempo、Loki、Artifact 通过 trace_id/exec_id/artifact_id 互查
  - K3s 日志采集从已 EOL 的 Promtail 迁移到 Grafana Alloy，并按 containerd CRI 格式解析
- **hci-sim KBD 27123 P0 Golden Agent E2E**（2026-07-30，阶段 A/B 于 2026-08-05 代码级收敛）：
  - `hci_sim/` 是唯一的 Go 自定义 SSH Runtime 源码目录；`deploy/helm/hci-sim/`、镜像和服务名继续使用 `hci-sim`。Runtime 使用 Manifest v2、`htp2` Scenario Lease、bounded worker queue 和 fail-closed 精确 RouteKey 隔离模拟环境；不得重新引入 `hci-sim/` 源码目录或 host 子串 simulation 标记。
  - Terminal Bridge 共用代码支持 `sim-ssh`、Lease 认证和 Trace over SSH；自动节点会话纳入 WebSocket ownership tracker，断开后 active SSH 归零。
  - dev 工单 `Q2026073088434` 已完成 Customer UI Headless Runner → Agent/CDD → Bridge → hci-sim → Artifact/Evaluation/Conclusion；三段信号 PASS，S4 definitive，同一 Trace 覆盖 7 个服务。
  - 当前仅代表 KBD 27123 单场景 Golden E2E；Windows desktop Bridge、real/sim differential、20 次稳定性和 100+ 并发仍未验收。
- **信息质量检查跳过 SOP 模式**：SOP 命中时 quality check 不再拦截

- **agent-service Langfuse Helm 条件判断修复**（PR #491，v1.49）：
  - **根因**：`deploy/helm/hci-platform/templates/agent-service/deployment.yaml` 中 Langfuse env 块的条件判断为 `{{- if .Values.langfuse }}`，仅检查 map 是否存在，未检查 `enabled` 标志。dev 环境 base values `langfuse: { enabled: false }` 使 map 存在但 enabled 为 false，模板仍渲染 `LANGFUSE_SECRET_KEY` 的 `secretKeyRef`，而 `hci-secrets` 中无此 key，导致 agent-service Pod `CreateContainerConfigError`。
  - **修复**：条件改为 `{{- if and .Values.langfuse .Values.langfuse.enabled }}`，同时检查 map 存在且 `enabled=true`。
  - **配套**：`hci-platform-env` dev values 显式声明 `langfuse.enabled: false`，防止 base chart 重构时再次踩坑。

- **SOP 技能 allowed_tools 修正与变量门禁范围优化**：
  - 修正了 `hci-alert-parsing` 和 `hci-task-parsing` 技能的 `allowed_tools` 绑定值为 `'bash_exec'`，解决 Staging 环境中执行器因工具名不匹配导致校验失败的问题。
  - 优化了 `find_missing_guarded_variables_for_node_window` 逻辑，在当前节点为非叶子节点时，只检测当前节点本身所需的受控变量，不合并子分支的前置变量进行提前阻断，从而避免 Agent 在根节点执行 acli_exec 或 get_active_alerts 等工具时被提前阻断。
- **SOP 发布与变量 Schema 更新依赖校验**：
  - 在 `kb-service` 引入了工具与技能的可用性校验，当发布 SOP（`POST /api/admin/sop/{id}/approve`）或修改变量 Schema（`PATCH /api/admin/sop/{id}/variable-schema`）时，会自动分析其变量策略，确保所有被依赖的 `tool_call` 工具或 `skill_call` 技能都在数据库（`tool_definition` / `skill_definition`）中注册且处于启用状态。
  - 如果检测到未注册或未启用的依赖，抛出 `422` 异常阻断流程，错误详情直接映射为 `ValidationIssue` 格式，与前端现有的校验报告弹框无缝对接。
- **Skill 调用失效根因分析与改进方案**（已实施）：
  - **根因**：以工单 Q2026062036731 为实例，通过 `dynamic_resource_usage_audit` 审计表确认 `hci-alert-parsing` 和 `hci-disk-vendor-lifetime` 两个关键 Skill 从未被触发。根因为变量门禁（Variable Gate）的覆盖范围存在盲区：`skill_call` 类型变量不在硬门禁范围内，且 ReAct 框架下 LLM 天然选择最短路径（`bash_exec` 直接解读 SMART 数据），完全绕过 `sop_request_variable` → Skill 触发链路。
  - **核心原则**：当前架构是 **Trust-based（信任依赖型）** 而非 **Enforce-based（强制约束型）**。任何只靠 Prompt/内容暗示建立的行为规范，都会在模型版本切换、上下文压缩、存在低阻力替代路径时失效。
  - **分层改进方案**（按优先级）：
    - **P0（核心）**：`sop_advance`/`get_sop_node` 返回体新增 `preferred_next_steps` 字段，当节点有未就绪的 `skill_call` 变量时，在 LLM 最近的 tool_result 上下文中嵌入显式推荐行动（Contextual Nudge 原则）。
    - **P1（补充）**：变量门禁分层设计，新增「软推荐（Preferred）」层专门覆盖 `skill_call`/`tool_call` 类型，缺失时不阻断但附加提示。
    - **P2（配合）**：系统提示词补充 `sop_request_variable` 使用规范段落。
  - **详细方案**：`docs/solution/agent/skill调用失效根因分析与改进方案.md`
- **ArgoCD PreSync Hook Job 失败残留污染 Application Health 修复**（D-011）：
  - **根因**：`argocd.argoproj.io/hook-delete-policy: HookSucceeded` 仅在 Hook 成功时清理 Job 资源，失败时不会触发清理，导致 Failed Job 长期残留。ArgoCD 评估 Application Health 时看到 `status.conditions[type=Failed]=True` 的 Job 会把 `status.sync.message` 标注为不健康，UI 持续显示 `Job has reached the specified backoff limit`。
  - **复盘**：`argocd-ops` Application 在 2026-07-02 02:47 因 `argocd-repo-server-probe-patch` Job 失败 6 次达 `BackoffLimitExceeded` 后，`status.operationState.phase: Failed` 一直无法被新 sync 覆盖，必须直接 patch 清空 operationState 才能恢复（`kubectl patch application ... -p='[{"op":"remove","path":"/status/operationState"}]'`）。
  - **修复**：`deploy/gitops/argocd-ops/argocd-repo-server-probe-patch.yaml` 升级到 v1.4，`hook-delete-policy` 改为 `HookSucceeded,HookFailed`，失败时也自动清理。
  - **避坑指南**：D-011（docs/deploy/pitfalls/k8s.md）

---

## 2. 技术栈

| 层 | 技术 |
|---|------|
| 后端 | Python 3.12, FastAPI, SQLAlchemy, asyncpg |
| 前端 | Vue 3, TypeScript, Vite, Element Plus |
| 数据库 | PostgreSQL 15, Redis 7 |
| 部署 | Docker Compose (开发), K3s + Helm (生产) |
| 可观测性 | OpenTelemetry, Loki, Tempo, Grafana |
| 包管理 | uv (Python), pnpm (前端) |

---

## 3. 目录结构与模块边界

```
hci-troubleshoot-platform/
├── backend/
│   ├── api-gateway/          # 流量入口、路由代理、WebSocket  [独立 Workspace]
│   ├── case-service/         # 工单全生命周期 CRUD + 状态机    [独立 Workspace]
│   ├── conversation-service/ # 对话管理、SSE 流式            [独立 Workspace]
│   ├── scheduler-service/    # Pod 热备池调度                [独立 Workspace]
│   └── shared/               # 共享代码（models, utils, db）  [⚠️ 需最先完成]
├── frontend/
│   ├── customer/             # 客户端对话 UI                 [独立 Workspace]
│   ├── admin/                # 管理控制台                    [独立 Workspace]
│   └── shared/               # 共享类型 + API 客户端          [⚠️ 需最先完成]
├── adapters/                 # CLI→OpenAI 适配器
├── database/                 # desired_schema.sql / desired_extras.sql / seeds
├── deploy/                   # Docker + Helm + 可观测性
├── scripts/                  # 自动化脚本
├── tests/                    # 根级测试
├── docs/                     # 设计文档
├── AGENTS.md                 # 本文件（项目规范，提交 git）
├── CLAUDE.md                 # → AGENTS.md（符号链接）
└── CLAUDE.local.md           # 个人本地配置（不提交 git）
```

### 模块所有权规则

- `shared/` 模块修改需最高优先级完成，其他模块依赖它
- 每个微服务（`backend/xxx-service/`）是独立的 Workspace 单元
- 前端双应用（`customer/` + `admin/`）可并行，但共享类型变更需先完成
- `database/desired_schema.sql` 或 `database/desired_extras.sql` 修改必须附带迁移说明

---

## 4. 编码规范

- 代码注释**必须使用**中文
- Git commit 消息**必须使用**中文
- **Git commit 消息和 PR 必须追加环境与工具标识**（见下方规则）
- 所有请求日志**必须使用** trace_id（W3C traceparent 自动传播）
- 数据库表设计**必须包含** trace_id 字段
- 所有新增模块**必须**进行可观测性设计（指标、日志、链路追踪）
- Python: `ruff` 做 lint + format，`target-version = "py312"`, `line-length = 120`
- TypeScript: ESLint + Prettier

### Git 推送规则（强制）

#### 文档门禁
改动 `backend/`、`frontend/`、`deploy/`、`scripts/`、`database/`、`.github/workflows/` 时，
**必须在同一 commit/PR 中**同步更新 `docs/`、`README.md`、`AGENTS.md` 或 `CLAUDE.md` 至少一项。
否则 CI `docs-governance` job 失败，PR 无法合并。

#### 分支与 PR 流程
- main 分支有保护规则，**禁止直接推送**，必须通过 PR
- 提交流程：创建 feature/hotfix 分支 → 推送远程 → 创建 PR → CI 全绿后合并

#### PIT-023：并发 hotfix 前置检查
创建 hotfix 分支**前**必须先执行：
```bash
gh pr list --state open
```
确认无其他 PR 正在修改同一目录。有并发 PR 时先协调合并，避免产生重复配置块。

#### PIT-024：安全基线改造必须分批 PR
全量修改 `securityContext`、`probe`、`resources.limits` 时，
必须按负载类型（**nginx / Python / Node.js**）拆成独立 PR，
不可一次提交跨多种运行时的安全基线变更。

#### PIT-025：修改 runAsNonRoot 前确认镜像文件系统
修改 `securityContext.runAsUser` 或 `runAsNonRoot` 前，确认镜像在非 root 下的写权限需求：
- **nginx 官方镜像**：需写 `/var/cache/nginx` 和 `/var/run`，必须挂载 `emptyDir` 覆盖这两个路径
- Python/Node.js 镜像：确认应用日志、临时文件写入路径的权限

### Git Commit/PR 标识规则

**所有 commit 消息末尾必须追加 `[env:<环境>:<hostname>][agent:<工具>]` 标识。**

**所有 PR 必须添加对应的 labels：`env:<环境>:<hostname>` 和 `agent:<工具>`。**

格式：
```
<commit message>

[env:<环境>:<hostname>][agent:<工具>]
```

示例：
```
fix: 修复 ArgoCD 升级脚本

[env:dev:gs][agent:codex]
```

**数据来源**：
- **环境**：从 `argocd` namespace 的标签 `hci.env.role` 获取（dev/staging/prod）
  ```bash
  kubectl get ns argocd -o jsonpath='{.metadata.labels.hci\.env\.role}'
  ```
- **hostname**：完整主机名，转小写
  ```bash
  hostname | tr '[:upper:]' '[:lower:]'
  ```
- **工具**：必须按实际执行工具填写，当前允许值包括 `codex`、`claude`、`gemini`、`copilot`、`gpt`。
  - Codex / Codex Desktop / Codex CLI：必须使用 `codex`
  - Claude Code：使用 `claude`
  - Gemini / Antigravity IDE：使用 `gemini`
  - GitHub Copilot：使用 `copilot`
  - 直接通过 GPT/Claude API 自动化提交：使用 `gpt`

**实现方式**：使用 `gcm` 和 `gpr` 函数（已配置在 `~/.my_custom_configs`）：

```bash
# Codex 提交 commit / 创建 PR
AGENT=codex gcm "fix: 修复问题"
AGENT=codex gpr "fix: 修复问题"

# Claude Code 提交 commit / 创建 PR
AGENT=claude gcm "fix: 修复问题"
AGENT=claude gpr "fix: 修复问题"

# Gemini (Antigravity IDE) 提交 commit
# 在 Antigravity 终端中执行 gcm 即可（已基于环境变量自适应），或显式指定：
gcm-g "fix: 修复问题"
# 或者：
AGENT=gemini gcm "fix: 修复问题"
AGENT=gemini gpr "fix: 修复问题"

# GitHub Copilot 提交 commit / 创建 PR
AGENT=copilot gcm "feat: 新功能"
AGENT=copilot gpr "feat: 新功能"
```

> ⚠️ **注意**：
> 1. `gcm` / `gpr` 在部分本地环境中可能默认 `AGENT=claude`。Codex、Copilot、Gemini 等非 Claude 工具必须显式加 `AGENT=<工具>` 前缀，防止 commit footer 和 PR label 打错。
> 2. `gpr` 生成的 body 可能是硬编码占位符，**创建 PR 后必须立即用以下模板补写完整描述**：
>    ```
>    ## 问题
>    （描述触发原因、影响范围、复现路径）
>    ## 修复
>    （按子任务分节列出具体改动）
>    ## 影响文件
>    （表格：文件 | 变更类型 | 说明）
>    [env:dev:gs][agent:codex]
>    ```
>    补写命令：`gh api --method PATCH /repos/{owner}/{repo}/pulls/{num} -f body="$(cat /tmp/pr_body.md)"`

---

## 5. 构建/测试命令

```bash
# 安装依赖
make install              # uv sync + pnpm install

# 开发环境
make dev-up               # Docker Compose 启动（含自动数据库迁移）
make dev-down             # Docker Compose 停止
make db-sync              # 手动数据库 Schema 同步（修改 desired_schema.sql 后使用）

# 测试（按服务隔离运行，避免 app/ 命名空间冲突）
make test                 # 全部测试
uv run pytest tests/ -q   # 根级测试
uv run pytest backend/api-gateway/tests/ -q         # 单服务测试
uv run pytest backend/conversation-service/tests/ -q

# 代码质量
make lint                 # ruff check
make quality-gate         # 完整质量门禁
make conflict-check       # worktree 冲突检测
make post-merge           # 合并后集成验证

```

---

## 6. 禁止操作清单

| 禁止操作 | 原因 |
|---------|------|
| 删除 `backend/shared/` 下的模型定义 | 多个服务依赖 |
| 直接修改 `database/desired_schema.sql` 或 `database/desired_extras.sql` 而不提供迁移说明 | 生产数据安全 |
| 修改 `deploy/helm/` 中的 Secret 值 | 安全敏感 |
| 在代码中硬编码 API Key / Token | 安全规范 |
| 修改 `pyproject.toml` 的 Python 版本要求 | 全局影响 |
| 删除或重命名已有的 REST API 路径 | 前后端兼容性 |

---

## 7. 工作前必读（避坑指南）

> **规则：在编写或审查对应类型的代码前，必须先读取相关避坑指南。**
> **规则：在排查问题前，必须优先读取相关避坑指南。**

避坑指南权威来源：`docs/deploy/pitfalls/`（部署类）和 `docs/verify/pitfalls/`（验证类）。

**所有 Agent 按场景读取对应索引：**
- 部署类：首先读取 `docs/deploy/pitfalls/_index.md`
- 验证类：首先读取 `docs/verify/pitfalls/_index.md`

Codex / OpenCode / Gemini 用户：请根据下表主动读取对应文件。

| 触发场景 | 指南文件 | 关键条目 |
|---------|----------|---------|
| **任何涉及进程/状态/外部服务的问题排查** | `docs/verify/pitfalls/debugging.md` | 原则一~六 |
| **网络/服务访问异常（502/503/超时/SSL/LLM）** | `docs/deploy/pitfalls/network-service-check.md` | §一~十一 |
| Shell 脚本、Makefile、CI 脚本 | `docs/deploy/pitfalls/shell.md` | PIT-001,002 |
| Python 代码（ORM、异常、数据类） | `docs/verify/pitfalls/python.md` | PIT-003,004,009,040,041 |
| 前端代码（pnpm、TypeScript、Vue）/ Docker 构建 | `docs/verify/pitfalls/frontend.md` | PIT-005,023,025,028,029 |
| dispatcher / 状态机 / 幂等资源管理 | `docs/verify/pitfalls/dispatcher.md` | PIT-006,007,008 |
| K8s/K3s 镜像导入、Helm、网络、HostPath | `docs/deploy/pitfalls/k8s.md` | PIT-014~019,021,022,024,034,037,038 |
| OpenClaw 401/崩溃/WebSocket/AI 超时 | `docs/verify/pitfalls/openclaw.md` | PIT-010,013,026,027,030,032,035 |
| Grafana 重定向/Ingress/iframe | `docs/deploy/pitfalls/grafana.md` | PIT-011,012,020,036 |

> 新发现的坑：先在对应 `_index.md` 分配编号（部署类 D- 前缀，验证类 V- 前缀），再写入对应分类文件，同一 commit 提交。

---

## 8. 服务间 API 变更规范（G-4）

> **违反此规范会导致服务间契约破裂和运行时 422 / KeyError 错误。**

### 8.1 变更三步法

所有修改 `backend/shared/models/` 或微服务 HTTP 接口的 PR **必须**遵循：

```
步骤 1：先更新共享类型（backend/shared/models/），提交并合并
步骤 2：更新提供方实现（新字段向后兼容，不立即删除旧字段）
步骤 3：更新所有调用方代码，移除对旧字段的依赖
步骤 4：提交 PR，CI 契约测试（tests/contract/）必须全部通过
```

### 8.2 破坏性变更禁令

| 禁止操作 | 原因 | 正确做法 |
|---------|------|---------|
| 直接重命名返回字段 | 调用方运行时 KeyError | 先增加新字段，一个 Release 后再删旧字段 |
| 删除 Pydantic 模型字段 | schema 序列化失败 | 先标注 `deprecated=True`，再删除 |
| 改变字段类型（str → int） | 类型校验报错 | 新增独立字段 + 过渡期兼容 |
| 修改 API path 不保留旧路径 | 前端 404 | 保留旧路径（返回 301 或同等处理）至少一个 Release |

### 8.3 共享类型版本管理

```python
# backend/shared/models/__init__.py
__schema_version__ = "2.1.0"
# 升级规则：
#   patch (2.1.x)  — 新增可选字段（向后兼容）
#   minor (2.x.0)  — 弃用字段（deprecated=True）
#   major (x.0.0)  — 删除已弃用字段（破坏性变更，需整体升级协调）
```

### 8.4 内部服务调用规范

- 所有服务间 HTTP 调用**必须**继承 `backend/shared/utils/internal_http.py` 的 `InternalHTTPClient`
- 调用方**必须**调用 `response.raise_for_status()`，不允许静默忽略错误响应
- 内部认证统一使用 `INTERNAL_API_TOKEN` 环境变量（由 Helm Secret 注入）

---

## 9. KBD 数据管道架构原则：内容与呈现分离（Content–Presentation Separation）

> **此原则是 KBD 数据管道的最高设计约束，优先级高于任何局部实现细节。**

### 9.1 核心思想

```
┌──────────────────────────────────────────────────────────────┐
│                   KBD 数据流向                                 │
│                                                              │
│  Sangfor 知识库（源）                                          │
│      │  原始 HTML（含任意厂商样式、排版、嵌套结构）              │
│      ▼                                                       │
│  data-pipeline（抽取层）                                       │
│      │  只关心"语义内容"：文字 + 图片                           │
│      │  丢弃：颜色、字体、缩进、加粗/斜体、HTML 结构             │
│      ▼                                                       │
│  数据库（content_md / 结构化字段）                              │
│      │  规范化的 Markdown + 图片描述块（我们定义的 schema）      │
│      ▼                                                       │
│  Frontend（呈现层）                                            │
│      │  100% 由我们控制样式：字体/颜色/缩进/展开折叠/图标        │
│      ▼                                                       │
│  用户界面（高一致性呈现）                                        │
└──────────────────────────────────────────────────────────────┘
```

### 9.2 强制规则

| 规则 | 说明 |
|------|------|
| **只取语义，不取样式** | `_html_to_semantic_text` 和 `_parse_sections` 只提取文字语义内容和图片占位符（`![img:N]`），原始 HTML 的 `style`、`class`、`font`、`color` 等样式属性一律丢弃 |
| **输出格式由我们定义** | content_md 的 schema（章节结构、截图块格式、占位符格式）由本项目的 `converter.py` 定义并保持稳定，不受源 HTML 格式变化影响 |
| **截图块统一封装** | content_md 中图片以 `> **【截图说明】**` 块渲染（由后端 `rebuild_content_md` 展开 `![img:N]` 占位符得到），字段结构（BACKGROUND/TYPE/FULL_TEXT/KEY/TIPS/DESCRIPTION）固定；前端解析并渲染，不依赖原始图片 URL 或 alt 属性 |
| **前端是样式唯一来源** | 所有展示细节（展开/折叠、缩进、颜色、图标、字体）只在前端实现，data-pipeline 不输出任何影响呈现的 HTML/CSS |
| **管道规范化** | content_md 由后端 `rebuild_content_md()` 统一渲染（data-pipeline 只输出含 `![img:N]` 占位符的语义文本，不生成 content_md），保证截图块顶格、空行折叠等格式由单一权威函数控制，不受源 HTML 格式变化影响 |

### 9.3 违规示例与正确做法

```python
# ❌ 错误：将源 HTML 的样式带入 Markdown
def wrong():
    return f"**{text}**"       # 保留了原始加粗
    return f"<span style='color:red'>{text}</span>"  # 带入 HTML 样式

# ✅ 正确：只提取纯文本语义内容
def correct():
    return soup.get_text(strip=True)   # 剥离所有标签，只取文字
    return f"![img:{seq}]"  # 图片以占位符输出，视觉描述存入 images_json.desc，由后端渲染截图块
```

### 9.4 扩展新字段时的检查清单

当 data-pipeline 需要处理新的内容类型时，**必须**回答以下问题：

- [ ] 新字段速度或语义是「内容」还是「样式」？如果是样式，丢弃它
- [ ] 输出格式是否遵循现有 schema（Markdown 段落、截图块、占位符）？
- [ ] 前端是否能在不修改解析逻辑的情况下渲染新内容？
- [ ] 输出是否仅含语义文本 + `![img:N]` 占位符（content_md 由后端 `rebuild_content_md` 统一渲染）？
- [ ] 新字段有对应的单元测试验证输出格式吗？

### 9.5 设计背景

此原则来自 2026-07 KBD 27123 案例的问题复盘：

- **根因**：`markdownify` 在将嵌套列表内的截图 span 转为 blockquote 时，携带了 CommonMark 规范的列表缩进（属于样式信息），污染了 content_md，导致前端解析失败
- **修复**：data-pipeline 改为只取语义文本（新增 `_html_to_semantic_text` 作为唯一权威提取器），content_md 不再由 pipeline 生成，改由后端 `rebuild_content_md()` 统一渲染——截图块格式由单一权威函数保证，彻底根除 markdownify 缩进污染
- **结论**：data-pipeline 必须对输出做**格式规范化**（只输出语义文本 + `![img:N]` 占位符），任何来自 markdownify 或源 HTML 的"意外格式"都不应透传到 content_md
