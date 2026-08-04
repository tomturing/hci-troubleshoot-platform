---
status: active
category: verify
audience: developer, tester
last_updated: 2026-08-04
owner: team
---

# KBD 关键信号 Candidate 三态门禁与批量自查验证

## 验证范围

验证 Candidate 完整输出、三类服务端门禁、历史 Schema 兼容、Prompt 热更新、管理端展示以及每批 5 篇真实重抽。静态验证、命令编译预览和真实运行证据分开记录，禁止互相冒充。

## 自动验证矩阵

| 层级 | 用例 | 预期 | 当前状态 |
|---|---|---|---|
| 单元 | 正常 `qkv_task(keyword=启动虚拟机)` + 三类坏候选混合 | qkv_task 通过；其余依次为 write/not_exists/run_failed | ✅ 已通过 |
| 单元 | write Candidate 同时有其他结构问题 | 优先 `write_signal` | ✅ 已覆盖 |
| 单元 | `acli hardware mc info/web_info`、`acli storage list` | `not_exists` 且 reason 含编译命令 | ✅ 已覆盖 |
| 单元 | 脱敏 Matcher、keyword 伪正则、exists 携带 pattern、数组混入无证据项 | `run_failed` | ✅ 已覆盖，数组逐项追溯 |
| 单元 | catalog 命中的裸 `acli system smartctl` | 缺少最小 argv，归入 `run_failed` | ✅ KBD 与 SOP 共用命令调用契约 |
| 单元 | BMC 外部事件→qkv_alert、普通版本截图→qfk_log(messages) | 采集器/evidence 不一致，归入 `run_failed` | ✅ 正常日志形态 evidence 反例保持通过 |
| 单元 | `ipmitool mc info`→RAID 固件、regex 无法命中自身 evidence | 命令能力/Matcher 预运行失败，归入 `run_failed` | ✅ 正常 BMC 固件与可命中 regex 不受影响 |
| Schema | 新 rejected candidate 带三值 reason_code | 合法 | ✅ 已通过 |
| Schema | 历史 rejected candidate 无 reason_code | 仍合法 | ✅ 已通过 |
| Prompt | 兼容 key `signals` 输出完整 Candidate，不含模型侧“不得输出”回归规则 | 合法且占位符集合不变；Prompt/服务任一先升级不产生空 Proposal | ✅ 已通过 |
| 数据迁移 | v1.9 → v2.1 前向替换、旧规则收敛与幂等断言 | 不改写历史 KBD/revision；不得残留模型侧过滤语句 | ✅ hci-dev 实际升级至 v2.1，并通过正向/负向断言 |
| 前端 | 三类标签与 TypeScript | 类型检查、构建通过 | ✅ Admin 生产构建通过 |
| 后端 | kb-service 完整测试与 Ruff | 全绿 | ✅ 330 passed；Ruff 通过 |

## 已执行命令与环境

仓库已有虚拟环境使用 Python 3.12.13。第一次使用 `uv run --project backend/kb-service` 时，uv 自动选择 Python 3.14.6，`asyncpg==0.29.0` 因不兼容 3.14 构建失败，测试尚未开始；该结果不计功能失败。后续统一使用仓库 `.venv/bin/python`。

专项命令：

```bash
.venv/bin/python -m pytest \
  backend/kb-service/tests/test_signal_generation.py \
  backend/kb-service/tests/test_system_prompt_contracts.py \
  backend/kb-service/tests/test_kbd_patch_signals.py -q
```

当前记录：聚焦测试 49 passed；kb-service 完整测试 303 passed（1 条既有 AsyncMock warning）；Ruff 全绿。

其他验证：

```text
Signal Schema：13 个契约自身合法；合法 fixture 通过；4 个非法 fixture 正确拒绝；代码导出无漂移
Admin UI：vue-tsc -b + vite build 通过（1724 modules transformed）
docs naming --full：仅报告仓库既有 docs/solution/agent/关键信号架构落地设计.md 白名单问题；本次四份事件文档无新增问题
data migration 021：在 hci-dev PostgreSQL 对当前 Prompt v1.9 执行 BEGIN → UPDATE/DO 断言 → ROLLBACK 成功；回滚后复核仍为 v1.9，未提前造成新 Prompt/旧服务不兼容
```

### 滚动升级对抗性验证

首次验证版将 Prompt wire key 改为 `candidates`。手工更新 kb-service Deployment 后，ArgoCD 立即恢复旧镜像；若此时单独应用 Prompt v2.1，旧服务只读取 `signals`，存在保存空 Proposal 的窗口。验证期间未触发任何 KBD 重抽，Prompt 随即从当天 02:00 PostgreSQL 备份精确恢复，MD5 与操作前一致：`c0e4028d38f712697a16dda59e9fe2af`。

最终方案保留 `signals` 传输 key，但明确其内容在服务端门禁前都是 Candidate；新服务同时接受 `candidates`。修订后的 migration 021 已再次在当前 v1.9 上执行 BEGIN → 全部断言 → ROLLBACK，操作前后版本和 MD5 完全一致。由此证明 Prompt 与服务任一先升级都不会因 key 不兼容产生空 Proposal。

### 存量 Prompt 冲突的对抗性验证

首次实际应用 migration 021 后，KBD30880 的 Proposal revision 56 仍未生成 `qkv_task`。数据库中的 v2.1 Prompt 事实检查发现：新规则 25 虽已追加，但 PR #668 的补充规则 24 仍明确要求“不生成 Signal，不以 phase=solution 形式保留”，同时还残留“宁缺毋滥”“不产出信号”和带下游条件的旧任务规则。版本号与正向关键字断言均通过，却不是语义完整的升级。

migration 021 随后改为收敛迁移：替换上述全部已知反向指令，并增加负向断言。KBD30880 五次回归期间使用的 Prompt SHA-256 为 `268e2f960e3fc80117cd4a8f72650f4e76e88177bac21aaceb27f26f8357101a`；旧规则 24、“宁缺毋滥/不产出”和任务下游前置条件均不存在。首批又依次发现脱敏值宽泛化、Matcher 数组夹带、裸 `smartctl`、BMC 页面伪装告警/本机日志、命令能力错配与 regex 自身不可命中等反例；第二批补充 phase 与明确平台告警召回语义，第三批补充配置文件不能伪装 qfk_log。Prompt 与服务端门禁均按通用契约逐轮收敛。当前实装 Prompt MD5 为 `b11de211cf9a25779f0504666a3757ec`，SHA-256 为 `bb5b632abe4eb1dc8b202cda361ac309d504ecf624b71cbf88a4b6100492405e`。

## KBD30880 回归协议

部署当前分支与 Prompt v2.1 后连续重新抽取 5 次，每次记录：

| 次数 | Proposal revision | Prompt hash | qkv_task | Signal 数 | Rejected 分类 | 结论 |
|---:|---:|---|---|---:|---|---|
| 1 | 57 | `268e2f960e3fc80117cd4a8f72650f4e76e88177bac21aaceb27f26f8357101a` | ✅ `keyword=启动虚拟机`、`is_failed=true`、产出 HOST/VM | 1 | write_signal=1，run_failed=2 | ✅ qkv_task 通过；“取消 gpu_type”写动作可见；最新 Proposal 无 Expert 配对 |
| 2 | 58 | `268e2f960e3fc80117cd4a8f72650f4e76e88177bac21aaceb27f26f8357101a` | ✅ title，HOST/VM | 3 | run_failed=1 | ✅ |
| 3 | 59 | `268e2f960e3fc80117cd4a8f72650f4e76e88177bac21aaceb27f26f8357101a` | ✅ problem_description，HOST/VM | 2 | run_failed=1 | ✅ |
| 4 | 60 | `268e2f960e3fc80117cd4a8f72650f4e76e88177bac21aaceb27f26f8357101a` | ✅ problem_description，HOST/VM | 2 | write_signal=1，run_failed=1 | ✅ |
| 5 | 61 | `268e2f960e3fc80117cd4a8f72650f4e76e88177bac21aaceb27f26f8357101a` | ✅ title，HOST/VM | 1 | write_signal=1，run_failed=3 | ✅ |

全部 5 次需满足：

- `qkv_task.args.keyword` 来自正文“启动GPU虚拟机”或等价稳定任务动作；
- `is_failed=true`，produces 至少包含后续真实需要的 HOST/VM；
- phase 为 diagnostic，进入 Signal；
- 没有 Candidate 因 keyword 含“启动”进入 write_signal；
- 新 Proposal 尚无专家修改时，页面相对 AI Proposal 修改数为 0。

第 1 次数据库配对核验：`latest_proposal_revision_id=57`、`working_revision_id=NULL`，以 revision 57 为 `baseline_proposal_revision_id` 的 Expert revision 数量为 0，因此相对当前 AI Proposal 的专家修改数应为 0。同步 API 内部已返回 revision id，但响应模型曾遗漏该字段并显示为 `null`；已将 `proposal_revision_id` 加入响应契约，后续镜像回归验证。

## 第一批 5 篇复跑协议

| KBD | 第二次同批结果 | 第三次同批结果 | 状态 |
|---|---|---|---|
| 27079 | revision 67：4 Signal；write_signal=1 | revision 73：2 Signal；run_failed=1；write_signal=1；正常 MCE 日志和 core 文件检查保持 Signal | ✅ 正常项独立通过，坏 qkv args 可见 |
| 27173 | revision 68：1 Signal；run_failed=3 | revision 74：1 Signal；run_failed=3；`ipmitool mc info` 正常通过，BMC Web/无人消费 producer 未伪装为 Signal | ✅ |
| 27222 | revision 69：3 Signal；not_exists=1；run_failed=1；pattern 数组仅部分可追溯 | revision 75：2 Signal；not_exists=1；run_failed=2；数组夹带已消失，但发现裸 `smartctl` 错误通过 | ❌ 已补命令最小 argv 门禁，待第四次同批复跑 |
| 27653 | revision 70：1 Signal；run_failed=2；write_signal=2 | revision 76：2 Signal；run_failed=2；带 `-i /dev/sdj` 的 smartctl 正常，坏 producer/qkv 契约可见 | ✅ |
| 27736 | revision 71：1 Signal；run_failed=3；write_signal=2 | revision 77：2 Signal；run_failed=2；write_signal=2；脱敏地址继续被拒，正常对话/日志检查保留 | ✅ |

第四次同批 revisions 78/79/83/84/87 验证：KBD27079 的 4 个正常告警/日志/core 检查均通过且处置 Candidate 为 `write_signal`；KBD27653 的 `smartctl -i /dev/sdj` 通过；KBD27736 的脱敏地址与写动作继续正确分流；KBD27222 的 lspci 为 `not_exists`，裸 smartctl 已不再出现。然而模型把 KBD27173 的 BMC 事件误生成为 `qkv_alert`，并把 KBD27222 的 BMC 固件页面版本误生成为本机 `messages` 日志 Signal。两项均是 acquisition/evidence 不一致，已修通用 `run_failed` 门禁，首批需第五次同批复跑。

第五次同批 revisions 88～92 验证：上述 BMC `qkv_alert` 已进入 `run_failed`，伪造 `messages` Signal 未再通过；正常 MCE 日志、平台告警、`smartctl -i` 与 `ipmitool mc info` 的 BMC 固件判定保持 Signal。进一步逐项审查发现 KBD27222 用 `ipmitool mc info` 判断 RAID 适配器固件，KBD27173 的 `^(9H|F6H)` 无法命中自身 evidence；已补命令能力契约与 regex evidence 预匹配，首批需第六次同批复跑。

第六次同批 revisions 93～97 通过：KBD27079 正常 MCE 日志/core 检查为 Signal，坏 qkv args 为 `run_failed`，处置为 `write_signal`；KBD27173 正常 BMC 固件命令为 Signal、外部 BMC 事件为 `run_failed`；KBD27222 的两个 `lspci` 为 `not_exists`、错误 RAID 固件 `ipmitool mc info` 为 `run_failed`；KBD27653 正常带参数 smartctl 为 Signal、坏 qkv args/无人消费 producer 为 `run_failed`；KBD27736 脱敏检查为 `run_failed`、处置为 `write_signal`。首批允许退出并进入下一批。

首批初跑证明三类分流路径均可见，但未达到整批退出标准。KBD27736 的脱敏 IP 没有直接进入 pattern，模型改用 `address`，只能证明配置存在地址行，不能证明 eth0 与 channel4 冲突。修复同时覆盖两层：Prompt 要求忠实保留脱敏 Candidate，服务端要求 keyword pattern 可从 `provenance.evidence` 逐字追溯；否则归入 `run_failed`。第二次同批重跑已闭环 KBD27736，但 KBD27222 暴露“数组至少一项可追溯”仍不充分：模型可在真实 evidence 项旁混入三个猜测项。第三轮收紧为数组每一项均需由逐字 evidence 或合法变量追溯。第三次复跑又发现 catalog 命中的裸 `smartctl` 会错误通过；新增 KBD/SOP 共用的命令最小 argv 契约，将该调用归为 `run_failed`，而 KBD27653 的 `smartctl -i /dev/sdj` 保持通过。第四次复跑补充采集器/evidence 一致性：BMC 外部事件不是 HCI 告警，普通页面字段不是本机日志。所有规则均不读取 support_id，不依赖单案例硬编码。

每篇逐 Candidate 检查：证据是否来自允许的四个章节；工具与参数是否合理；编译命令是否真实；Matcher 是否能在现场数据上成立；变量链是否可达；正常 Candidate 是否未受拒绝项牵连。

## 后续批次模板

### 第二批初跑

第二批固定 IDs 10～14（KBD28094/28156/28177/28900/29294），初跑 Proposal revisions 98～102：

- KBD28094：5 个 Candidate 均因现场占位参数、无 Matcher/producer 或证据不足进入 `run_failed`，未静默丢失；
- KBD28156：坏 qkv args、exists null、keyword 伪 regex 进入 `run_failed`，lspci 进入 `not_exists`；但“重启后 lspci 验证”因 phase=solution 被误分到 `write_signal`；
- KBD28177：正常平台告警与 kernel `PCIe link lost` 日志为 Signal，结构/来源不成立的日志 Candidate 为 `run_failed`；
- KBD28900：只读 ipmitool/lsmod/raw 查询为 Signal，关闭 RAID 监控动作进入 `write_signal`；
- KBD29294：磁盘温度告警与带参数 smartctl 阈值为 Signal，无人消费 producer 为 `run_failed`。

本批唯一通用阻断项是 phase 语义误用。修复只对封闭可证明的只读命令纠偏；实际命令命中写词表时仍优先 `write_signal`，未知/opaque solution Candidate 仍保守进入 `write_signal`。同批待重跑。

第二次同批 revisions 103～107 已证明 phase 纠偏生效：KBD28156 的修复后 lspci 从错误 `write_signal` 改为 `not_exists`，KBD28900 的真实 raw 写动作仍为 `write_signal`。但 KBD29294 明确的磁盘温度告警在本轮被随机省略，说明 Prompt 缺少逐告警召回硬约束；已补“每个不同 HCI 平台告警至少一个 qkv_alert Candidate，BMC 外部事件除外”，同批需第三次重跑。

第三次同批 revisions 108～112 通过：KBD29294 的磁盘温度 qkv_alert 与 smartctl 阈值均为 Signal；KBD28156 两条平台告警分别召回、lspci 为 `not_exists`、reboot 为 `write_signal`；KBD28177 正常告警/日志为 Signal，坏 exists 为 `run_failed`；KBD28900 的 raw 写动作继续为 `write_signal`，结构不完整只读项为 `run_failed`；KBD28094 的正文 `alert_info=无`，未把案例标题标签强制冒充平台告警，LAN set 动作进入 `write_signal`。第二批允许退出。

### 第三批初跑

第三批固定 KBD29713/30396/30838/30884/32010（IDs 15/16/17/19/20；已完成 5/5 专项回归的 published KBD30880 不重复计入）。前四篇完成 Proposal，KBD32010 在 Candidate 分流后因所有通过项均为 `role=context`，verification_contract 的 must 为空，持久化抛出 500，导致 Proposal 与 Rejected Candidate 均未保存。修复将第一条 diagnostic Signal 的角色与 Contract 投影同时确定性提升为 must；没有 Signal 时继续返回 `verification_contract=null`。第三批必须整批重跑。

第二次同批 revisions 117～121 已确认 KBD32010 以 0 Signal + 5 Rejected Candidate 正常持久化，不再 500；KBD29713/30396 的明确平台告警通过，KBD30838 的 RAID 进程/硬件事实通过，KBD30884 的失败任务 producer 与命令检查通过。新发现 KBD30884 将 `/cfs/nodes/.../vmid.conf` 配置文件错误映射成无 path 的 qfk_log 并通过；已补配置扩展名采集器门禁，同批待第三次重跑。

第三次同批 revisions 122～126 通过，使用 Prompt SHA-256 `bb5b632abe4eb1dc8b202cda361ac309d504ecf624b71cbf88a4b6100492405e`：

| KBD | Proposal revision | Signal | Rejected Candidate | 结论 |
|---|---:|---:|---|---|
| 29713 | 122 | 2 | run_failed=1，write_signal=1 | CPU 温度平台告警与 sensors 只读检查通过；宽泛 BMC 检查不可判定，BMC reset 作为写动作可见 |
| 30396 | 123 | 1 | run_failed=2，write_signal=1 | 网口损坏平台告警通过；结构或来源不成立的日志检查被拒绝；删除日志文件作为写动作可见 |
| 30838 | 124 | 3 | run_failed=1 | RAID 告警、进程及硬件事实检查通过；坏 qkv 参数继续可见且未影响正常项 |
| 30884 | 125 | 1 | run_failed=5 | 跨集群迁移失败 qkv_task 通过；无人消费 producer、证据不足的日志/进程检查及 `/cfs/nodes/.../vmid.conf` 配置读取均进入 run_failed，不再伪装为 qfk_log Signal |
| 32010 | 126 | 2 | not_exists=2，run_failed=2 | Proposal 正常持久化；正常网卡统计/历史日志通过，ifconfig/netdoctor catalog 缺口可见，坏 qkv 参数与 Extract 契约进入 run_failed |

5/5 请求均返回 200，没有 API 500、Proposal 丢失或 Candidate 静默过滤。第三批允许退出并进入下一批。

### 第四批初跑

第四批固定 KBD32300/33510/33882/34094/34164（IDs 21～25），初跑 Proposal revisions 127～131。5/5 请求返回 200，任务/平台告警、可追溯日志与 catalog 内只读检查可以独立通过；无消费 producer、坏 Matcher/路径及 catalog 缺口均完整可见。对抗性审查发现写门禁只扫描 `acquire.args.command`，遗漏实际执行向量中的变更：

- KBD33510 的 `soft_raid_lit --off /dev/sda` 会关闭磁盘灯，却被 `not_exists` 掩盖；
- KBD34164 的 `strace -f /sf/bin/sfscp 源 目标` 会执行文件复制，却只按外层 strace 进入 `not_exists`。

修复将明确写门禁扩展到 `command_args` 中的完整写子命令/开关和被包装执行程序 basename，仍不做自由 Shell 推断；`write_signal` 固定优先于 catalog 与运行门禁。第四批必须整批重跑。

修复提交后的确定性回放使用 revisions 127/131 中不可变保存的原始 Rejected Candidate：`soft_raid_lit --off /dev/sda` 命中写动作 `--off`，`strace -f /sf/bin/sfscp ...` 命中被包装写程序 `sfscp`，两者均在 catalog 前得到 `write_signal` 原因。完整代码回归为 319 passed（1 条既有 AsyncMock warning），Ruff、Signal Schema 自身/fixture/代码导出漂移、Admin `vue-tsc -b + vite build` 与 docs 治理均通过。

配额恢复后先后完成两轮真实同批重抽。revisions 132～136 的 KBD33510 生成等价写向量 `sf_cli disk light off /dev/sda`，暴露裸状态子命令 `off` 未进入 write_signal；revisions 137～141 又暴露 KBD34164 的 `ls -l /sf/bin/sfscp` 被按路径 basename 误判写动作，以及 KBD32300 的无证据 state 字面值可进入 Signal。修复采用通用闭集：明确 `on/off` 状态子命令；封闭只读顶层命令不扫描 argv 为动作；非只读 wrapper 继续扫描实际执行程序；state pattern 与 keyword 一样要求 evidence 逐字可追溯。

最终同批重抽使用 `glm-5`，Prompt SHA-256 为 `bb5b632abe4eb1dc8b202cda361ac309d504ecf624b71cbf88a4b6100492405e`：

| KBD | Revision | Signal | Rejected | 结论 |
|---|---:|---:|---:|---|
| 32300 | 142 | 3 | 0 | 平台告警与只读 LLDP 采集独立通过；未再出现无证据 state 字面值 |
| 33510 | 143 | 1 | 3 | `soft_raid_lit --off` 为 write_signal；两个无消费 producer 为 run_failed |
| 33882 | 144 | 2 | 2 | 任务与 MTU 证据通过；无证据日志/ping Matcher 为 run_failed |
| 34094 | 145 | 2 | 2 | 任务与 VM 配置通过；不可追溯 qfk_log 来源为 run_failed |
| 34164 | 146 | 2 | 3 | `strace → sfscp` 为 write_signal；无消费 producer、无判定器信号为 run_failed |

5/5 请求返回 200，revisions 连续 142～146，无 429、API 500、空 Proposal 或 Candidate 静默丢失。`sf_cli ... off` 与 `ls ... sfscp` 本轮模型未同时复现，分别由确定性单测锁定；真实重抽未执行任何 HCI 命令。第四批允许退出，后续批次仍须独立提交和同批闭环。

每批事件记录以下内容：

```text
批次编号 / 5 个 support_id / Proposal revision / Prompt hash / 模型
每篇 Candidate 总数 / Signal 数 / write_signal / not_exists / run_failed
逐条准确性、合理性、可执行性结论
发现的共性问题与反例
代码/Prompt/Schema 修改及本地 commit
同批重跑结果
是否允许进入下一批
```

## 运行事实边界

- `not_exists`：当前代码随附 catalog 未登记；不能直接断言真实 HCI 永远不存在。
- 静态 `run_failed`：当前保存/编译契约失败；未触发真实命令。
- 命令预览成功：当前 Agent Handler 可构造模板；不代表执行成功。
- 真实运行失败：必须记录环境、产品版本、节点、变量、exit code、stderr、Signal ID 和精确 revision；再由专家判断是知识错误、catalog/Handler 缺口还是环境条件不满足。

## 退出标准

1. 专项、完整后端、Ruff、Schema、前端、docs 检查全绿。
2. KBD30880 5/5 通过。
3. 第一批 5 篇修复后同批闭环。
4. 后续每批 5 篇均留下独立提交和验证记录。
5. 最终 PR CI 全绿，包含 `env:dev:sf` 与 `agent:codex` 标签。
