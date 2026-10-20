# QKV/QFK 扩展性与配置易用性评估

> 版本：v1.0
> 日期：2026-07-16
> 作者：AI 助手 + 人类工程师协作
> 关联文档：[关键信号架构落地任务.md](../../../task/agent/关键信号架构落地任务.md)
> 关联代码：`backend/agent-service/app/tools/qkv/`、`backend/agent-service/app/tools/qfk/`

---

## 一、核心认知纠正

### 1.1 QKV/QFK 的定位

> **QKV/QFK 就是跟 acli/sop/scp 一样的 tool**，只是定位不同。

| 工具类型 | 定位 | 调用方式 | 扩展性要求 |
|---------|------|---------|-----------|
| acli_exec | 通用命令执行 | LLM 自主决策调用 | 参数固定 |
| get_active_alerts | 告警查询 | LLM 自主决策调用 | 参数固定 |
| get_sop_node | SOP 导航 | LLM 自主决策调用 | 参数固定 |
| **QKV** | 前端信号（生产者） | KBD 差分引擎调用 | **可配置输入+输出** |
| **QFK** | 后端信号（消费者） | KBD 差分引擎调用 | **可配置输入+输出** |

### 1.2 为什么需要可配置的输入/输出

**问题背景**：agent 不能执行 KBD 的根因是获取信息依赖**隐藏经验**。

> 例如 KBD 写"登录主机后台检查镜像文件是否被其他进程占用"，但隐藏经验是：
> - 怎么知道登录哪台主机？（需要从任务详情中提取 HOST）
> - 用什么命令查？（lsof | grep <vmid>）
> - VMID 从哪来？（从前端任务查询结果中提取）
> - 输出怎么解析？（要找到 .qcow2 文件和 PID）

**解决方案**：把这些隐藏经验工具化——QKV/QFK 就是承载这些隐藏经验的 tool。

### 1.3 acli 限制的作用

> acli 限制是**兜底安全网**：LLM 幻觉了要删系统文件，acli 会报错拒绝执行。

**不是**：防止 QKV/QFK 被 LLM 调用
**而是**：即使 LLM 幻觉，acli 也会拦截危险操作

---

## 二、扩展性评估

### 2.1 QKV 输入配置扩展性

| 参数 | 配置方式 | 扩展性 | 说明 |
|------|---------|--------|------|
| `keyword` | acquirer_args.keyword | ✅ 灵活 | 任意搜索关键字 |
| `is_failed` | acquirer_args.is_failed | ✅ 布尔 | 过滤失败任务 |
| `limit` | acquirer_args.limit | ✅ 数值 | 结果数量限制 |
| `query` | acquirer 类型推断 | ⚠️ 固定 3 种 | alert/task/dialog，不支持扩展 |

**评估结论**：QKV 输入参数覆盖常见场景，但 `query` 类型固定为 3 种（alert/task/dialog），新增查询类型需改代码。

### 2.2 QKV 输出配置扩展性

| 特性 | 实现方式 | 扩展性 | 说明 |
|------|---------|--------|------|
| 动态字段提取 | produces=[{name, path}] | ✅ 完全可配置 | 自定义输出字段 |
| 多路径容错 | path="vm\|object_id" | ✅ 支持 | 字段名兼容 |
| 硬编码兜底 | produces 为空时 | ✅ 兼容旧信号 | 向后兼容 |

**评估结论**：✅ QKV 输出配置扩展性**满足需求**。`produces` 字段支持完全自定义输出，无需改代码。

### 2.3 QFK 输入配置扩展性

| 参数 | 配置方式 | 扩展性 | 说明 |
|------|---------|--------|------|
| `namespace` | acquirer 后缀 | ⚠️ 固定 8 种 | log/service/vm/network/storage/hardware/platform/system |
| `sub_command` | acquirer_args.sub_command | ✅ 灵活 | 任意 acli 子命令 |
| `keywords` | matcher.pattern | ✅ 灵活 | 关键字列表 |
| `match_mode` | matcher.mode | ✅ 固定 2 种 | any/all |
| `target.*` | acquirer_args.target | ✅ 灵活 | 资源定位 |
| `container` | acquirer_args.container | ✅ 灵活 | 服务容器类型 |

**评估结论**：QFK 输入参数基本满足需求，但 `namespace` 固定为 8 种，新增 namespace 需：
1. 在 `HandlerRegistry._registry` 中添加映射
2. 在 `ACQUIRER_CATALOG` 中添加描述
3. 更新 DB Prompt 模板

### 2.4 QFK 输出配置扩展性

| matcher 类型 | 用途 | 扩展性 | 说明 |
|-------------|------|--------|------|
| `keyword` | 关键字匹配 | ✅ 已实现 | any/all 模式 |
| `regex` | 正则匹配 | ✅ 已实现 | 支持复杂模式 |
| `state` | 状态匹配 | ✅ 已实现 | running/stopped/healthy |
| `threshold` | 阈值比较 | ✅ 已实现 | >/>=/</<=/==/!= |
| `json_path` | JSON 路径取值 | ✅ 已实现 | 嵌套 JSON 提取 |
| `exists` | 存在性判定 | ✅ 已实现 | 含否定标记检测 |

**评估结论**：✅ QFK 输出配置扩展性**满足需求**。6 种 matcher 类型覆盖全部判定场景。

---

## 三、配置易用性评估

### 3.1 当前状态

| 维度 | 现状 | 问题 |
|------|------|------|
| 工具定义注册 | ❌ QKV/QFK **不在** tool_definition 表 | 无 admin-ui 参数配置页面入口 |
| HandlerRegistry | 硬编码 8 个 namespace | 新增 namespace 需改代码 + 发版 |
| ACQUIRER_CATALOG | 硬编码在代码中 | 修改需发版 |
| produces 配置 | 写在 KBD signals_json 中 | 运营人员无法在 admin-ui 编辑 |
| matcher 配置 | 写在 KBD signals_json 中 | 运营人员无法在 admin-ui 编辑 |

### 3.2 与 tool_definition 工具的对比

| 维度 | tool_definition 表工具 | QKV/QFK |
|------|------------------------|---------|
| 注册方式 | DB 表 → ToolRegistryManager 启动加载 | 硬编码在代码中 |
| 动态扩展 | ✅ 支持（15s TTL 热刷新） | ❌ 不支持 |
| Admin-UI 配置 | ✅ ToolManageView.vue | ❌ 无专用页面 |
| LLM 可发现 | ✅ 自动出现在工具列表 | ❌ 需差分引擎路由 |

### 3.3 QKV/QFK 注册到 tool_definition 的决策

> **最终决策：将 QKV/QFK 注册到 tool_definition，复用现有 ToolManageView.vue 管理入口。**

| 选项 | 优点 | 缺点 | 最终决策 |
|------|------|------|---------|
| 注册到 tool_definition | 复用现有 Admin-UI，降低开发成本 | 需要增强 ToolManageView.vue | ✅ **采纳** |
| 新建信号模板管理页 | 语义清晰，可定制 UI | 需要额外开发 7 天 | ❌ 未采纳 |

**决策理由**：
1. **降低开发成本**：无需新建 `SignalTemplateManageView.vue`，节省约 7 天开发时间
2. **语义相符**：QKV/QFK 本质是工具，与 acli/sop/scp 并列，注册到 tool_definition 符合工具定义
3. **易用性已达标**：通过 `ProducesEditor.vue` 和 `MatcherEditor.vue` 提供可视化编辑，配置门槛已降低

**实现方式**：
- 通过 `database/seeds/03_qkv_qfk_tools.sql` 注册 11 个工具定义
- 在 `ToolManageView.vue` 新增 QKV/QFK 分类选项和可视化编辑 Tab
- 新增 `ProducesEditor.vue`（产出变量编辑）和 `MatcherEditor.vue`（判定器编辑）

---

## 四、缺口分析

### 4.1 扩展性缺口

| 缺口 | 影响 | 优先级 | 解决方案 |
|------|------|--------|---------|
| HandlerRegistry 硬编码 | 新增 namespace 需改代码 | 中 | 改为 DB 配置加载 |
| ACQUIRER_CATALOG 硬编码 | 修改需发版 | 中 | 迁移到 DB 表 |
| QKV query 类型固定 | 无法新增查询类型 | 低 | 当前 3 种够用 |
| QFK namespace 固定 | 无法新增 namespace | 中 | 当前 8 种够用，但需考虑扩展 |

### 4.2 易用性缺口

| 缺口 | 影响 | 优先级 | 解决方案 |
|------|------|--------|---------|
| 无 admin-ui 配置页面 | 运营人员无法编辑 produces/matcher | **高** | 新建 SignalTemplateManageView.vue |
| produces 字段需手写 JSON | 配置门槛高 | 高 | 提供 produces 字段动态表单编辑器 |
| matcher 配置需手写 JSON | 配置门槛高 | 高 | 提供 matcher 类型选择器 + 参数表单 |

### 4.3 功能缺口

| 缺口 | 影响 | 优先级 | 解决方案 |
|------|------|--------|---------|
| 变量池填充 bug（已修复） | produces 无法传递 | **已修复** | 改为 name.lower() 查找 |
| threshold 提取首个数值 | SMART 5 值场景需调整输出格式 | 低 | 文档说明或改进提取逻辑 |

---

## 五、任务计划

### 5.1 第一优先级：QKV/QFK 可视化编辑支持

> **状态：✅ 已实现（方案调整）**
>
> 实际实现决策：将 QKV/QFK 注册到 `tool_definition` 表，复用现有 `ToolManageView.vue` 管理入口，
> 无需新建 `SignalTemplateManageView.vue`，节省约 7 天开发时间。

**实际实现清单**：

| 序号 | 任务 | 状态 | 输出 |
|------|------|------|------|
| 1 | QKV/QFK 注册到 tool_definition | ✅ 已完成 | `database/seeds/03_qkv_qfk_tools.sql`（11 个工具定义） |
| 2 | HandlerRegistry 动态注册改造 | ✅ 已完成 | `handlers.py` 新增 register/unregister/get/supported_namespaces 方法 |
| 3 | 可视化表单：ProducesEditor.vue | ✅ 已完成 | 产出变量数组编辑器 |
| 4 | 可视化表单：MatcherEditor.vue | ✅ 已完成 | 6 种 matcher 类型选择器 |
| 5 | ToolManageView.vue 集成 | ✅ 已完成 | QKV/QFK 分类 + 双 Tab（可视化/JSON）编辑 |

**原计划（已废弃）**：

| 序号 | 任务 | 预估工时 | 输出 |
|------|------|---------|------|
| 1 | 新建 signal_template 表 | 0.5 天 | DB migration |
| 2 | HandlerRegistry 改为从 DB 加载 | 1 天 | 代码改动 |
| 3 | 后端 API：GET/POST/PUT/DELETE /api/v1/signal-templates | 1 天 | 路由代码 |
| 4 | 前端：SignalTemplateManageView.vue | 2 天 | Vue 组件 |
| 5 | produces 字段动态表单编辑器 | 1 天 | Vue 组件 |
| 6 | matcher 类型选择器 + 参数表单 | 1 天 | Vue 组件 |
| 7 | 集成测试 | 0.5 天 | 测试代码 |

**总计**：约 7 天（已通过方案调整节省）

### 5.2 第二优先级：ACQUIRER_CATALOG 迁移到 DB

**目标**：新增/修改 acquirer 描述无需发版。

**任务清单**：

| 序号 | 任务 | 预估工时 | 输出 |
|------|------|---------|------|
| 1 | 新建 acquirer_catalog 表 | 0.5 天 | DB migration |
| 2 | extract_signals.py 改为从 DB 加载 | 0.5 天 | 代码改动 |
| 3 | 种子数据迁移 | 0.5 天 | SQL seed |
| 4 | Admin-UI 管理页面（可选） | 1 天 | Vue 组件 |

**总计**：约 2.5 天

### 5.3 第三优先级：文档与培训

**目标**：确保运营人员理解 QKV/QFK 配置方法。

**任务清单**：

| 序号 | 任务 | 预估工时 | 输出 |
|------|------|---------|------|
| 1 | 更新架构文档（已完成） | - | 本文档 |
| 2 | 编写运营手册 | 1 天 | Markdown 文档 |
| 3 | 录制配置教程视频 | 2 天 | 视频文件 |

**总计**：约 3 天

---

## 六、总结

### 6.1 评估结论

| 维度 | 评分 | 说明 |
|------|------|------|
| QKV 输入配置扩展性 | ⭐⭐⭐⭐ (4/5) | 参数灵活，query 类型固定但够用 |
| QKV 输出配置扩展性 | ⭐⭐⭐⭐⭐ (5/5) | produces 完全可配置，支持多路径容错 |
| QFK 输入配置扩展性 | ⭐⭐⭐⭐ (4/5) | sub_command 灵活，namespace 固定但够用 |
| QFK 输出配置扩展性 | ⭐⭐⭐⭐⭐ (5/5) | 6 种 matcher 覆盖全部判定场景 |
| 配置易用性 | ⭐⭐⭐⭐ (4/5) | 已有可视化编辑器（ProducesEditor + MatcherEditor），支持双 Tab 编辑 |

### 6.2 核心结论

> **QKV/QFK 的扩展性已满足需求，配置易用性已通过可视化编辑器提升。**
>
> - 扩展性：produces 动态提取、6 种 matcher 类型均已实现，新增场景无需改代码
> - 易用性：✅ 已通过 `ToolManageView.vue` + 可视化编辑器实现，运营人员可通过表单编辑
> - 实现决策：注册到 `tool_definition` 表，复用现有管理入口，无需新建信号模板管理页

### 6.3 后续优化建议

1. **已完成**：QKV/QFK 可视化编辑支持（通过 ToolManageView.vue + 双 Tab 编辑）
2. **中优先级**：ACQUIRER_CATALOG 迁移到 DB（2.5 天）
3. **低优先级**：HandlerRegistry 改为 DB 加载（当前动态注册已支持，但可进一步优化）

---

*文档结束。*