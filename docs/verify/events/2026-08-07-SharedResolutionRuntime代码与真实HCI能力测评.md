---
status: in_progress
category: verify
audience: developer, tester, operator, security
last_updated: 2026-08-07
owner: team
---

# Shared Resolution Runtime 代码与真实 HCI 能力测评

> 持久化说明：本报告是本轮代码、现场命令、SQLite 只读检查和对抗性审查的权威归档；会话输出不作为验收证据。凡标记为“已落实”的内容必须能在代码/测试中定位，凡标记为“仅测评发现”的内容明确表示尚未编码。

## 测评范围

本次测评覆盖：

- `Shared Resolution Runtime` 的六个 Resolver：`LogResolver`、`SystemResolver`、`DomainResolver`、`ServiceResolver`、`QkvResolver`、`VariableResolver`；
- QFK/QKV 执行前的 `compile()`/`resolve()` 接入；
- 日志别名、错别字、部分路径、完整绝对路径、D/DD 日期目录、`vt` 子目录和 gzip 轮转；
- aCLI 本地 Catalog 与在线命令列表的一致性；
- 真实 HCI `172.28.25.1` 的只读能力和代表性命令。

现场连接只使用用户提供的 root 账号，所有远端命令均为只读命令、`--help`、路径存在性检查或无命中的日志查询；没有执行服务启停、配置写入、删除、重启、轮转或解压写操作。

## 代码实现结果

### 六阶段状态

```text
阶段一 Runtime 基础契约：完成
阶段二 六领域 Resolver 纵向切片：完成首个切片，生产 probe 待补强
阶段三 QKV 语义纠错：完成首批 Action Catalog、canonical-first 和有限 alias 回退
阶段四 执行保证闭环：QFK/QKV 层 verified gate 和摘要快照已接入，通用 Executor/持久化审计待补
阶段五 对象类型 Handler：Archive 只读检查内核已新增，专用 Catalog/SQLite Handler 待补
阶段六 生产治理与能力测评：跨版本、TOCTOU、大规模 replay 和 KBD 保存门禁待补
```

新增共享运行时：

| 组件 | 实现位置 | 结果 |
|---|---|---|
| Pydantic 数据契约 | `backend/shared/resolution/models.py` | `SignalIntent`、`ResolutionPlan`、`ResolvedAcquisition`、状态和结构化错误 |
| 唯一 Registry | `backend/shared/resolution/runtime.py` | `resolver_id=log/system/domain/service/qkv/variable` |
| 声明式 Runtime Catalog | `backend/shared/resolution/catalogs/resolution_catalog.json` | 别名、命令参数需求、Catalog 版本 |
| aCLI Catalog 读取 | `backend/shared/resolution/catalog.py` | 与现有 336 条 aCLI 命令快照匹配 |
| 日志解析 | `backend/shared/resolution/resolvers.py::LogResolver` | basename/别名、相对/绝对路径、END、D/DD、today、vt、gzip 前置检查 |
| 系统命令解析 | `SystemResolver` | token/argv、点号命令规范化、未知命令 fail closed |
| 领域命令解析 | `DomainResolver` | vm/network/storage/hardware/platform 共享框架；VM config get 要求 `--vm-id/-v` |
| 服务解析 | `ServiceResolver` | 服务组、服务名、只读 `status` |
| QKV 解析 | `QkvResolver` | alert/task/dialog 查询契约 |
| 变量解析 | `VariableResolver` | 变量替换、未解析变量 fail closed |
| QFK/QKV 接入 | `app/tools/qfk/resolution.py`、QFK/QKV engine | 执行前统一调用 Runtime，并将 resolution 结果纳入结果对象 |

## 自动化测试结果

### Shared Runtime 定向矩阵

```text
42 passed in 0.85s
```

覆盖：

- `vtpdeamon` → `sfvt_vtpdaemon.log`；
- D/DD 候选生成与注入式现场 path probe 选择；
- `vt/sfvt_vtpdaemon.log` 和 `/sf/log/today/vt/sfvt_vtpdaemon.log`；
- gzip 搜索缺少 `archive_precheck=verified` 时拒绝；
- `acli.vm.config.get` → `['acli', 'vm', 'config', 'get']`；
- 未知系统命令 fail closed；
- VM `config get` 缺少 `--vm-id/-v` 时拒绝；
- service 非 `status` 动作拒绝；
- 未解析 `{{VM}}` 变量进入 `needs_probe`。

### Agent Service 全量回归

```text
587 passed, 1 skipped, 3 warnings in the latest full run
```

3 个 warning 来自既有 `test_tool_audit.py` 中 `AsyncMock` 未 await 的资源警告，不是本次 Runtime 变更引入的失败。

### Schema 和静态质量

```text
ruff check：All checks passed
信号 Schema 漂移检查：OK
13 个契约文件合法；4 个非法 fixture 正确拒绝；代码导出与 JSON 文件一致
```

## aCLI 在线文档与本地 Catalog 测评

在线文档：[aCLI 命令列表](http://acli.sangfor.com.cn:6888/commandList)

在线页面标注更新时间为 `2026-05-20`。本次解析到在线命令 336 条；仓库快照
`backend/agent-service/app/tools/acli/catalog/acli_command_catalog.json` 也是 336 条，交集 336 条：

```text
online_count       336
local_count        336
intersection       336
online_only_count    0
local_only_count     0
```

在线文档和现场帮助共同确认的关键契约：

| 能力 | 事实 |
|---|---|
| `acli log get -f` | 只能接 basename，不能包含路径 |
| `acli log get -p` | 只允许 `/sf/log` 和 `/sf/data/local` 下绝对路径；目录/文件名可使用 `*` |
| `acli log get -t` | 支持 `YYYY-MM-DD HH:MM:SS`、`YYYY-MM-DD HH` 等绝对时间 |
| `acli log get -g` | 针对 `-p` 搜索 gzip；目录搜索深度有限，不能把 tar.gz 当普通 gzip 文本 |
| `acli service` | 当前现场公开 `anet`、`asv`、`host` 三个服务组 |
| `acli vm config get` | 必需 `-v/--vm-id`，不能由无参数的模型字符串直接执行 |

## 真实 HCI 测评结果

### 基础版本和目录事实

```text
aCLI version: 1.0.0
build: 2026-06-01 19:27:42
/sf/log/today -> /sf/log/7
```

现场文件检查结果：

```text
present /sf/log/7/vt/sfvt_vtpdaemon.log
present /sf/log/6/vt/sfvt_vtpdaemon.log
present /sf/log/6/vt/sfvt_vtpdaemon.log.2.gz
absent  /sf/log/07/vt/sfvt_vtpdaemon.log
absent  /sf/log/7/sfvt_vtpdaemon.log
```

这证明当前节点实际采用月内无前导零日目录 `D`，并且 `sfvt_vtpdaemon.log` 位于 `vt` 子目录；代码保留 `D/DD` 候选是为了兼容其他版本，现场 probe 会优先选择真实存在的候选。

blackbox 现场存在日期 tar.gz 归档，例如：

```text
/sf/log/blackbox/20260803.tar.gz
/sf/log/blackbox/20260806/pmxcfs.db_202608061456.tar.gz
/sf/log/blackbox/20260807/pmxcfs.db_202608071456.tar.gz
```

### 代表性只读命令

| 测试 | 结果 |
|---|---|
| `acli log get -k <无命中> -f sfvt_vtpdaemon.log -p /sf/log/7/vt -t 2026-08-07` | exit 0，空输出；参数契约成立 |
| `acli log get -k <无命中> -p /sf/log/7/vt/sfvt_vtpdaemon.log` | exit 0，完整绝对文件路径成立 |
| `acli log get -k <无命中> -p /sf/log/6/vt/sfvt_vtpdaemon.log.2.gz -g` | exit 0，显式 gzip 文件搜索成立 |
| `acli system df -P /sf/log` | exit 0，`/sf/log` 使用率约 73% |
| `acli service asv vtpdaemon status` | exit 0，服务 started/running |
| `acli --formatter json vm list` | exit 0，返回约 46 KiB 结构化 VM 数据 |

### Runtime 与现场路径交叉验证

将现场已确认存在的 `/sf/log/7/vt` 和 `/sf/log/6/vt` 注入只读 `path_exists` probe：

```text
vtpdeamon + END=2026-08-07 → sfvt_vtpdaemon.log → /sf/log/7/vt/sfvt_vtpdaemon.log → verified
完整 /sf/log/7/vt/sfvt_vtpdaemon.log → /sf/log/7/vt/sfvt_vtpdaemon.log → verified
qfk_system df -P /sf/log → acli system df -P /sf/log → verified
qfk_vm list → acli vm list → verified
qfk_service asv/ vtpdaemon/status → acli service asv vtpdaemon status → verified
```

## `/sf/log` 深度测评（2026-08-07 16:09 CST）

本节针对“时间目录 + 上层目录 + 文件/结构化对象”三个定位因素重新取证。`find` 对符号链接默认不跟随，因此现场同时使用 `stat/readlink -f` 和 `find -L`；否则会错误地把 `today` 误判为空目录。

### 目录和符号链接事实

| 目标 | 现场事实 | 对定位器的含义 |
|---|---|---|
| `/sf/log/today` | 符号链接，`readlink -f` 为 `/sf/log/7`；跟随后约 1,945 个文件 | `today` 是白盒 D 目录别名，不是固定文本目录；解析必须记录 link target，并支持继续进入 `vt/vn/vs/acli/audit_log` 等深层目录 |
| `/sf/log/blackbox/today` | 符号链接，指向 `/sf/log/blackbox/20260807`；跟随后 84 个文件 | 黑盒时间目录使用 `YYYYMMDD`，与白盒 D 目录不同；包含 `.txt`、`.zip` 和 `pmxcfs.db_*.tar.gz` |
| `/sf/log/vn-blackbox/today` | 符号链接，指向 `/sf/log/vn-blackbox/20260807`；跟随后 26 个文件 | VN 黑盒有独立根和独立日期树，不能复用 `/sf/log/blackbox` 的上层规则 |
| `/sf/log/vn-cpp` | 不存在；在 `/sf/log` 内也未发现 `vn-cpp`/`*cpp*` 匹配 | 该名称不能作为当前版本的存在性事实；应返回 `NOT_FOUND` 或要求候选目录，而不是拼接一个看似合理的绝对路径 |
| `/sf/log/apache2` | 普通目录；`access.log`、`api_access.log`、`error.log` 及 `.1`、`.2.gz`…轮转 | 组件目录不是日期目录；轮转序号与 gzip 必须分别建模，`error.log.1` 甚至可比当前文件旧一个月，不能只按 mtime 选 |
| `/sf/log/kafka` | 普通目录；`kafkaServer-gc.log.0.current`、`.current.1`…及 `log-cleaner.log` | Kafka GC 轮转文件无 `.log.N` 传统格式，basename 精确匹配和轮转族规则不能套 Apache 规则 |
| `/sf/log/checkitem` | 12 个无扩展名指标文件（`cpu_rate_date`、`netio_data` 等） | 这是结构化检查项/时序数据，不应默认按 `timestamped_lines` 白盒日志解析；需要独立 parser/Schema |
| `/sf/log/sf-openapi` | 普通目录；当前 `app.log` 加 `app.log-YYYY-MM-DD.gz`，覆盖 2026-07-08 至 2026-08-06 | 日期编码在文件名，不在父目录；`today`/D-DD 候选对该组件不适用，归档是普通 gzip 日志而非 tar member |

白盒目录的关键对抗性事实是：同一 `today` 入口下同时存在根文件、`vt`、`vn`、`vs`、`acli` 和 `audit_log` 多层对象；aCLI 帮助又明确 `-p` 对目录的 gzip 搜索不能递归更深层。因此“找到目录”不等于“aCLI 一定能找到目录下的目标文件”，定位器必须输出最终文件绝对路径或明确的递归深度/handler 能力。

对代表性绝对路径执行无命中只读查询（关键字为特意构造的不存在值）均返回 exit 0、空结果：

```text
/sf/log/blackbox/today/LOG_dmesg.txt       -g   → exit 0
/sf/log/vn-blackbox/today/LOG_arp.txt            → exit 0
/sf/log/sf-openapi/app.log                       → exit 0
/sf/log/today/vt/sfvt_vtpdaemon.log              → exit 0
```

这只能证明 aCLI 接受这些路径和 handler 组合，不能证明任意关键字都有命中；“exit 0 + 空输出”不能被消费校验误判成文件存在且内容已验证。

### 黑盒与 VN 黑盒的归档语义

`/sf/log/blackbox/today` 的代表性对象包括 `LOG_dmesg.txt`（约 120 MiB）、`LOG_ps_user.txt`、`LOG_ps_kernel.txt`、`LOG_cgroup.txt` 及其 `.zip` 历史版本；另有 `pmxcfs.db_202608071456.tar.gz`。`/sf/log/vn-blackbox/today` 则包含 `LOG_arp.txt`、`LOG_ethtool_statistic.txt`、`LOG_vxlan_ping_statistic.txt` 等网络面向快照及 `.zip` 历史版本。

这证明至少存在三种不同的“压缩”语义：

1. `.gz`：单个文本日志（如 Apache、sf-openapi）；
2. `.zip`：黑盒快照中单文件的轮转压缩；
3. `.tar.gz`/`.tar.zst`：多成员归档或整日打包（根目录还存在 `1.tar`、`1.tar.zst`…`31.tar.zst`）。

不能用一个 `gzip=True` 标志覆盖三者。`LogResolver` 应将 `archive_kind` 和 member selector 作为计划字段，并由专用 Archive Handler 验证成员存在；当前代码对 `.gz` 有前置门禁，但 tar/tar.zst 仍是未完成能力。

### `log_new.db`：不是文本日志，而是 QKV 数据源

现场 `file`/`stat` 和只读复制后的 SQLite `mode=ro` 检查结果：

```text
/sf/log/log_new.db
SQLite 3.x database；size=26,009,600 bytes；mtime=2026-08-07 16:06:44 CST
PRAGMA integrity_check = ok
```

数据库对象和行数如下：

| 表 | 行数 | 关键字段/语义 |
|---|---:|---|
| `alert` | 24,518 | `type, host, hostname, vm, object_type, object_id, description, start, end, status, level, log_id` 等；`level='alert'`；`reserved3` 为 JSON 文本（维护模式等） |
| `alert_translation` | 49,036 | `log_id, lang, type, object_type, object_name, description`；中英文翻译，每条告警通常两种语言 |
| `log` | 856 | 操作/任务审计，含 `request_id, event_id, risk_level, action_type, module_type, start, end, status, upid` 等；`level='op'`；描述字段含模板/参数文本 |
| `log_translation` | 1,712 | 操作日志翻译，`log_id, lang` 及展示字段 |
| `alertuser_object_id` | 0 | 告警用户对象过滤配置表 |
| `alertuser_type` | 0 | 告警用户类型过滤配置表 |
| `vmwarealert` | 0 | VMware 告警扩展表，当前为空 |

时间范围（把 Unix 秒转换为 CST）为：`alert` 2026-06-12 18:39:39 至 2026-08-07 16:06:43，`log` 2026-06-12 04:34:09 至 2026-08-07 15:46:57；`log.request_id` 有 830 条非空记录。表名没有 `task`，但“task”数据确实存在于 `log`：现场 `acli --formatter json task list` 返回的 id 854/855/856 可在 `log.id` 精确找到，字段和 `request_id`、时间、主机、状态一致；其中 854/855 是“系统备份/合并系统备份”，856 是“登录”。因此不能以“没有 task 表”判断没有任务数据。

同理，`acli --formatter json alert list` 返回的告警 id 24417、24514–24518 可在 `alert.id` 精确找到，且 `vm/object_id/status/start/end/log_id` 一致。结论是：`log_new.db` 是 aCLI/QKV 使用的本地结构化事件库（告警 + 操作/任务 + 翻译），不是供 `qfk_log` 逐行扫描的普通日志文件。

### `alert_translation` 的关系语义：字典角色，但实例记录形态

针对用户提出的“它是字典还是记录”问题，进行了第二轮关系级只读分析。结论不能简单二选一：

```text
业务角色：本地化翻译/展示字典
物理形态：与 alert 实例一对多关联的翻译记录
```

证据如下：

| 检查项 | `alert_translation` 结果 |
|---|---:|
| 总行数 | 49,036 |
| `lang=en_US` | 24,518 |
| `lang=zh_CN` | 24,518 |
| `distinct(log_id)` | 24,518 |
| 每个 `log_id` 的翻译行数 | 全部为 2 |
| 每个 `log_id` 的语言数 | 全部为 2（`zh_CN` + `en_US`） |
| `alert` 中没有翻译的实例 | 0 |
| 没有对应 `alert` 的翻译行 | 0 |
| `alert_translation.log_id` 索引 | 存在：`alert_translation_log_id` |

这说明它不是只保存如下静态字典：

```text
event_code → 中文文案 / 英文文案
```

因为它的 `object_name` 和 `description` 会携带告警实例的动态值。例如同一类“虚拟机磁盘读时延过高”会按具体虚拟机、磁盘号、时延和阈值生成不同的翻译记录；`alert_translation` 中共有 929 个不同 description，而不是只有少量固定模板。它更准确的定义是：

```text
alert 实例的本地化投影表
alert.id 1 ── 1..N ── alert_translation.log_id
```

当前数据恰好是每条告警两条翻译记录，但 DDL 没有 `FOREIGN KEY` 约束，`PRAGMA foreign_keys=0`；关系依靠应用层的 `log_id` 约定和索引维护。后续 DB adapter 必须自己检查孤儿、缺失语言和重复语言，不能仅凭 DDL 得出完整性保证。

### `alert_translation` 是否包含 task/log 翻译？

结论：**不包含。task/操作日志使用独立的 `log_translation` 表。**

两张翻译表 DDL 形状相似，但来源表不同：

```text
alert_translation.log_id → alert.id
log_translation.log_id   → log.id
```

关系级检查结果：

```text
alert_translation → alert：
  覆盖 24,518/24,518 个 alert
  孤儿记录 0
  缺失翻译的 alert 0

log_translation → log：
  覆盖 856/856 个 log
  孤儿记录 0
  缺失翻译的 log 0
```

`log_translation` 的规模正好是：

```text
856 个 log 实例 × 2 种语言 = 1,712 行
```

样例能够直接区分两者：

```text
log.id=854
  log_translation.zh_CN.type = 系统备份
  log_translation.en_US.type = Create system backup

alert.id=854
  alert_translation.zh_CN.type = 虚拟机磁盘写时延过高
  alert_translation.en_US.type = High VM Disk Write IO Delay
```

这里故意选取相同的整数 854，是因为 `alert.id` 和 `log.id` 数值范围重叠。它证明不能用“相同的 `id`”把 alert 翻译和 task 翻译混为一谈：必须根据翻译表所属的父表区分语义。数据库中也没有跨表外键来替系统阻止这种错误 Join。

因此以下查询是错误的：

```sql
-- 错误：仅按整数 id 在 alert 和 log 之间混连
select *
from alert_translation t
join log l on l.id = t.log_id;
```

正确的关系查询必须明确父表：

```sql
-- 告警本地化记录
select a.id, a.start, a.status, t.lang, t.type, t.description
from alert a
join alert_translation t on t.log_id = a.id
where a.id = :alert_id;

-- task/操作本地化记录
select l.id, l.start, l.status, t.lang, t.type, t.description
from log l
join log_translation t on t.log_id = l.id
where l.id = :log_id;
```

### 对 QkvResolver 的最终影响

`QkvResolver` 必须把 `alert` 和 `task` 视为两个不同的 query kind：

```text
alert  → alert + alert_translation
task   → log + log_translation
dialog → 按产品契约选择对应的事件/对话数据源
```

翻译表不是独立的“事件真相源”，也不是可随意替代父表的全局字典。查询结果应以父表的事件状态、时间、host/vm/object_id 为事实，以对应 translation 行提供语言化展示；当翻译缺失、语言重复或父记录不存在时，QkvResolver 应返回结构化数据质量问题，而不是静默回退到另一张翻译表。

### QKV 关键词纠错优化：`开启虚拟机` 与 `启动虚拟机`

用户输入错误关键词时，当前现场行为已经可以确定：

```text
acli task get -k "开启虚拟机"
```

不能因为 `开启虚拟机` 与产品动作“启动虚拟机”语义相近，就假设 aCLI 会自动扩展同义词。`-k` 是文本匹配条件，QkvResolver 必须在调用 aCLI 前完成规范化，或在无命中后执行受控候选回退。

推荐的内部模型不是“把 translation 表当作全局同义词字典”，而是：

```text
原始 keyword
  → 规范化
  → Action Catalog action_id
  → canonical keyword + bounded aliases
  → aCLI 查询
  → 结果动作类型验证
```

例如：

```json
{
  "action_id": "vm.power_on",
  "canonical_keywords": ["启动虚拟机"],
  "aliases": ["开启虚拟机", "开机虚拟机", "启动VM"],
  "negative_aliases": ["重启虚拟机", "恢复虚拟机"]
}
```

`log_translation.type` 可以参与构建“历史出现过的动作标签”候选集，但不能直接作为可执行 alias 发布，原因是：

- 它是 task 实例翻译记录，不是独立规范库；
- 同一 action 可能有不同语言和历史展示词；
- `object_name`、`description` 含动态 VM/主机/参数，不能进入同义词表；
- 未出现过的动作不会出现在当前数据库快照中；
- 当前数据库没有动作本体 ID 和负例约束。

运行时应采用有界回退：先查规范词；零命中时最多尝试审核过的少量 alias；记录每个候选的 `match_count`、样例 `type/action_id`、时间范围和状态分布；只有结果唯一且动作语义一致才验证成功。多候选命中但无法消歧必须返回 `AMBIGUOUS`，全部无命中返回 `NOT_FOUND`，禁止无限同义词扩展或再次让 LLM 自由猜测。

如果已有 `HOST`、`VM`、`END`、`status` 或 `UPID`，应优先使用 aCLI 的结构化过滤参数 `-H/-v/-t/-s/-u`，把关键词降级为辅助条件。这样比仅依赖中文词面更可靠。

当前代码状态：`QkvResolver` 已实现 query 类型、keyword 非空、limit 和 alert/task/dialog 命令模板校验；本轮已新增首批 Action Catalog、规范动作 ID、canonical-first、空结果后的有界 alias retry 和关键词 evidence。结果 action_id 消歧、结构化 selector 优先、完整 Action Catalog 和通用 Executor 层硬门禁仍未完成，因此不能标记为已完成生产能力。

### 对 Runtime 分层的直接影响

```text
qfk_log / LogResolver  → 文本、轮转、黑盒快照、归档 member 的绝对路径
QkvResolver            → alert/task/dialog 查询契约；优先通过受控 aCLI/QKV 接口
DatabaseQuery handler  → 仅在明确只读 schema、版本和锁/快照策略后，mode=ro 查询 log_new.db
```

当前不应让 `qfk_log` 直接把 `log_new.db` 当 `timestamped_lines`；也不应让 LLM 猜测 `task` 表名。若未来需要数据库直读，应增加独立 `SQLiteQueryHandler`：只读 URI、白名单表/列、参数化 SQL、查询超时、结果 Schema、数据库 inode/size/mtime 指纹和审计；默认路径仍是 `QkvResolver`/aCLI，保证与产品语义一致。

### 其他需要纳入观察面的对象

本次顶层枚举还发现：按日白盒目录 `/sf/log/6`、`/sf/log/7`，`blackbox`/`vn-blackbox` 日期树，`audit_log`、`pods`、`vtp/tasks`、`vs`、`vn-ccp`、`zookeeper`、`kubelet`、`containerd` 以及根部 `*.tar.zst`/`*.tar`。它们分别代表白盒服务日志、Kubernetes 容器日志、审计日志、组件快照、整日归档，不能被单一 filename+parent 规则覆盖。下一轮 Catalog 应至少为每类声明：时间来源（父目录/文件名/mtime）、symlink 是否跟随、递归深度、轮转排序、archive_kind、parser、最大读取大小和权限/TOCTOU 策略。

## 本轮分析的代码落实矩阵

为避免把“现场发现”误读成“已经编码”，以下矩阵逐项区分代码现状。状态含义为：

```text
已落实：当前代码已有可执行实现并有自动化测试或现场交叉证据
部分落实：已有通用框架/声明，但缺少该对象的完整 handler、硬门禁或生产接线
仅测评发现：本轮已确认现场事实，尚未进入 Runtime/Catalog/Handler
```

| 分析结论/能力 | 当前代码状态 | 代码证据或缺口 | 后续实现落点 |
|---|---|---|---|
| `today` symlink、`D/DD`、`vt` 白盒布局 | 已落实（通用 LogResolver） | `backend/shared/resolution/resolvers.py::LogResolver` 生成 D/DD 候选；`path_exists` probe 可选择真实 `/sf/log/7/vt/...` | 生产 bridge 必须提供真实 probe，并把 symlink target 写入 evidence |
| basename、部分路径、完整绝对路径、别名/错别字 | 已落实 | LogResolver canonical filename、`path_hint`、absolute path dirname 拆分和 alias 规则已有测试 | 增加编辑距离候选的 Catalog 约束和多候选拒绝测试 |
| blackbox / vn-blackbox 日期根 | 部分落实 | `backend/shared/schemas/log_source_catalog.py` 已有 `blackbox`、`vn_blackbox` family 和 today/YYYYMMDD 候选 | 增加目录/文件存在性、`find -L` 语义、`.zip` 与 tar member Handler |
| `LOG_dmesg.txt`、`LOG_ps_*`、`LOG_arp.txt` 等快照 parser | 部分落实 | Catalog 已有 `host_blackbox`、`process_snapshot`、`vn_network_snapshot` 等声明 | 将 snapshot 类型、快照时间字段、采样周期纳入统一 Schema |
| `.gz` 单文件日志 | 已落实（前置门禁） | `include_archives=true` 必须 `archive_precheck=verified`；现场 Apache/sf-openapi gzip 可被 aCLI 接受 | 增加实际 gzip 成员/可读性证据与 Executor 硬门禁 |
| `.zip` 黑盒轮转文件 | 仅测评发现 | 现场已确认存在，但当前 Runtime 没有 ZipArchive Handler | 新增 `archive_kind=zip` 和只读成员校验 |
| `.tar.gz` / `.tar.zst` 多成员或整日归档 | 仅测评发现 | 当前代码没有独立 Archive Handler；不能按普通 gzip 读取 | 新增 tar/tar.zst member selector、路径穿越防护、大小/压缩炸弹限制 |
| `/sf/log/apache2` 数字轮转族 | 仅测评发现 | 当前 Catalog 没有 `apache2` source_id 和 numeric rotation strategy | 增加 Apache Catalog、轮转族排序和日期/mtime 语义测试 |
| `/sf/log/kafka` `.current.N` 轮转族 | 仅测评发现 | 当前 Catalog 没有 Kafka rotation handler | 增加 Kafka GC log source、current/rotated 选择规则 |
| `/sf/log/checkitem` 无扩展名指标文件 | 仅测评发现 | 当前 parser 集合没有专用 checkitem schema | 增加 `metric_snapshot` parser、字段类型和时间来源声明 |
| `/sf/log/sf-openapi/app.log-YYYY-MM-DD.gz` | 仅测评发现 | 当前 Catalog 没有 filename-date source；现有 D/DD 逻辑不适用 | 增加 `time_source=filename` 和日期文件名解析 |
| `/sf/log/vn-cpp` 不存在、`vn-ccp` 存在 | 已记录为现场事实，未启用自动纠错 | 当前 Catalog 没有这两个别名，故不会自动改写 | 如需纠错，必须提交专家审核的 alias + 现场唯一性 fixture |
| `log_new.db` SQLite schema | 仅测评发现 | 当前 Runtime 没有 SQLiteQueryHandler，也没有数据库 schema Catalog | 新增独立只读 DB Resolver/Handler，禁止 qfk_log 文本解析器接管 |
| `alert` 数据 | 已通过现场交叉验证，未实现 DB 直读 | QkvResolver 统一 alert 查询契约；现场 aCLI id 与 SQLite `alert.id` 一致 | 保持 aCLI/QKV 为默认接口，后续增加可选只读 DB adapter |
| `task` 数据 | 已通过现场交叉验证，未实现 DB 直读 | QkvResolver 有 task 契约；现场 aCLI task id 与 SQLite `log.id` 一致 | Catalog 明确 task→log 表映射，不猜 `task` 表名 |
| 审核 alias 的 QKV 关键词 | 部分落实 | Action Catalog 已支持 `vm.power_on` 等 canonical/alias；QKV engine 已 canonical-first、空结果后有限 alias 回退并写入 evidence | 增加结果 action_id 消歧、结构化 selector 优先和完整 Action Catalog |
| `exit 0 + 空输出` 的语义 | 已识别风险，未完全硬化 | 当前 Handler 仍可能把 aCLI 成功退出当作采集成功 | 结果模型拆分 `path_exists/readable/query_executed/match_count`，消费门禁逐项检查 |
| immutable resolution audit snapshot | 未落实 | resolution 当前只附加到 QFK/QKV 结果对象 | 增加不可变审计表、catalog/resolver 版本和 inode/size/mtime 指纹 |
| 生产 Executor `verified=True` 硬门禁 | 部分落实 | QFK match/consume 与 QKV engine 已拒绝非 `verified` acquisition；QFK `produce` 保留只读 discovery 例外；通用 Bridge/其他调用方尚未统一接入 | 将 verified gate 下沉到 Executor API，并把 produce 结果转成真实 probe evidence |
| Log D/DD 候选 retry | 部分落实 | QFK match 模式会使用 Runtime 已输出的候选目录扩展受控 `-p` 重试；真实 probe bridge 和失败/空结果语义仍待接线 | 将 retry 与现场 path/archive probe 绑定，并记录每次候选结果 |

因此，本报告中“已通过”只覆盖矩阵中标记为“已落实”或“已通过现场交叉验证”的项目；“仅测评发现”不代表已经提交代码，“部分落实”也不代表生产可用。该矩阵是后续实现和 PR 审核的唯一边界，避免再次依赖会话记忆。

## 能力结论

### 已通过

- 共享 Runtime 和六个 Resolver 的代码级入口已建立；
- 日志关键输入的 basename、别名、错别字、相对目录、完整绝对路径和 END 日期候选可结构化解析；
- 当前 HCI 的 `D` 目录、`vt` 子目录、gzip 轮转和 `today` symlink 已被真实验证；
- aCLI 在线命令列表与本地 Catalog 336/336 一致；
- qfk_log、qfk_system、qfk_vm、qfk_service、QKV 均经过统一 Runtime preflight；
- 未知系统命令、服务写动作、缺失 VM ID、未解析变量均有 fail-closed 路径。

### 尚未通过或仍需补齐

- 生产 Executor 尚未强制要求外部真实 path probe 后才执行；当前 QFK 兼容路径仍允许 Runtime 返回 `needs_probe` 后交给既有 Handler 执行，需下一步将 probe/verified acquisition 作为硬门禁；
- qfk_log 目前支持显式 gzip 文件搜索，但 blackbox `tar.gz` 需要独立 archive Handler，不能按普通文本 gzip 直接读取；
- `D/DD` 已生成候选，但既有 Handler 尚未实现“首候选失败后自动重试下一个候选”的完整 fallback 执行闭环；
- Runtime 结果已回传到 QFK/QKV 结果对象，但尚未统一持久化到运行审计表中的 immutable resolution snapshot；
- 未进行生产环境的大规模 KBD replay、跨版本 HCI 矩阵和 TOCTOU 并发压力测试。

## 验收判定

本次结果判定为：

```text
代码级纵向切片：通过
本地契约/回归：通过
真实 HCI/aCLI 代表性只读能力：通过
生产级绝对路径硬门禁、tar.gz archive handler、immutable audit snapshot：进行中
```

因此不能把本次结果表述为“所有版本、所有归档和所有 KBD 已完成生产验收”；正确表述是“共享解析运行时已完成可运行的第一阶段，关键当前版本能力已用真实 HCI 校准，剩余生产硬门禁和归档扩展已明确”。
