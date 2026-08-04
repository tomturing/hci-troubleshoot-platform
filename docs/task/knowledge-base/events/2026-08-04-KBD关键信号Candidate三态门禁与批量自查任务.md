---
status: active
category: task
audience: developer
last_updated: 2026-08-04
owner: team
---

# KBD 关键信号 Candidate 三态门禁与批量自查任务

## 关联文档

- [需求](../../../requirement/events/2026-08-04-KBD关键信号Candidate三态门禁与批量自查需求.md)
- [方案](../../../solution/knowledge-base/events/2026-08-04-KBD关键信号Candidate三态门禁与批量自查方案.md)
- [验证](../../../verify/events/2026-08-04-KBD关键信号Candidate三态门禁与批量自查验证.md)

## 实施任务

| ID | 任务 | 状态 | 验收 |
|---|---|---|---|
| T-KB-CANDIDATE-01 | Prompt 完整输出 Candidate；传输层保留兼容 key `signals`，后端同时接受 `candidates` | ✅ 代码完成 | Prompt/服务可非原子滚动升级，不会产生空 Proposal |
| T-KB-CANDIDATE-02 | 移除模型侧写动作/catalog 缺失过滤，明确 `qkv_task` 只读查询语义 | ✅ 代码完成 | KBD30880 连续 5 次出现 qkv_task |
| T-KB-CANDIDATE-03 | 动态注入当前 aCLI catalog 命令知识 | ✅ 代码完成 | Prompt 有依据生成，缺失命令仍不被模型侧删除 |
| T-KB-GATE-01 | 建立 `write_signal → not_exists → run_failed → Signal` 分流 | ✅ 代码完成 | 单测覆盖固定优先级和混合候选 |
| T-KB-GATE-02 | catalog 校验复用 SOP 单一实现 | ✅ 代码完成 | KBD/SOP 不再维护两套 catalog 判定 |
| T-KB-GATE-03 | keyword Matcher 每个 pattern 必须能从 Candidate evidence 或合法变量逐项追溯 | ✅ 代码完成 | 脱敏值不得降级为宽泛词，也不得在有证据项旁混入猜测项 |
| T-KB-GATE-04 | catalog 命令最小 argv 运行契约 | ✅ 代码完成 | 裸 smartctl 等已登记但不能运行的调用进入 run_failed；KBD/SOP 复用 |
| T-KB-GATE-05 | acquisition/evidence 一致性门禁 | ✅ 代码完成 | BMC 事件不伪装 qkv_alert；无日志来源/形态证据不伪装 qfk_log |
| T-KB-GATE-06 | 命令能力与 regex evidence 预运行门禁 | ✅ 代码完成 | ipmitool mc info 不采集 RAID 固件；regex 必须命中自身 evidence |
| T-KB-GATE-07 | 修复后只读验证 phase 纠偏 | ✅ 代码完成 | 封闭只读命令不因上下文重启误报 write_signal；真实写命令仍优先拒绝 |
| T-KB-CANDIDATE-04 | 明确 HCI 平台告警逐项召回 | ✅ Prompt 完成 | 每个不同告警至少一个 qkv_alert；已有后台检查不能替代，BMC 外部事件除外 |
| T-KB-CONTRACT-01 | 空 must Contract 确定性兜底 | ✅ 代码完成 | 第一条 diagnostic Signal 同步提升为 must；全拒绝时不生成 Contract；抽取不得 500 |
| T-KB-GATE-08 | 配置文件不得伪装 qfk_log | ✅ 代码完成 | 明确安全配置路径归一 cat；其他配置扩展名 Candidate 进入 run_failed |
| T-KB-GATE-09 | 实际执行向量写动作门禁 | ✅ 代码完成 | command_args 中的明确写子命令/开关及被包装写程序优先进入 write_signal，不被 not_exists 掩盖 |
| T-KB-SCHEMA-01 | `rejected_candidates[].reason_code` 增加三值枚举且保持可选 | ✅ 代码完成 | 新数据可分类，历史快照继续合法 |
| T-KB-UI-01 | 审核页展示三类标签、关注级别、原因与完整 Candidate | ✅ 代码完成 | 专家能区分安全、能力和运行问题 |
| T-KB-PROMPT-DB-01 | seed 升至 v2.1，data migration 021 前向修复已部署 v1.9 | ✅ dev 收敛验证 | 迁移不只追加规则，并以负向断言保证不残留模型侧过滤规则 |
| T-KB-DOC-01 | 事件文档和现行全量文档同步 | ✅ 完成 | docs 治理检查通过 |
| T-KB-VERIFY-01 | 后端专项/完整测试、Ruff、Schema、前端类型与构建 | ✅ 完成 | 49 项聚焦测试、303 项完整测试、Ruff、Schema 漂移和 Admin 构建通过 |
| T-KB-BATCH-00 | KBD30880 连续重抽 5 次回归 | ✅ 5/5 | Proposal 57～61 均有 qkv_task；最新 Proposal 无 Expert 配对，修改数为 0 |
| T-KB-BATCH-01 | 重跑 KBD27079/27173/27222/27653/27736 | ✅ 六轮闭环 | revisions 93～97 正常 Signal 与 write_signal/not_exists/run_failed 均按预期分流，无新增问题 |
| T-KB-BATCH-02 | 重跑 KBD28094/28156/28177/28900/29294 | ✅ 三轮闭环 | revisions 108～112 明确告警、正常后台检查与三类拒绝均按预期分流 |
| T-KB-BATCH-03 | 重跑 KBD29713/30396/30838/30884/32010 | ✅ 三轮闭环 | revisions 122～126 验证配置文件不再伪装 qfk_log；5/5 返回 200，正常 Signal 与三类拒绝独立分流 |
| T-KB-BATCH-04 | 重跑 KBD32300/33510/33882/34094/34164 | ⏸️ 等待外部配额后第二次同批重跑 | revisions 127～131 暴露 command_args 写动作被 not_exists 掩盖；确定性回放已命中 write_signal，但 DashScope 总配额 429，未生成复跑 revision，不得进入下一批 |
| T-KB-BATCH-N | 剩余草稿按每批 5 篇自查、修复、重跑、独立提交 | ⬜ 待执行 | 前一批通过后才进入下一批 |
| T-KB-PR | 汇总本地提交并创建 PR | ⬜ 待全部批次完成 | CI 全绿；标签齐全 |

## 实施约束

1. 每个 Candidate 独立分流；不得因同批其他候选错误而拒绝正常 Signal。
2. 所有拒绝项保存原始 Candidate、稳定 reason code、具体 reason；不得只写计数。
3. 只修改通用 Prompt、Schema、门禁、catalog 或运行契约；禁止 support_id 条件分支。
4. 批次问题未闭环前不进入下一批；若只修改文案且不影响行为，可与该批通用提交合并。
5. 每次行为优化独立本地提交，格式示例：

   ```text
   fix(kbd): 建立 Candidate 三态门禁并修复任务信号遗漏

   [env:dev:sf][agent:codex]
   ```

6. 不改动主工作树中用户已有的 Helm、`uv.lock` 和 hackathon 文件。

## 首批判定清单

| KBD | 审查重点 | 通过条件 |
|---|---|---|
| 30880 | “启动虚拟机失败”任务 producer | qkv_task 是 Signal，不被 write gate 误杀 |
| 27079 | 正常告警、日志、core 文件检查 | 正常 Signal 数量与语义不因新门禁回退 |
| 27173 | BMC/ipmitool 与硬件命令映射 | 未登记的 hardware 命令完整进入 not_exists |
| 27222 | system/hardware/storage catalog 和 Matcher | 未登记命令 not_exists；Matcher 问题 run_failed |
| 27653 | SMART 工具、args、变量消费 | 结构/依赖/编译问题 run_failed |
| 27736 | 脱敏地址 Matcher | 无法现场命中的 Matcher 为 run_failed |

## 完成定义

- 代码、Schema、Prompt、迁移、UI 和文档在同一 PR；
- 自动测试与 dev 实际重抽均有证据；
- 每批记录输入 revision、Prompt version/hash、模型、Signal/Rejected 数量和逐条结论；
- 所有剩余 KBD 完成后再创建最终 PR，不以“部分批次通过”宣称任务完成。
