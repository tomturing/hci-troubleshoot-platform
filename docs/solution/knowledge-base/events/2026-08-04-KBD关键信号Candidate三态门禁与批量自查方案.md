---
status: active
category: solution
audience: developer
last_updated: 2026-08-04
owner: team
---

# KBD 关键信号 Candidate 三态门禁与批量自查方案

## 背景与需求

PR #668 的安全目标是“写操作不能成为 KBD Signal”，但 Prompt 将其实现为“模型不要输出包含启停等动作的候选”。KBD30880 连续重抽不再出现 `qkv_task`，数据库又没有对应 Rejected Candidate，证明候选在 LLM 阶段已消失，而不是被 Python 门禁拒绝。

首批 5 篇进一步证明“Schema 合法”“catalog 已登记”“真实可运行”是三个不同事实。方案必须让错误可见、可归因、可供专家补 catalog 或修正 Candidate，同时保持系统简单。

## 方案（WHAT）

### 1. 唯一状态模型

```text
LLM 识别
  │
  ▼
Candidate（全部候选）
  │
  ├─ write_signal ──► Rejected Candidate
  ├─ not_exists  ───► Rejected Candidate
  ├─ run_failed  ───► Rejected Candidate
  └─ 全部门禁通过 ─► Signal
```

不增加“待定 Signal”“Capability Signal”“自动修复结果”等额外状态。`Rejected Candidate` 的 `reason_code` 表达工程分类，`reason` 保存具体子原因。

### 2. LLM 边界

Prompt v2.1 输出 Candidate，但传输层保留历史 key `signals`，避免 Prompt 数据迁移与服务滚动发布必须原子切换：

```json
{
  "schema_version": 2,
  "signals": []
}
```

这里的 `signals[]` 在服务端门禁前全部视为 Candidate，不代表已经成为领域 Signal。后端同时兼容未来的 `candidates[]`，但当前 Prompt 不改 wire key。LLM 获得以下知识：

- 采集器及 args 契约；
- QKV 是前台历史事实查询，QFK 是后台采集/判定；
- 当前代码随附 aCLI catalog 命令列表；
- Matcher、声明式 Extract 和变量依赖基本规则。
- 明确 HCI 平台告警的召回契约：每个不同告警至少一个 qkv_alert Candidate，不能因已有后台检查而省略；BMC/iBMC 外部事件除外。

知识只用于提高映射质量。若正文已有明确候选但属于写动作、catalog 缺失或表达不完整，LLM 仍输出 Candidate 并诚实标注 `phase=solution`/`needs_review`，不能自行删除。

### 3. 服务端门禁

固定顺序及原因如下：

1. `write_signal`
   - `orchestrate.phase=solution`；或
   - `qfk_*` Candidate 的实际执行向量命中明确写动作：同时检查 `command`、完整的写子命令/开关，以及被包装执行程序的 basename；例如 `soft_raid_lit --off` 与 `strace ... /sf/bin/sfscp` 均先归入 `write_signal`，不能被后续 catalog 结果掩盖。
   - 必须先判，避免写操作因 args/Matcher 错误被误标成普通运行失败。
   - phase 描述 Candidate 自身执行语义；仅在 LLM Candidate 抽取入口，对封闭词表可证明的只读命令，若模型因“修复后验证”错标 solution，先归一为 diagnostic，再继续 catalog/运行门禁。专家保存、发布与 Agent 运行保持严格模式，任何残留 solution Signal 均拒绝。
2. `not_exists`
   - Candidate 的 tool/args 已足以按运行时同形规则编译；
   - 编译出的 `acli <namespace> <command>` 不在当前代码随附 catalog。
3. `run_failed`
   - 非对象、缺少 acquire、未知 tool、args Schema 失败；
   - 安全管道转换、变量依赖、未消费 producer、Matcher 质量、Signal v2 Schema 失败；
   - `keyword` Matcher 的每一个 pattern 无法从 Candidate 的逐字 evidence 或合法变量追溯，尤其是把脱敏 IP/ID 降级成 `address`、`ip`、`error` 等宽泛词，或在一个有证据项旁混入模型猜测项；
   - catalog 已登记，但编译调用缺少命令自身必需的最小参数；例如裸 `acli system smartctl` 只会打印 usage/失败，不能冒充可执行采集；
   - 采集器与 Candidate evidence 不一致：BMC/iBMC 外部事件伪装成 HCI `qkv_alert`，或 `qfk_log` 没有日志文件/路径且 evidence 也不具备日志形态；
   - `.conf/.cfg/.ini/.json/.yaml` 配置文件被伪装成 qfk_log；明确 `/sf/cfg` 读取可确定性归一为只读 cat，其他不明配置路径进入 `run_failed`；
   - 命令固有能力与目标事实不一致，或 regex 连 Candidate 自己的逐字 evidence 都无法命中；前者如用 `ipmitool mc info` 采集 RAID 适配器固件；
   - 后续编译/预运行和真实执行失败也使用相同分类，并保留更具体失败模式。
4. 通过后执行既有 enrich、Verification Contract 投影并保存为 Signal。

`qkv_task` 不参与写命令扫描；其 keyword 中的动作词只是历史任务检索条件。

### 4. 持久化与兼容

`signals_json` 仍是单一 JSONB 文档，不新增表列：

```json
{
  "schema_version": 2,
  "signals": [],
  "rejected_candidates": [
    {
      "candidate": {},
      "reason_code": "write_signal|not_exists|run_failed",
      "reason": "人类可读的具体原因"
    }
  ]
}
```

`reason_code` 对新数据写入，对历史快照保持可选。重新抽取继续产生不可变 Proposal revision，不改写历史 Proposal/Expert。

Prompt 数据迁移采用“收敛”而不是“只追加新规则”：存量库会依次经历 015～020，旧 Prompt 中可能同时存在“不得输出”“宁缺毋滥”“不产出信号”和新版“完整输出 Candidate”。migration 021 必须替换这些已知反向指令，并在结束时断言它们已经消失；仅把版本号改成 2.1 或在末尾追加规则 25 不算升级成功。新装环境的 seed 与存量迁移后的 Prompt 必须表达同一三态语义。

### 5. 专家界面

- Signal 区域保持现有查看、编辑和完整命令预览。
- Rejected Candidate 显示稳定中文标签：变更动作、命令未实现、验证/执行失败。
- `write_signal` 和 `run_failed` 明确“必须处理”，`not_exists` 明确“需确认 catalog 缺口或模型错误”。
- 完整 Candidate JSON 与具体 reason 始终可展开；专家可恢复后编辑，但 KBD 保存门禁仍禁止真正写操作进入 Signal。

### 6. 批量闭环

每批 5 篇执行同一流程：

```text
最新代码/Prompt 重抽 5 篇
  → 审查 Candidate、Signal、Rejected Candidate
  → 只修共性规则
  → 自动测试 + 同批重抽
  → 5 篇全部闭环
  → 独立本地提交
  → 下一批
```

## 决策依据（WHY）

### 第一性原理

1. 模型的职责是召回和结构化证据；工程门禁的职责是决定能否安全执行。两者不能合并，否则被过滤的候选没有审计证据。
2. 专家能改善 Prompt、catalog 和执行器的前提，是系统保留失败输入及精确失败原因。只保留“拒绝数量”没有学习价值。
3. `catalog contains(command)` 只回答“代码当前登记了吗”，不回答“参数适用于该版本/节点吗”“命令能成功吗”“输出能被 Matcher 正确判定吗”。因此 `not_exists` 与 `run_failed` 必须分开。
4. KBD Signal 的领域定义是可重复的只读事实采集、判定或变量生产。写候选具有审计价值，但不具有直接执行资格。

### 对抗性审查

| 攻击/反例 | 失败模式 | 方案防护 |
|---|---|---|
| “启动虚拟机失败”包含“启动” | 按词过滤会删除只读 `qkv_task` | 写门禁只检查真实 QFK 执行语义，QKV keyword 不参与 |
| 模型造出 `acli hardware mc info` | 模型侧过滤后专家看不到能力缺口 | Candidate 保留，服务端标 `not_exists` |
| catalog 本身不完整 | 将所有缺失命令当模型错误会阻碍 catalog 演进 | `not_exists` 明确要求专家区分真实缺口与乱造 |
| 命令在 catalog 但参数/环境不兼容 | 误认为 catalog 命中即可运行 | Schema/编译/预运行/真实运行失败统一 `run_failed` |
| 写命令同时有非法 Matcher | 若先做 Matcher 校验，会掩盖安全风险 | `write_signal` 优先 |
| “重启后执行 lspci 验证”被标 solution | 把上下文中的历史变更误当 Candidate 自身写操作 | 封闭只读命令证明后归一 diagnostic，继续进入 `not_exists/run_failed/Signal` |
| 同批一个坏候选 | 整批失败会吞掉正常事实 | 逐 Candidate 分流，正常项独立成为 Signal |
| 所有通过门禁的 Signal 都被 LLM 标为 context | verification_contract 没有 must，持久化 500，整篇 Candidate/Rejected 审计丢失 | 确定性提升第一条 diagnostic Signal 为 must；无 Signal 时不生成 Contract |
| 历史 revision 没有新字段 | 强制 required 会破坏不可变历史读取 | `reason_code` 可选兼容，新增数据稳定写入 |
| 自动修复 Candidate | 修改原意后无法审计模型真实输出 | 仅允许封闭的确定性规范化；拒绝项保留完整 Candidate |
| Prompt 先于服务滚动升级 | 若改成新 JSON key，旧服务会保存空 Proposal | wire key 继续使用 `signals`；新服务把它解释为 Candidate 并兼容 `candidates` |
| 新规则只追加在旧 Prompt 末尾 | 模型同时收到“完整输出”和“不得输出”，行为取决于冲突消解 | migration 021 替换全部已知反向语句，并以负向断言阻止假升级 |
| 脱敏值无法直接运行 | 模型把 `xx.100.88` 改成 `address` 以绕过脱敏门禁，产生必然误报 | Prompt 要求保留原始脱敏 Candidate；服务端同时要求 keyword pattern 逐项可由 evidence 或合法变量追溯，否则 `run_failed` |
| Matcher 数组首项有证据、其余项靠猜 | “至少一项可追溯”让猜测项搭便车成为 Signal | 对数组逐项校验；任一无法追溯即保留完整 Candidate 并归入 `run_failed` |
| catalog 已登记但调用缺少必需参数 | 裸 `smartctl` 被误认为“catalog 命中即可运行” | 复用命令级最小 argv 契约；失败进入 `run_failed`，不冒充 `not_exists` |
| BMC 页面/普通版本字段被伪造成告警或日志 | Schema 与 catalog 均合法，但运行采集源不存在 | acquisition 必须由 evidence 支持；不一致时完整保留 Candidate 并进入 `run_failed` |
| `/cfs/.../vmid.conf` 被映射为 qfk_log | 文件名可追溯但采集器类型错误，默认日志路径会执行错误目标 | 配置扩展名不得作为日志；明确安全配置路径才归一只读 cat，否则 `run_failed` |
| 同篇已有 smartctl 后省略明确平台告警 | 只保留后台判定会丢失用户可见的生产者事实 | Prompt 对明确 HCI 告警逐项召回 qkv_alert，BMC 外部事件明确排除 |
| `ipmitool mc info` 判断 RAID 固件 | 命令能运行但输出领域不含目标事实 | 命令能力契约拒绝，进入 `run_failed` |
| regex 表达了意图却无法命中 evidence | 保存后现场必然无法按当前证据成立 | 编译并对逐字 evidence 预匹配；失败进入 `run_failed` |

### 为什么不选其他方案

- 不回滚 PR #668：安全边界正确，错误在 Prompt 把门禁前移到了模型阶段。
- 不让 Prompt 只输出 catalog 内命令：会隐藏真实 catalog 缺口，无法持续完善能力目录。
- 不增加第四种状态：三态已覆盖输入、通过、拒绝；具体差异由三个 reason code 表达。
- 不做自动修复循环：会增加不可解释状态和模型调用成本，且可能把错误命令改成“看似合法”的另一个错误命令。
- 不为 KBD27173/KBD27222/KBD27653/KBD27736 写案例硬编码：案例只是通用门禁的回归样本。
- 不把 `smartctl` 最小 argv 规则写成 KBD27222 特判：规则属于命令调用契约，KBD 与 SOP 共用；catalog 将来提供结构化 argv schema 后可直接替换当前最小元数据。
- 不把真实运行失败自动永久改写历史 Proposal：运行结果受现场版本、节点和时点影响，应以运行审计关联精确 revision；确认需要修改知识时由专家产生新 Expert/Proposal。
- 不为概念纯度强制重命名 LLM wire key：`signals` 是既有滚动发布契约，门禁前按 Candidate 解释即可；改名会制造 Prompt/服务非原子升级故障。

## 事实边界

- 静态 Schema/Matcher 检查：证明结构满足当前代码契约。
- catalog 检查：证明命令路径在当前代码随附目录中登记。
- 命令预览/编译：证明当前 Handler 能构造命令模板，不执行命令。
- 预运行：只能证明指定验证环境在该时点可运行。
- 真实运行：只证明精确环境、版本、节点、变量和时点下的结果；失败必须关联 revision 与执行审计，不能外推为所有环境永久失败。

## 影响范围

- KB 抽取路由与 catalog 复用接口；
- Signal v2 JSON Schema 与生成脚本；
- Prompt seed 和 data migration 021；
- KBD 审核页 Rejected Candidate 展示；
- KB、数据库、接口、测试现行文档；
- 后续每批 5 篇的验证事件与本地提交。

## 验收标准

- 自动测试覆盖三类 reason code、分类优先级、历史兼容和正常 Candidate 独立通过。
- KBD30880 连续 5 次重抽均产生 `qkv_task` Signal。
- 首批 5 篇按需求文档的预期分流，修复后同批重跑闭环。
- 管理端能区分并解释三类 Rejected Candidate。
- 全部批次完成后提交单一 PR，CI 全绿并带两个指定标签。
