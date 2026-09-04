# 关键信号多 Agent 分层建模与最佳实践库架构设计方案

> **规范与治理声明**：
> 本设计方案全程统一基于**第一性原理（First Principles）**解构信号物理本质，并以**对抗性审查（Adversarial Review）**穿透潜在漏洞。
> 方案深度整合 staging 环境 **23 个已发布 KBD / 67 个专家金标信号** 的全量实测对账数据，将原单一黑盒抽取 Agent 彻底解耦重构为分层、分工、可观测、具备自愈闭环的多 Agent 协同系统。
>
> 状态：设计定稿（Approved）  
> 归档目录：`docs/solution/agent/02-架构设计/`  
> 最近更新：2026-09-04

---

## 零、架构演进决策背景与设计哲学

### 0.1 第一性原理解构：单体 Agent 认知过载的物理必然性
一个达到工业级可执行标准的“关键信号（Signal）”，在逻辑与工程上包含四个不可逾越的认知层级：
1. **事实解构层**：从半结构化运维排障叙事中提取物理实体、现象描述与处置动作边界，确定信号的独立数量与观察维度；
2. **工具映射层**：在平台已严格定义的 13 个采集探针词表（5 QKV 生产者 + 8 QFK 消费者）中，基于关键字、动作语义与分类基线实现精准二选一或分类收敛；
3. **结构建模层**：将自然语言指令准确翻译为具备可执行契约的 JSON（包含 `acquire.args` 标准命令/路径、`match` 模式规则、`extract` 取值规则、`orchestrate` 变量流转拓扑）；
4. **拓扑与门禁层**：验证跨信号间变量生产与消费的 DAG 有向无环闭环性，确保命令符合 aCLI 白名单、无硬编码特定环境 UUID、无必然恒真的伪断言。

**单体 Agent 的致命缺陷**：原系统试图让单个 Prompt 在一次 LLM 交互中同时消化上述全部四层异构认知，大模型的注意力在长上下文与多重约束下被严重稀释，导致：
- **漏抽率高达 37.3%**（专家最终 67 个信号中有 25 个为手工全新增补）；
- **参数失真率高达 68.2%**（30 个信号被专家大幅修改）；
- **工具错配严重**（`qkv_alert` 过度多配 +350%，而核心的 `qfk_log` 严重欠配 -82%）；
- **深层逻辑空白**（数值阈值 `threshold` 类型的抽取覆盖率为绝对 0%）。

### 0.2 重构原则：多 Agent 分层流水线与资产闭环
本方案将单一 Agent 拆分为四个职责单一、边界清晰的 Agent 梯队，各 Agent 间通过严格的数据契约和拓扑协议进行解耦调度：
- **a. 计数 Agent（业务与实体理解）**：对数量与实体负责，解决“漏抽”根因；
- **b. 分类 Agent（属性与语义抽象）**：对工具与语义负责，解决“工具错配”根因；
- **c. 建模 Agent（规则与边界约束）**：对单一信号的标准化 JSON 负责，解决“参数失真与硬编码”根因；
- **d. 验证 Agent（验证、评审与反馈）**：对全局拓扑、门禁通过率与自愈负责，解决“门禁只拦不修”根因。

同时，建立**建模模板库（Template）**与**最佳实践库（Best Practice）**作为模型生成的生成前强约束（In-Context Knowledge），配合**异常抽取日志表（Failure Log）**实现数据飞轮闭环。

---

## 一、多 Agent 分工分层架构体系详解

```
                             ┌─────────────────────────────────────────────────────────────┐
                             │                   KBD 结构化文档输入                        │
                             │   (【标题】+【问题描述】+【告警信息】) 复合源  │  【有效排查步骤】 步骤源 │
                             └──────────────────────────────┬──────────────────────────────┘
                                                            │
                                                            ▼
                             ┌─────────────────────────────────────────────────────────────┐
                             │                  1. 计数 Agent (业务与实体理解)             │
                             │  - 纯内容驱动，屏蔽字段名称偏见                             │
                             │  - 实体/动作边界切分（复合源至少 1 生产者，步骤源≥1 消费者）  │
                             │  - 角色感知去重（避免现象被步骤粗暴覆盖）                   │
                             │  - 产出：Signal Intent Manifest (带全局变量意图标注)        │
                             │  - 异常落库：signal_failure_extraction (UNCOUNTABLE)        │
                             └──────────────────────────────┬──────────────────────────────┘
                                                            │
                                                            ▼
                             ┌─────────────────────────────────────────────────────────────┐
                             │                  2. 分类 Agent Teams (属性与语义抽象)       │
                             │  - 并发处理各 Signal Intent                                 │
                             │  - 三维判定：关键词(硬词) + 语义(动作) + 分类基线(kb_category)│
                             │  - 双重视角对抗审查（单信号局部 vs 全局上下文对抗）         │
                             │  - 13 类封闭 Catalog 映射（5 QKV + 8 QFK + 无法分类）      │
                             │  - 异常落库：signal_failure_extraction (UNCLASSIFIED)      │
                             └──────────────────────────────┬──────────────────────────────┘
                                                            │
                                                            ▼
                             ┌─────────────────────────────────────────────────────────────┐
                             │                  3. 建模 Agent Teams (规则与边界约束)       │
                             │  - 接收：单个分类意图 + 原文 + 【全局共享变量白名单协议】    │
                             │  - 注入：L1 类型契约 + L3 最佳实践库 (signal_best_practice) │
                             │  - acquire: 标准化参数与 catalog 对齐 (无环境 UUID 硬编码)   │
                             │  - orchestrate: 统一变量命名 (VM, HOST, STORAGE_ID...)      │
                             │  - 异常落库：signal_failure_extraction (UNMODELABLE)       │
                             └──────────────────────────────┬──────────────────────────────┘
                                                            │
                                                            ▼
                             ┌─────────────────────────────────────────────────────────────┐
                             │                  4. 验证 Agent (验证、评审与反馈)           │
                             │  - 全局数量对账 (最终数 + 确认放弃数 == 计数原始数)         │
                             │  - 变量 DAG 拓扑连通性静态分析 (杜绝未声明变量)             │
                             │  - 恒真断言扫描 (拦截 date/uptime+exists 等伪信号)          │
                             │  - 门禁自愈修复回路 (结合 rejected_candidates 错误反馈重试) │
                             │  - 终审输出：写入 kbd_entry.signals_json                    │
                             └─────────────────────────────────────────────────────────────┘
```

---

### 1.1 计数 Agent（业务与实体理解）

#### 职责定义
对 KBD 输入进行业务语义解析，准确识别并切分其中蕴含的全部原子事实、观测现象与排查动作，给出精确的**关键信号数量**与**核心实体证据**。

#### 核心规则与第一性原理约束
1. **纯内容驱动，字段名脱敏**：
   - 提取输入时，仅向模型提供字段内的纯文本正文，严禁传递【告警信息】等字段标签，杜绝模型根据“告警”两字产生先入为主的认知偏差。
2. **复合字段与步骤字段的角色解耦**：
   - 【标题】+【问题描述】+【告警信息】定义为**复合源**：本质是故障发生时的前台外在现象，天然承担“前端生产者信号（QKV）”的职责。经去重后**至少产出 1 个生产者信号**。
   - 【有效排查步骤】定义为**步骤源**：正常情况下每一步对应至少一个“后端消费者信号（QFK）”。若单一步骤中包含多组独立的“执行命令 + 结果断言”（如既查日志又查进程锁），必须按判定目标拆分为多个信号；相邻步骤若针对同一实体目标，则予以合并。
3. **角色感知去重机制（对抗性审查加固）**：
   - *规则要求*：复合源抽取的信号与步骤源抽取的信号需进行重叠去重。
   - *加固原则*：**严禁将现象生产者误当重复项剔除**！去重前必须先执行“现象/探针角色判定”。只有当复合源与步骤源提取的探针目标完全同质（例如两处均描述“去后台查看 task.log 报错”）时，才优先保留排查步骤中的详细探针；若复合源是“任务提示创建虚拟机失败”（产出 `VM` 变量），而步骤源是“后台检查 qemu 进程”，两者属于因果链的前后相继，绝对不得去重！
4. **异常持久化**：
   - 无法明确数量或结构混乱的 KBD 原文，写入 `signal_failure_extraction` 表（`stage='count', reason='UNCOUNTABLE'`），以便后期离线分析。

#### 接口输入输出契约
- **输入**：复合源纯文本 `composite_text`、步骤源纯文本 `steps_text`、`kbd_id`
- **输出格式（JSON）**：
```json
{
  "signal_count": 3,
  "intents": [
    {
      "intent_id": "intent_001",
      "role_type": "producer",
      "core_entity": "创建虚拟机任务报错无法新建镜像文件",
      "evidence_raw": "任务详情：状态失败，行为创建虚拟机，描述无法新建镜像文件",
      "proposed_variables": ["HOST", "VM", "REQUEST_ID"]
    },
    {
      "intent_id": "intent_002",
      "role_type": "consumer",
      "core_entity": "检查主机 /dev 分区使用率是否已满",
      "evidence_raw": "执行 df -h 检查各主机 /dev 分区使用率达到 100%",
      "proposed_variables": ["HOST"]
    },
    {
      "intent_id": "intent_003",
      "role_type": "consumer",
      "core_entity": "检查 vtpdaemon 日志报错",
      "evidence_raw": "查看 /sf/log/today/vt/sfvt_vtpdaemon.log 报错 No space left on device",
      "proposed_variables": ["HOST"]
    }
  ]
}
```

---

### 1.2 分类 Agent（属性与语义抽象）

#### 职责定义
针对计数 Agent 产出的每一个 `Signal Intent`，映射到平台严格封闭的 13 种信号采集器词表，给出高置信度的分类结果与分类证据。

#### 封闭 Catalog 词表规范（修正事实错误：总计 13 种，非 14 种）
- **前端生产者 QKV（5 种）**：
  1. `qkv_task`：前端任务查询（`acli task get`，产出 `HOST`, `VM`, `STATUS`, `ERRCODE_TRACING` 等）
  2. `qkv_alert`：平台异步告警查询（`acli alert get`，产出 `ALERT_TYPE`, `TARGET`, `HOST` 等）
  3. `qkv_dialog`：前端弹框文本复合检索（在 today/today-vt 日志检索弹框，产出 `REQUEST_ID`, `END`）
  4. `qkv_vm_console`：条件型视觉生产者（虚拟机控制台 VNC 截图，产出 `VM_CONSOLE_*`）
  5. `qkv_effect`：条件型效果验证生产者（用于排障恢复后的状态复查，**严禁作为唯一生产者**）
- **后端消费者 QFK（8 种）**：
  1. `qfk_log`：统一日志采集与判定（whitebox/blackbox/pod 日志检查）
  2. `qfk_system`：系统底层命令探针（`lsof`, `ps`, `df`, `lsblk`, `smartctl` 等）
  3. `qfk_vm`：虚拟机领域只读命令（`acli vm ...`）
  4. `qfk_service`：服务状态探测（`asv`, `anet`, `asan`, `host` 服务状态）
  5. `qfk_network`：网络领域探针（`acli network ...`）
  6. `qfk_storage`：存储领域探针（`asan disk list`, 存储池状态）
  7. `qfk_hardware`：硬件固件与传感器探针（IPMI、RAID 卡状态）
  8. `qfk_platform`：平台级集群状态探针
- **特殊分类**：`unclassified`（无法分类，落库复盘）

#### 判定逻辑与双向对抗审查加固
1. **三维立体判定法**：
   - **关键字维**：识别产品硬编码（如明确写有“告警码”、“任务失败”、“弹窗报错”）；
   - **语义动作维**：识别动词意图（如包含“查日志”、“看进程状态”、“检查目录文件”等一律具备 QFK 消费者特征）；
   - **分类基线维**：与数据库 `kb_category` 表的任务/告警名称库进行语义对齐。
2. **双重视角对抗审查（Adversarial Review）**：
   - **视角 A（单意图局部视角）**：仅看单条 `evidence_raw`，独立给出概率最高的分类；
   - **视角 B（全局上下文视角）**：带上完整 KBD 诊断叙事上下文，审查该意图在整个排障链路中所处的因果位置；
   - **对抗裁决机制**：
     - *动词优先律*：凡原文包含具体执行命令或日志文件名者，**强制裁决为 QFK**，全局上下文不得将其降级为 QKV；
     - *任务优先律*：前端现象中，`qkv_task` 与 `qkv_alert` 先验比例为 10:1。若未出现明确系统巡检/容量阈值告警特征，所有因操作引发的界面报错**默认归入 `qkv_task` 或 `qkv_dialog`，禁止泛滥使用 `qkv_alert`**。
3. **异常持久化**：
   - 无法归入上述 13 类的信号，打上 `unclassified` 标签，写入 `signal_failure_extraction` 表（`stage='classify', reason='UNCLASSIFIED'`）。

---

### 1.3 建模 Agent（规则与边界约束）

#### 职责定义
针对已确定工具类型的单条信号意图，结合 KBD 原始事实，注入同类型**最佳实践案例（signal_best_practice）**，生成完全合规、无硬编码、具备健壮判定规则的标准 signals_json 结构。

#### 核心机制加固
1. **分层建模与职责清晰**：
   - **`acquire` 层目标是极致标准化**：
     - 命令必须来自 aCLI Catalog 白名单；
     - `qfk_log.file` 必须是**纯 basename 文件名**（如 `vn-node-agent-api.log`），禁止在文件名中拼接目录或 `<日期>` 等人工占位符；时间范围统一通过 `time_window: "{{END}}"` 或 `{{YMD}}` 约束；
     - 严禁硬编码特定客户环境的存储卷 ID、UUID、IP 地址，强制进行模板变量参数化（如将 `/sf/data/01ec19a8_vs_vol_rep2/...` 替换为 `/sf/data/{{STORAGE_ID}}/...`）。
   - **`orchestrate` 层目标是全局变量契约闭环**：
     - 统一接收调度器下发的**全局共享变量白名单协议**（如 `{"target_vm": "VM", "target_host": "HOST", "target_storage": "STORAGE_ID"}`）；
     - QKV 生产者在 `produces` 中严格输出该协议标准名；QFK 消费者在 `requires` 和 `args` 占位符中严格引用该标准名，消除并行 Agent 间的变量命名漂移。
2. **正向案例驱动（Few-Shot In-Context Injection）**：
   - 建模 Agent 启动时，根据其负责的 `tool_name`，从 `signal_best_practice` 库中实时动态拉取 1~3 个完整度最高的黄金实例注入 Prompt。
3. **数值与阈值类型专项强化**：
   - 当证据涉及字节大小换算（如 2TB 超限）、时间偏差（时钟差）、数值范围时，强制采用 `threshold` Matcher 并补充标准 `ai_processing.derive` 提取换算指令。
4. **异常持久化**：
   - 遇到语法矛盾或无法生成合法参数的信号，写入 `signal_failure_extraction` 表（`stage='modeling', reason='UNMODELABLE'`）。

---

### 1.4 验证 Agent（验证、评审与反馈）

#### 职责定义
以全局裁判视角审查全部已建模信号的集合，执行完整性对账、DAG 连通性分析、合规门禁检测，并驱动自愈修复回路，对最终呈现在 KBD 展示区的信号质量负全责。

#### 核心审查网络与加固对策
1. **完整性刚性对账（解决查“对”查不出“漏”的漏洞）**：
   - 验证 Agent 必须接收计数 Agent 的**原始信号数量**与**被拒绝候选列表（rejected_candidates）**；
   - 设立强制断言：
     $$\text{最终合格信号数} + \text{已确认废弃候选数} == \text{计数 Agent 原始信号数}$$
     若数量不平，直接打回建模层重审，严禁静默发布。
2. **全局变量 DAG 拓扑连通性校验**：
   - 运行拓扑排序算法，扫描所有 QFK 信号中的 `requires` 集合，必须完全是前期 QKV 信号 `produces` 集合与系统内置环境变量（`DEFAULT_VARIABLE_SCHEMA`）的子集；
   - 拦截循环依赖与悬空依赖。
3. **恒真断言与防伪扫描**：
   - 静态扫描所有 `qfk_system` 命令，对于 `date`, `uptime`, `hostname`, `id` 等系统级必然成功输出的命令，**严禁使用 `exists: true` 作为断言**，强制要求配置具体状态行提取或具体关键字匹配。
4. **门禁反馈与智能自愈回路（Self-Healing Loop）**：
   - 验证 Agent 调用底层 `review_signal_document` 与 Shared Resolution Runtime 运行时审查；
   - 若出现阻断（如 `acquire.args` 缺少参数或日志路径包含非标准占位符），验证 Agent 解析 `issue.message` 与 `rejected_candidates.reason`，自动发起 **1 轮针对性 Patch 修复**；自愈成功后方予放行。

---

## 二、多 Agent 提示词管理与热加载机制规范

为保持与当前系统平台架构的一致性与高内聚性，多 Agent 提示词全部纳入现有的 `system_prompt` 表统一管控，统一由 `StrictPromptLoader` 提供防呆加载、参数契约校验与在线热加载能力。

### 2.1 Prompt 体系注册元数据规范

| Agent 阶段 | Prompt 注册名称 | Stage | Version | 核心输入占位符契约 (`expected_placeholders`) | 职责说明 |
| :--- | :--- | :---: | :---: | :--- | :--- |
| **计数 Agent** | `kbd_signal_count_v1` | `KEY` | `1.0` | `composite_text`, `steps_text` | 实体切分、角色解耦、数量提取与去重 |
| **分类 Agent** | `kbd_signal_classify_v1` | `KEY` | `1.0` | `core_entity`, `evidence_raw`, `composite_text`, `steps_text`, `acquirer_catalog`, `category_baseline` | 双视角对抗分类、13 类 Catalog 映射 |
| **建模 Agent** | `kbd_signal_model_v1` | `KEY` | `1.0` | `tool_name`, `core_entity`, `evidence_raw`, `shared_variables`, `best_practices`, `acli_catalog` | JSON 参数化建模、变量契约注入 |
| **验证 Agent** | `kbd_signal_verify_v1` | `KEY` | `1.0` | `signals_json`, `rejected_candidates`, `raw_count`, `kbd_context`, `gate_issues` | 全局对账、DAG 检查、门禁错误智能自愈 |

### 2.2 StrictPromptLoader 契约集成与热加载流程

```python
# 示例：kb-service 中多 Agent 提示词的热加载加载范式
async with db_manager.async_session_factory() as session:
    # 1. 计数 Agent Prompt 加载与占位符防呆校验
    count_template = await StrictPromptLoader.load_and_validate(
        session,
        "kbd_signal_count_v1",
        ["composite_text", "steps_text"],
        consumer="kb-service.signal_extract.count",
    )
    
    # 2. 分类 Agent Prompt 加载与占位符防呆校验
    classify_template = await StrictPromptLoader.load_and_validate(
        session,
        "kbd_signal_classify_v1",
        ["core_entity", "evidence_raw", "composite_text", "steps_text", "acquirer_catalog", "category_baseline"],
        consumer="kb-service.signal_extract.classify",
    )
    
    # 3. 建模 Agent Prompt 加载与占位符防呆校验
    model_template = await StrictPromptLoader.load_and_validate(
        session,
        "kbd_signal_model_v1",
        ["tool_name", "core_entity", "evidence_raw", "shared_variables", "best_practices", "acli_catalog"],
        consumer="kb-service.signal_extract.model",
    )
    
    # 4. 验证 Agent Prompt 加载与占位符防呆校验
    verify_template = await StrictPromptLoader.load_and_validate(
        session,
        "kbd_signal_verify_v1",
        ["signals_json", "rejected_candidates", "raw_count", "kbd_context", "gate_issues"],
        consumer="kb-service.signal_extract.verify",
    )
```

**热更新与版本治理优势**：
- 运维与算法工程师在 `Admin UI -> Prompt 管理` 页面修改任一 Agent 提示词后，由于 `load_and_validate` 在每个抽取任务事务中实时动态读取 `system_prompt` 视图，修改**即时生效，无需重启任何 Pod 或服务**；
- `StrictPromptLoader` 在读取时会强制校验模板内部的双花括号占位符，若管理员误删除了关键变量（如 `{shared_variables}`），系统会在加载时主动报错拦截，防止坏 Prompt 污染生产抽取流水线。

---

## 三、建模模板库与最佳实践库（数据模型与资产沉淀）

### 3.1 数据库表结构设计（DDL 规范）

按用户要求，数据模型正式命名为 `signal_modeling_template`、`signal_best_practice` 和 `signal_failure_extraction`：

```sql
-- 1. 信号类型建模标准模板库 (Schema & Constraint Definition)
CREATE TABLE IF NOT EXISTS signal_modeling_template (
    id SERIAL PRIMARY KEY,
    tool_name VARCHAR(32) NOT NULL UNIQUE,       -- 工具名称 (如 qkv_task, qfk_log, qfk_system)
    category VARCHAR(16) NOT NULL,               -- frontend (生产者) / backend (消费者)
    description TEXT NOT NULL,                   -- 语义职责与场景说明
    acquire_schema JSONB NOT NULL,               -- acquire.args 标准 JSON Schema 定义
    allowed_matcher_types VARCHAR(32)[] NOT NULL,-- 允许的 matcher 类型列表
    variable_protocol JSONB NOT NULL,            -- 支持产出/消费的标准变量列表
    anti_patterns TEXT[] NOT NULL DEFAULT '{}',  -- 明确禁止的写法与反例约束
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
COMMENT ON TABLE signal_modeling_template IS '信号类型建模标准模板库：定义 13 类信号的输入 Schema 与参数契约';

-- 2. 信号建模最佳实践库 (Golden Few-Shot Dataset)
CREATE TABLE IF NOT EXISTS signal_best_practice (
    id SERIAL PRIMARY KEY,
    template_id INT REFERENCES signal_modeling_template(id) ON DELETE CASCADE,
    tool_name VARCHAR(32) NOT NULL,              -- 工具名称 (冗余提升检索效率)
    pattern_category VARCHAR(64) NOT NULL,       -- 场景模式子类 (如 "任务失败超时", "日志阈值换算", "进程D状态卡死")
    source_kbd_id BIGINT REFERENCES kbd_entry(id) ON DELETE SET NULL, -- 溯源的已发布 KBD 实体 ID
    support_id VARCHAR(32),                      -- 案例 Support ID (如 18906, 39233)
    raw_evidence TEXT NOT NULL,                  -- KBD 原始文本切片证据
    signal_json JSONB NOT NULL,                  -- 经过专家验证的标准 signals_json 完整片段
    design_notes TEXT NOT NULL,                  -- 专家设计要点与避坑说明
    completeness_score INT DEFAULT 10,           -- 规范与完整度评分 (0-10)
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_signal_best_practice_tool ON signal_best_practice(tool_name) WHERE is_active = TRUE;
COMMENT ON TABLE signal_best_practice IS '信号建模最佳实践库：沉淀已发布 KBD 中专家最终审核通过的黄金实例';

-- 3. 信号抽取异常复盘日志表 (Failure Feedback Loop)
CREATE TABLE IF NOT EXISTS signal_failure_extraction (
    id BIGSERIAL PRIMARY KEY,
    kbd_id BIGINT REFERENCES kbd_entry(id) ON DELETE CASCADE,
    stage VARCHAR(32) NOT NULL,                  -- 失败发生阶段：count / classify / modeling / verification
    raw_content TEXT NOT NULL,                   -- 失败时的原始输入内容
    reason VARCHAR(64) NOT NULL,                 -- 结构化失败原因码 (UNCOUNTABLE, UNCLASSIFIED, UNMODELABLE, VERIFY_FAILED)
    detail_payload JSONB DEFAULT '{}'::jsonb,    -- 详细上下文、报错堆栈或未通过的 Candidate 结构
    resolved BOOLEAN DEFAULT FALSE,              -- 是否已完成人工复盘标记
    resolved_by VARCHAR(64),                     -- 复盘专家 ID
    resolved_notes TEXT,                         -- 复盘纠偏说明
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_signal_failure_stage ON signal_failure_extraction(stage, resolved);
COMMENT ON TABLE signal_failure_extraction IS '信号抽取异常复盘日志表：沉淀计数、分类、建模、验证各阶段未通过的异常案例';
```

---

### 3.2 黄金最佳实践库（Seed 资产精选）

从 Staging 环境 23 个已发布 KBD 的 67 个专家修订信号中，提炼出覆盖高频核心场景的种子最佳实践（写入 `signal_best_practice`）：

#### 实践 1：`qkv_task` 标准任务生产者（来源：Case 18906）
```json
{
  "tool_name": "qkv_task",
  "pattern_category": "前台任务失败捕获与变量全量产出",
  "support_id": "18906",
  "raw_evidence": "导入三方的qcow2镜像，提示新建虚拟机失败，虚拟机磁盘镜像损坏或未知的磁盘镜像",
  "signal_json": {
    "id": "sig_001",
    "role": "must",
    "acquire": {
      "tool": "qkv_task",
      "args": {
        "keyword": "新建虚拟机",
        "is_failed": true,
        "limit": 1,
        "timeout": 60,
        "instruction": "查看新建虚拟机任务详情确认失败报错信息"
      }
    },
    "match": null,
    "orchestrate": {
      "phase": "diagnostic",
      "produces": [
        { "name": "HOST", "path": "host" },
        { "name": "VM", "path": "vm" },
        { "name": "REQUEST_ID", "path": "request_id" },
        { "name": "STATUS", "path": "status" }
      ],
      "requires": []
    },
    "provenance": { "category": "frontend", "confidence": 0.95 }
  },
  "design_notes": "任务类关键字必须逐字来自失败描述；produces 产出基础环境变量供给下游消费者。"
}
```

#### 实践 2：`qfk_log` 标准组件日志探针（来源：Case 39233）
```json
{
  "tool_name": "qfk_log",
  "pattern_category": "精准组件日志多词与时间窗口判定",
  "support_id": "39233",
  "raw_evidence": "检查 vn-node-agent-api 日志是否存在文件句柄泄露错误 Too many open files",
  "signal_json": {
    "id": "sig_002",
    "role": "must",
    "acquire": {
      "tool": "qfk_log",
      "args": {
        "file": "vn-node-agent-api.log",
        "path": "/sf/log",
        "host": "{{HOST}}",
        "time_window": "{{YMD}}",
        "timeout": 60,
        "instruction": "检查 vn-node-agent-api 日志是否存在文件句柄泄露错误"
      }
    },
    "match": {
      "type": "keyword",
      "mode": "and",
      "pattern": ["Too many open files", "vn-node-agent-api"],
      "expected": true,
      "extract": {
        "type": "text",
        "source": "stdout",
        "cardinality": "all",
        "rows": { "mode": "keywords", "include": ["Too many open files"], "case_sensitive": true }
      }
    },
    "orchestrate": { "phase": "diagnostic", "produces": [], "requires": ["HOST"] },
    "provenance": { "category": "backend", "confidence": 0.9 }
  },
  "design_notes": "file 只传纯 basename 文件名，严禁包含目录；时间维度使用 {{YMD}} 宏而不是硬编码日期目录。"
}
```

#### 实践 3：`threshold` 数值提取与阈值计算判定（来源：Case 36203，填补 0 覆盖空白）
```json
{
  "tool_name": "qfk_log",
  "pattern_category": "日志大容量字节数值提取换算与大于比较",
  "support_id": "36203",
  "raw_evidence": "qemu-img-real: error while writing at byte 2276332666880 超过 2TB 限制",
  "signal_json": {
    "id": "sig_003",
    "role": "must",
    "acquire": {
      "tool": "qfk_log",
      "args": {
        "file": "sfvt_vtpdaemon.log",
        "timeout": 60,
        "instruction": "获取日志中的字节大小是否超过 2T 大小限制"
      }
    },
    "match": {
      "type": "threshold",
      "operator": ">",
      "value": 2.0,
      "expected": true,
      "aggregation": "max",
      "extract": {
        "type": "text",
        "source": "stdout",
        "value_mode": "string",
        "cardinality": "all",
        "rows": { "mode": "keywords", "include": ["qemu-img-real: error while writing at byte"], "case_sensitive": false },
        "ai_processing": {
          "mode": "derive",
          "instruction": "提取紧跟在英文单词'byte '后面的纯数字，将其换算为TB单位(除以 1024 的 4 次方)。直接输出转换后的浮点数字符串，保留两位小数(如 2.07)。",
          "output_type": "number"
        }
      }
    },
    "orchestrate": { "phase": "diagnostic", "produces": [], "requires": [] },
    "provenance": { "category": "backend", "confidence": 0.88 }
  },
  "design_notes": "深层数量故障标准建模：extract.rows 粗筛 + ai_processing.derive 提取换算 + threshold 阈值判定。"
}
```

#### 实践 4：`qfk_system` 系统结构化表格解析与模板变量参数化（来源：Case 41671, 17002）
```json
{
  "tool_name": "qfk_system",
  "pattern_category": "系统命令whitespace_table表格解析与存储卷占位符引用",
  "support_id": "41671",
  "raw_evidence": "查看进程状态，确认是否有D状态进程及长时间运行的qemu-img resize进程",
  "signal_json": {
    "id": "sig_004",
    "role": "must",
    "acquire": {
      "tool": "qfk_system",
      "args": {
        "command": "ps",
        "command_args": ["auxf"],
        "host": "{{HOST}}",
        "timeout": 60,
        "instruction": "查看进程状态确认是否有D状态进程"
      }
    },
    "match": {
      "type": "keyword",
      "pattern": [" D "],
      "expected": true,
      "extract": {
        "type": "text",
        "source": "stdout",
        "parser": "whitespace_table",
        "cardinality": "exactly_one",
        "value_key": "STAT",
        "value_mode": "string",
        "columns": [{ "key": "STAT", "selector": { "by": "index", "index": 8 }, "value_mode": "string" }],
        "rows": { "mode": "keywords", "include": ["qemu-img-real", "{{VM}}"], "case_sensitive": true }
      }
    },
    "orchestrate": { "phase": "diagnostic", "produces": [], "requires": ["HOST", "VM"] },
    "provenance": { "category": "backend", "confidence": 0.92 }
  },
  "design_notes": "命令参数强制使用 {{VM}} 占位符避免环境耦合；使用 whitespace_table 精准摘取状态列。"
}
```

---

## 四、细化工程实施线路与阶段验收标准

工程改造划分为四个严密衔接的里程碑阶段：

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│  阶段一：数据模型升级与最佳实践冷启动 (Milestone 1)                                   │
│  - 执行 DDL 创建 3 张核心资产表                                                        │
│  - 清洗并导入已发布 23 个 KBD 的首批 30+ 黄金实践                                     │
│  - 验收：数据表就绪、查询 API 就绪、种子数据完整度 100%                               │
└───────────────────────────────────────────┬────────────────────────────────────────────┘
                                            │
                                            ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│  阶段二：Prompt 治理与热加载基础设施建设 (Milestone 2)                                 │
│  - 编写 4 个 Agent 的标准系统提示词并在 system_prompt 注册                             │
│  - 扩展 StrictPromptLoader 支持多槽位防呆校验                                          │
│  - 验收：Prompt 单元测试 100% 通过、在线修改热加载时延 < 1 秒                          │
└───────────────────────────────────────────┬────────────────────────────────────────────┘
                                            │
                                            ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│  阶段三：多 Agent 编排调度器与自愈门禁开发 (Milestone 3)                               │
│  - 研发 SignalExtractionOrchestrator 状态机流水线                                      │
│  - 落地计数角色感知、分类双视角对抗、建模实例注入、验证 DAG 与自愈回路                  │
│  - 验收：单 KBD 端到端执行通过，门禁被拒率下降 60%                                     │
└───────────────────────────────────────────┬────────────────────────────────────────────┘
                                            │
                                            ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│  阶段四：23 个已发布 KBD 全量回归回测与上线运营 (Milestone 4)                          │
│  - 对 23 个已发布 KBD 进行新旧架构 Shadow 对比回测                                     │
│  - Admin UI 审核区接入 signal_failure_extraction 复盘看板                              │
│  - 验收：直接可用率 ≥ 50%、漏抽率 ≤ 15%、零伪信号上线                                 │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

---

### 阶段一：数据模型升级与最佳实践冷启动

#### 具体任务清单
1. **任务 1.1（Atlas 迁移脚本编写）**：创建 `database/atlas-migrations/` 数据库迁移文件，定义 `signal_modeling_template`、`signal_best_practice`、`signal_failure_extraction` 三张表及索引结构。
2. **任务 1.2（种子数据抽取脚本）**：编写 `scripts/seed_signal_best_practices.py`，遍历 staging 数据库中 23 个已发布 KBD 的 `kbd_revision` 终稿，自动解析提取符合规范的黄金信号，写入 `signal_best_practice` 表。
3. **任务 1.3（DAO 与仓储层集成）**：在 `backend/kb-service/app/services/` 中实现模板与实践库的数据查询缓存（内存 TTL 60 秒，支持快速检索）。

#### 验收标准
- [ ] 数据库迁移执行成功，无任何 SQL 语法错误；
- [ ] `signal_modeling_template` 完整登记 13 类工具的标准 Schema；
- [ ] `signal_best_practice` 包含不少于 30 个有效种子实例，完整覆盖已发布 KBD 中的 7 类活跃工具；
- [ ] 单元测试验证 `BestPracticeService.get_examples_by_tool("qfk_log")` 返回耗时 < 5ms。

---

### 阶段二：Prompt 治理与热加载基础设施建设

#### 具体任务清单
1. **任务 2.1（Prompt 模板研磨）**：
   - 编写 `kbd_signal_count_v1`（重点加入角色感知和复合源/步骤源去重规则）；
   - 编写 `kbd_signal_classify_v1`（重点加入双视角对抗审查指导语及 Catalog 白名单）；
   - 编写 `kbd_signal_model_v1`（重点加入 L1 类型契约与变量命名契约）；
   - 编写 `kbd_signal_verify_v1`（重点加入 DAG 完整性对账和门禁错误自愈指令）。
2. **任务 2.2（初始化数据入库）**：编写迁移 SQL 将上述 4 个 Prompt 插入 `system_prompt` 表，设置 `stage='KEY', is_active=true`。
3. **任务 2.3（StrictPromptLoader 拓展测试）**：增加 `test_multi_agent_prompt_contracts.py` 自动化测试，验证 4 个 Prompt 的占位符契约严格匹配。

#### 验收标准
- [ ] `system_prompt` 表中存在对应 4 个激活状态的 Agent 提示词；
- [ ] `StrictPromptLoader.load_and_validate()` 测试用例通过率 100%；
- [ ] 在数据库中模拟更新任一 Prompt 文本，服务内加载结果在 1 秒内实时刷新，无需重启服务。

---

### 阶段三：多 Agent 编排调度器与自愈门禁开发

#### 具体任务清单
1. **任务 3.1（核心流水线编排开发）**：在 `backend/kb-service/app/services/signal_orchestrator.py` 实现 `SignalExtractionOrchestrator`：
   - 第一步：调用计数 Agent 获取 `SignalIntents`；若失败记录 `signal_failure_extraction`；
   - 第二步：异步并发调用分类 Agent（单信号 vs 全局上下文双向审查）；
   - 第三步：注入变量契约与最佳实践，异步并发调用建模 Agent；
   - 第四步：汇总送入验证 Agent，执行 DAG 拓扑分析与门禁自愈。
2. **任务 3.2（路由接口替换）**：重构 `backend/kb-service/app/routes/extract_signals.py` 中的 `extract_signals_for_kbd`，平滑替换旧版单体 `_call_llm` 调用。
3. **任务 3.3（自愈闭环实现）**：验证 Agent 拦截到 `rejected_candidates` 后，自动生成差分修复 Prompt 进行最多 1 轮自动补正。

#### 验收标准
- [ ] 针对异常输入能够准确向 `signal_failure_extraction` 表插入记录，不中断主流程；
- [ ] 门禁拦截率（因路径错误、格式错误导致的候选直接丢失率）较旧架构降低 60% 以上；
- [ ] 全链路端到端集成测试 `test_signal_extraction_pipeline.py` 全部 passed。

---

### 阶段四：23 个已发布 KBD 全量回归回测与上线运营

#### 具体任务清单
1. **任务 4.1（Shadow 回归测试）**：使用新多 Agent 流程对 Staging 环境 23 个已发布 KBD 执行重抽仿真，记录比对新提案与旧提案的各项指标。
2. **任务 4.2（重点问题案例回归）**：
   - 重点回归 Case 38744（旧版产出为 0）：验证新流程能否召回 ≥ 2 个信号；
   - 重点回归 Case 36203（超 2TB 限制）：验证新流程能否自主建立 `threshold` 信号；
   - 重点回归 Case 41398（date 伪信号）：验证新流程能否彻底杜绝 `date + exists` 恒真信号。
3. **任务 4.3（Admin UI 复盘工作台）**：在前端“KBD 审查”与“知识库维护”页面增加“抽取异常复盘”面板，展示 `signal_failure_extraction` 中的未决记录，支持专家一键采纳并沉淀为新的最佳实践。

#### 验收标准
| 核心指标 | 旧架构基线（实测） | 新架构验收底线标准 | 验证方式 |
| :--- | :---: | :---: | :--- |
| **直接可用率（无需专家改动）** | 27.3% (12/44) | **≥ 50%** | 对比新提案与专家最终发布版 Diff |
| **信号漏抽率（需专家新增比例）** | 37.3% (25/67) | **≤ 15%** | 统计新提案对专家发布信号的召回率 |
| **`threshold` 判定类型覆盖数** | 0 个 (0%) | **≥ 4 个 (50%+)** | 统计新提案中 threshold 信号产出数 |
| **必然恒真的伪信号数** | 存在 (Case 41398) | **0 个 (完全归零)** | 静态扫描 `qfk_system + exists` |
| **重跑稳定性（Case 32563）** | 4 次重抽信号单调递减 (4→1) | **连续 3 次重抽数量恒定** | 对 Case 32563 连续执行 3 次抽取对账 |

---

## 五、总结与价值闭环

本方案通过**多 Agent 关注点分离**攻克了单体认知过载难题，通过**严格的变量契约与类型模板**杜绝了参数失真与环境硬编码，通过**自愈验证回路**扭转了门禁“只拦不修导致信号丢失”的被动局面，并通过**最佳实践库与异常复盘表**建立起持续学习的飞轮体系。这套架构不仅适用于当前 23 个已发布 KBD 的全量质量回升，更为后续平台数千篇存量 KBD 的自动化工程级信号抽取提供了坚如磐石的技术底座。
> 实现审查补充：计数、分类、建模、验收均为独立决策；生产失败复盘使用独立事务和 trace_id，离线回归必须使用只读数据库事务、禁用失败写入，并将结果写入临时文件，不得改写 KBD/Revision/Batch。

## 六、PR999 对抗性审查与 Shadow 验证结论（2026-09-04）

按第一性原理将成功拆为“数量正确、分类正确、建模可执行、整体门禁通过、与专家 Gold Label 一致”五个独立条件；任何一个条件失败都不能计为抽取成功。评估脚本必须使用当前 PR 的候选 Prompt，而不是 staging 已部署旧 Prompt，并分别输出管线状态与专家一致性。

当前 PR Prompt + PR 代码对 23 个已发布 KBD 的只读重跑结果：完整通过管线 1/23，与专家完全一致 0/23；专家 67 条信号、新模式通过门禁 24 条，工具类型匹配 13 条、严格 key 匹配 7 条，微平均 Precision 29.17%、Recall 10.45%、F1 15.38%。主要阻断为计数 Agent 未产出可追溯意图 14 次、Matcher/Schema 不兼容、非法参数或命令、日志证据不可追溯。该结果未达到阶段四验收底线，PR 在完成 Prompt/模板继续收敛和同一 Gold 集重复验证前不得宣称优化效果达标。

因此 `ENABLE_MULTI_AGENT_EXTRACTION` 默认关闭；当前只能以不写库的 Shadow 模式运行。只有达到阶段四验收线并完成人工抽样复核后，才允许在 staging 显式开启，再按灰度比例进入生产。

staging `tool_definition` 当前登记 QKV 5 个、QFK 9 个；第 9 个 `qfk_var` 仅描述变量处理能力，但仓库没有对应 acquire 参数 Schema、Signal Schema 与 Resolution Runtime，其职责已由 `orchestrate.output_processing` 承担。因此可抽取信号 Catalog 仍为 QKV 5 + QFK 8。上线前应将 `qfk_var` 从信号分类口径中剥离并标为内部处理原语，或先完整补齐三层运行时契约；禁止只加入分类白名单。

Shadow 评估前后正式库计数均保持：`kbd_entry=2788`、`kbd_revision=8732`、`kbd_batch_job=19`、`signal_failure_extraction=0`，确认测试数据未入库。

## 七、候选发现与局部失败隔离（2026-09-04）

在上述复测基础上补充确定性候选发现层：从复合字段提取故障短语，从每个编号排查步骤提取一个动作/命令/日志证据候选；候选只携带原文证据，不直接决定工具或 JSON。LLM 计数结果为空或局部 malformed 时，仍使用规则候选继续分类和建模，并在 `last_diagnostics` 中记录发现、计数、分类、建模、验证各阶段数量。

验证 Agent 的自愈结果必须绑定原候选 `id` 且满足数量守恒，禁止凭空新增信号。评估脚本输出阶段汇总指标，避免把建模失败误报为业务信号未发现。

对抗性复测注意事项：staging 原默认 `deepseek-v4-flash` 已被模型服务拒绝，切换 `glm-5` 后才可调用；模型不可用时只能报告规则候选和配置阻断，不能将该次结果当作准确率。多 Agent 仍保持 Shadow 默认关闭，所有复测使用只读事务和本地 JSON 输出。

本次不依赖模型的 23 条候选发现复测得到 136 个候选、Gold Label 共 67 条，22/23 条 KBD 的候选数量不少于 Gold Label；这只能证明发现层覆盖能力较高，不能证明信号准确率，因为候选尚未完成分类、建模和门禁。候选与 Gold 的数量比约 2.03，说明后续必须继续做主题聚合和消费者步骤边界收敛。

## 八、PR999 合并后问题修复（2026-09-04）

针对合并版本审查发现的问题，本轮补齐以下边界：rejected-only 的多 Agent 结果进入传统链路兜底，避免空信号直接写回；图片诊断证据追加进入多 Agent 复合上下文；候选使用 `kbd_<id>_candidate_<n>` 身份贯穿分类、建模和验证，模型生成的随机 id 不再作为身份依据；建模阶段强制要求 `provenance.evidence` 包含候选原文，阻断 Few-Shot 外部证据污染；评估和失败复盘迁移补齐可配置入口及 `trace_id`。

## 九、DAG 变量符号表穿透与 Few-Shot 样本污染自愈闭环（PR1000 最终加固）

1. **DAG 变量拓扑偏序建模**：
   - 彻底打破原有候选全盲并发建模模式；
   - 第一轮优先建模 Producer 候选（QKV / producer），并动态汇聚提取 `orchestrate.produces` 中声明的变量集合（如 `VM`, `HOST`, `DISK_ID`）；
   - 第二轮将生成的动态变量符号表作为只读上下文，注入 Consumer 候选（QFK / consumer）的建模阶段，使消费者在生成参数与 `requires` 时天然闭环，彻底根治跨候选变量断裂。
2. **Few-Shot 源头防污染与证据自愈纠偏**：
   - 在最佳实践示例注入时添加明确的范式参考注释，禁止模型抄袭示例中的特定日志路径与私有变量名；
   - 优化证据校验机制：对于轻微改写自动纠偏回填候选原文 `evidence_raw`，避免由于标点或空白微小差异导致建模失败；
   - 自动清洗 `orchestrate.requires` 中未闭合且未被引用的悬空非法变量（如模型臆造的 `VM_DISK_PATH`），确保 DAG 门禁一次性绿灯。
