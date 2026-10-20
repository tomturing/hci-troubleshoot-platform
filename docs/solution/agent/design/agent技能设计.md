---
status: active
category: solution
audience: developer
last_updated: 2026-06-05
owner: team
---

# Agent 技能设计

> 本文档描述 HCI 智能排障平台 Skill（技能）子系统的完整设计方案，遵循 [Agent Skills Open Standard](https://agentskills.io)（由 Anthropic 发起，已被 Claude Code、GitHub Copilot、OpenAI Codex、Cursor、Gemini CLI、Roo Code 等 30+ 主流 AI Agent 客户端采纳）。

---

## 一、核心概念：Skill vs Tool 的本质区别

理解 Skill 的前提是明确它与 Tool 的本质差异，这两个概念在业界经常被混淆：

```
┌──────────────────────────────────────────────────────────────────┐
│  TOOL（工具）                                                     │
│  本质：函数声明 / 可调用的原子操作                                  │
│  内容：函数名 + 参数类型 Schema + 输出 Schema                      │
│  作用："你能做什么操作"                                            │
│  触发：Agent 显式调用（主动选择工具）                               │
│  例子：execute_ssh_command(host, cmd) → {stdout, returncode}      │
│  ✅ tool_definition 表设计正确，保持不变                            │
└──────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────┐
│  SKILL（技能）                                                    │
│  本质：过程性知识包 / 领域专业知识 + 标准操作流程                    │
│  内容：Markdown 指令正文 + 辅助资源文件                             │
│  作用："你在做这类任务时应遵循哪些流程、规范和注意事项"               │
│  触发：Agent 根据任务语义自动激活（渐进式加载）                      │
│  例子：hci-disk-vendor-lifetime — 包含厂商识别规则 + 各厂商寿命     │
│        判断标准 + Gotchas（希捷磁盘 Raw_Read_Error_Rate 正常偏高等）│
└──────────────────────────────────────────────────────────────────┘
```

**历史问题**：原 `skill_definition` 表设计了 `parameters_schema` / `output_schema` 两个字段，
实质上是把 Skill 设计成了 Tool（函数接口），未能体现 Skill 的知识包本质。本次重新设计完全纠正这一偏差。

---

## 二、Agent Skills 开放标准要点

### 2.1 SKILL.md 结构规范

标准定义 Skill 为一个目录，核心文件是 `SKILL.md`：

```
skill-name/
├── SKILL.md          # 必须：YAML frontmatter + Markdown 指令正文
├── scripts/          # 可选：可执行脚本（Python / Bash / JS）
├── references/       # 可选：参考文档（技术手册、Schema 文件等）
└── assets/           # 可选：静态资源（输出模板、图片、数据表）
```

### 2.2 SKILL.md Frontmatter 字段规范

| 字段 | 必填 | 约束 | 说明 |
|------|------|------|------|
| `name` | ✅ | 1-64 字符，kebab-case，不以连字符开头/结尾，无连续连字符 | Skill 唯一标识，须与目录名一致 |
| `description` | ✅ | 1-1024 字符 | 描述"做什么"及"何时触发"，Agent 发现阶段只读此字段 |
| `license` | ❌ | 1-100 字符 | 许可证名称或内置文件路径 |
| `compatibility` | ❌ | 1-500 字符 | 环境依赖说明（系统版本、工具、网络权限等） |
| `metadata` | ❌ | key-value 映射 | 扩展元数据（作者、分类、标签等） |
| `allowed-tools` | ❌ | 空格分隔 | 预批准工具列表（实验性） |

### 2.3 渐进式加载机制（Progressive Disclosure）

```
启动时   ──► 只加载 name + description（~100 tokens，所有 Skill）
任务匹配 ──► 读取完整 SKILL.md 正文（< 5000 tokens 推荐，激活的 Skill）
执行时   ──► 按需加载 scripts/ references/ assets/ 中的文件
```

这是 Skill 区别于 Prompt 的核心机制：**最小化上下文占用，按需扩展**。

---

## 三、数据库设计决策

### 3.1 方案选择

**本项目采用"数据库替代文件系统"方案**，而非标准的"文件系统目录"方案。

#### 决策依据

| 维度 | 文件系统目录（标准方案） | 数据库存储（本项目方案）| 决策 |
|------|----------------------|----------------------|------|
| **编辑方式** | 需要 IDE 或 CLI，无法通过 Web UI 管理 | 通过管理控制台直接编辑 | ✅ 数据库 |
| **多环境同步** | 需要 git 版本控制 + 部署流水线 | 数据库本身提供一致性，通过 seed SQL 迁移 | ✅ 数据库 |
| **检索/过滤** | 文件系统无法高效按分类/标签检索 | JSONB GIN 索引支持高效检索 | ✅ 数据库 |
| **Agent 下发** | 需要文件系统访问，在 K8s 容器中较复杂 | 直接通过 API 下发，与现有架构无缝集成 | ✅ 数据库 |
| **版本历史** | git log 提供完整历史 | 需要另行实现（当前不做） | ❌ 文件系统 |
| **标准兼容性** | 完全符合 agentskills.io 规范 | 字段语义遵循标准，存储形式有差异 | 可接受 |

**结论**：HCI 平台是企业内部运维系统，需要运维人员通过 Web 控制台灵活管理 Skill，数据库方案更符合实际运维场景。字段语义和内容结构完全遵循 Agent Skills 开放标准。

### 3.2 新表 `skill_definition`（替换旧表）

旧表删除重建，字段按 Agent Skills 标准重新设计：

```sql
-- 删除旧表（含所有旧字段 parameters_schema / output_schema）
DROP TABLE IF EXISTS skill_definition CASCADE;

CREATE TABLE skill_definition (
    id                  SERIAL PRIMARY KEY,

    -- ===== 标准规范字段（对应 SKILL.md frontmatter）=====

    -- name: kebab-case，符合 Agent Skills name 字段规范
    -- 规则：1-64字符，小写字母+数字+连字符，不以连字符开头/结尾，无连续连字符
    skill_name          VARCHAR(64) NOT NULL UNIQUE,

    -- description: 供 Agent 发现阶段使用，描述"做什么"和"何时触发"
    -- 最长 1024 字符（与标准一致），必须包含触发条件关键词
    description         VARCHAR(1024) NOT NULL,

    -- instructions_md: SKILL.md 正文 Markdown（供 Agent 激活阶段加载）
    -- 内容：Step-by-step 指令 + Gotchas + 示例 + 输出模板 + 检查清单等
    -- 建议不超过 500 行 / 5000 tokens
    instructions_md     TEXT NOT NULL DEFAULT '',

    -- compatibility: 环境兼容性（可选，最长 500 字符）
    compatibility       VARCHAR(500),

    -- license: 许可证（可选）
    license             VARCHAR(100),

    -- allowed_tools: 预批准工具列表（空格分隔，实验性）
    allowed_tools       TEXT,

    -- metadata_json: 扩展元数据 key-value（对应标准 metadata 字段）
    -- 建议字段：{"author": "...", "category": "...", "tags": [...]}
    metadata_json       JSONB NOT NULL DEFAULT '{}',

    -- ===== 平台扩展字段（超出标准规范，满足平台管理需求）=====

    -- display_name: 中文展示名（平台特有，非标准字段）
    display_name        VARCHAR(200),

    -- is_active: 启用开关（平台管理需求）
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,

    -- assets_json: 轻量资产内联存储（模拟 assets/ 目录）
    -- 格式：[{"filename": "template.md", "type": "template", "content": "..."}]
    assets_json         JSONB NOT NULL DEFAULT '[]',

    -- references_json: 参考文档内联存储（模拟 references/ 目录）
    -- 格式：[{"filename": "REFERENCE.md", "title": "...", "content": "..."}]
    references_json     JSONB NOT NULL DEFAULT '[]',

    trace_id            VARCHAR(64),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_skill_definition_active   ON skill_definition(is_active);
CREATE INDEX idx_skill_definition_metadata ON skill_definition USING GIN(metadata_json);
```

### 3.3 其他开放问题的决策

#### Q2：Markdown 编辑器选型

**决策：原生 `<textarea>`（代码字体样式），阶段二再引入 CodeMirror**

**依据**：
- 当前 HCI 团队使用者主要是运维工程师，不需要富文本 WYSIWYG
- 引入 CodeMirror/Monaco 增加 ~500KB 前端体积，且需要额外维护语法高亮配置
- textarea + Markdown 预览（marked.js）已足够满足当前需求
- 预览功能通过现有项目已使用的 `marked` + `DOMPurify` 实现（zero new dependency）

#### Q3：种子数据范围

**决策：当前预置 1 个 HCI 存储类 Skill（硬盘厂商识别与寿命判定）**

**依据**：
- 该 Skill 有完整的原始材料（`skills/硬盘厂商识别与寿命判断.md`），可直接转化为标准格式
- 其他 Skill 需要领域专家知识支撑，后续由运维团队通过管理控制台自行添加
- Seed 数据以 SQL 文件形式维护，便于在新环境中幂等重建

#### Q4：references/assets 文件内联 vs 文件上传

**决策：内联存储在 JSONB 字段中，不实现文件上传**

**依据**：
- HCI 平台 Skill 的辅助内容均为文本（Markdown、YAML），体积小，内联合理
- 避免引入对象存储（MinIO/S3）依赖，降低运维复杂度
- 若未来需要存储二进制文件（图片、脚本），可扩展 `assets_json` 的 `content` 为 base64 或外链

---

## 四、Skill 正文最佳实践

以下模式来自 Agent Skills 官方最佳实践，适用于 HCI 运维场景：

### 4.1 Gotchas 陷阱清单（最高价值内容）

```markdown
## Gotchas

- 希捷（Seagate）磁盘的 SMART 第 1 项（Raw_Read_Error_Rate）的 RAW_VALUE
  通常非常高（正常现象），勿误判为故障。应关注第 5、187、197、198 项。
- SMART 第 173 项（Kioxia）的触发条件是 VALUE 字段，而非 RAW_VALUE，
  两者单位和含义不同，必须区分。
```

### 4.2 计划-校验-执行模式（Plan-Validate-Execute）

适用于批量操作或破坏性操作前的安全验证。

### 4.3 描述字段（description）写法规范

```yaml
# 不好（过于笼统）
description: 硬盘相关诊断

# 好（描述做什么 + 何时触发）
description: >
  识别 HCI 节点物理磁盘的厂商，并依据各厂商专属 SMART 指标（第 173、233、
  177、202、167、231 项等）判断磁盘是否达到返修阈值。当用户报告磁盘 IO 异常、
  存储池降级、坏道告警，或需要确认磁盘健康状态时触发。
```

---

## 五、前端 UI 设计

### 5.1 列表页

| 列 | 内容 | 宽度 |
|----|------|------|
| Skill 标识 | `<code>skill-name</code>` + 展示名称（换行） | min-width 220 |
| 描述（触发条件）| description，show-overflow-tooltip | flex |
| 分类 | `metadata_json.category` 标签 | 120 |
| 兼容性 | `compatibility` 小字 | 160 |
| 状态 | Switch（启用/禁用） | 100 |
| 操作 | 编辑 + 删除（同行，宽度固定保证不换行） | **150 固定** |

### 5.2 编辑弹窗（参考 SOP 详情弹窗样式，全宽 Tab 布局）

**Tab 1：基本信息**
- skill_name（kebab-case，新建可编辑，编辑时禁用）
- display_name（中文展示名）
- description（多行，最长 1024 字符，带字符计数器）
- compatibility（单行，最长 500 字符）
- license（单行）
- is_active（Switch）
- allowed_tools（Tag 输入，空格分隔）
- metadata_json（author / category / tags 三个常用字段 + 自定义键值对）

**Tab 2：技能指令（SKILL.md 正文）**
- 左：`<textarea class="code-textarea">`（代码字体 Consolas，行号显示）
- 右：Markdown 预览（marked + DOMPurify，与项目其他 Markdown 渲染保持一致）
- 折叠提示面板：推荐内容要素（Gotchas、Step-by-step、示例、输出模板）

**Tab 3：资源文件**
- references_json：参考文档列表（添加 / 编辑 / 删除，内联编辑器）
- assets_json：资源文件列表（同上）

---

## 六、API 设计

路由前缀保持 `/api/v1/skills`（替换旧实现，不需要 v2 前缀，因为是全新重建）。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/skills` | 列表查询（支持 `is_active`、`category` 过滤） |
| GET | `/api/v1/skills/{id}` | 获取单个 Skill 完整内容（含 instructions_md） |
| GET | `/api/v1/skills/{id}/discovery` | 仅返回 name + description（Agent 发现阶段专用） |
| POST | `/api/v1/skills` | 创建新 Skill |
| PUT | `/api/v1/skills/{id}` | 更新 Skill |
| DELETE | `/api/v1/skills/{id}` | 删除 Skill |

> **设计说明**：`/discovery` 端点模拟 Agent Skills 标准的"发现阶段"，
> 只返回 100 tokens 级别的元数据，可用于未来对接外部 Agent 客户端。

---

## 七、种子数据清单

| skill_name | display_name | category | 来源 |
|-----------|--------------|----------|------|
| `hci-disk-vendor-lifetime` | 硬盘厂商识别与寿命判定 | storage | `skills/硬盘厂商识别与寿命判断.md` |

后续由运维团队通过管理控制台自行添加更多 Skill。

---

## 变更历史

| 日期 | 版本 | 变更内容 | 关联事件文档 |
|------|------|---------|------------|
| 2026-06-05 | v1.0 | 初版，基于 Agent Skills Open Standard 重新设计技能系统 | — |
