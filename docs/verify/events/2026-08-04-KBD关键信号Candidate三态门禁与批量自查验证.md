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
| 单元 | 脱敏 Matcher、keyword 伪正则、exists 携带 pattern | `run_failed` | ✅ 已覆盖 |
| Schema | 新 rejected candidate 带三值 reason_code | 合法 | ✅ 已通过 |
| Schema | 历史 rejected candidate 无 reason_code | 仍合法 | ✅ 已通过 |
| Prompt | 兼容 key `signals` 输出完整 Candidate，不含模型侧“不得输出”回归规则 | 合法且占位符集合不变；Prompt/服务任一先升级不产生空 Proposal | ✅ 已通过 |
| 数据迁移 | v1.9 → v2.1 前向替换、旧规则收敛与幂等断言 | 不改写历史 KBD/revision；不得残留模型侧过滤语句 | ✅ hci-dev 实际升级至 v2.1，并通过正向/负向断言 |
| 前端 | 三类标签与 TypeScript | 类型检查、构建通过 | ✅ Admin 生产构建通过 |
| 后端 | kb-service 完整测试与 Ruff | 全绿 | ✅ 303 passed；Ruff 通过 |

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

migration 021 随后改为收敛迁移：替换上述全部已知反向指令，并增加负向断言。KBD30880 五次回归期间使用的 Prompt SHA-256 为 `268e2f960e3fc80117cd4a8f72650f4e76e88177bac21aaceb27f26f8357101a`；旧规则 24、“宁缺毋滥/不产出”和任务下游前置条件均不存在。第一批又发现脱敏值被模型降级成宽泛 matcher，修订后 hci-dev Prompt 仍为 2.1，MD5 为 `ecebfe41f13b43e768677cf7d714ac18`，SHA-256 为 `432155ecb4ca218df6b395627491cef5397bf3ad37615b14aeafb6961f9b03db`。

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

| KBD | 最新方案预期 | 首次结果 | 修复后结果 | 状态 |
|---|---|---|---|---|
| 27079 | 正常告警、日志、系统检查保持 Signal | revision 62：4 Signal；write_signal=1 | 待同批重跑 | 🔄 |
| 27173 | 不存在 hardware 命令进入 not_exists；若正确映射 ipmitool 则正常通过 | revision 63：2 Signal；重复无人消费 producer → run_failed=1 | 待同批重跑 | 🔄 |
| 27222 | 不存在命令 not_exists；Matcher 问题 run_failed | revision 64：2 Signal；lspci not_exists=2；write_signal=1 | 待同批重跑 | 🔄 |
| 27653 | args/变量/编译问题 run_failed | revision 65：1 Signal；qkv_alert keyword list 与无人消费 producer → run_failed=2 | 待同批重跑 | 🔄 |
| 27736 | 脱敏 Matcher 进入 run_failed | revision 66：4 Signal；write_signal=1；发现 `pattern=address` 宽泛降级误通过 | 待同批重跑 | ❌ 已定位并修复通用门禁 |

首批初跑证明三类分流路径均可见，但未达到整批退出标准。KBD27736 的脱敏 IP 没有直接进入 pattern，模型改用 `address`，只能证明配置存在地址行，不能证明 eth0 与 channel4 冲突。修复同时覆盖两层：Prompt 要求忠实保留脱敏 Candidate，服务端要求 keyword pattern 至少有一项能从 `provenance.evidence` 逐字追溯；否则归入 `run_failed`。该规则不读取 support_id，不依赖 IP 专项硬编码。

每篇逐 Candidate 检查：证据是否来自允许的四个章节；工具与参数是否合理；编译命令是否真实；Matcher 是否能在现场数据上成立；变量链是否可达；正常 Candidate 是否未受拒绝项牵连。

## 后续批次模板

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
