# HCI 智能排障平台 — 项目规范

> **本文件是所有 AI Agent（Claude Code / Codex CLI / Gemini CLI / Antigravity IDE）的项目层规范文件。**
> `./CLAUDE.md` 是引用本文件的薄壳（内容为 `@./AGENTS.md`），确保 Claude Code / Antigravity IDE 读到相同内容。本文件是规范的唯一事实源。
> `./CLAUDE.local.md` 存放个人本地配置（不提交 git）。
> 全局编码规范见 `~/.claude/CLAUDE.md`，全局避坑指南见 `~/.claude/pitfalls/`。

---

## 启动时必读文件

**在开始任何工作前，请优先读取本仓库内的规范文件：**

1. `AGENTS.md` — 项目层规范。本文件是 Claude Code / Codex / Gemini / Copilot 等 Agent 的统一入口。
2. `docs/deploy/pitfalls/_index.md` 或 `docs/verify/pitfalls/_index.md` — 按任务场景读取对应避坑指南索引，再读取具体指南。
3. 涉及 KBD、关键信号、语义推荐或审核发布时，从 [KBD 与关键信号文档入口](docs/solution/knowledge-base/README.md) 按职责读取现行主文档；旧路径仅作兼容跳转，历史方案不作为当前执行规范。

---

## 1. 项目概述

**HCI 智能排障平台** — AI 驱动的超融合基础设施运维故障诊断系统。

- 用户创建工单描述故障 → AI 助手多轮对话引导排障 → 建议命令和操作步骤 → 形成可复用知识库
- **根因信号轨阶段二落地：`kbd_entry.root_signal_json` 生产抽取 + 审核 UI + 全库回刷**（详见 `docs/solution/knowledge-base/根因信号抽取方案.md` §7）：
  - **根因**：关键信号（`signals_json`）只描述「现场可采集的证据」，缺少结论侧「这篇 KBD 到底特殊在哪」的定位锚点，语义兜底路径因此只能按标题/分类粗匹配，同分类下多篇案例互相撞车。若把结论锚点塞进 `signals_json` 混写，会让关键信号的 v2 契约校验器被迫理解结论侧语义，两条轨同时腐化。
  - **修复**：新建与关键信号**完全解耦**的第二抽取轨。①落库：`desired_schema.sql` 新增 `root_signal_json jsonb` + CHECK（只允许 `NULL` 或 jsonb object）+ `match_type`/`needs_review` 两个 partial 索引，并把 `extract_root_signal` 加入 `ck_kbd_batch_job_type` 枚举；该列属 **AI 派生元数据**（同 `ai_category_id`），不入 `kbd_revision`、不动 content_md/embedding/tsv，故写回路径**禁止**使用 `require_mutable_kbd`（否则 published 回刷必撞 409）。②生产链路 `app/routes/extract_root_signal.py`：`POST /kbd/{id}/extract-root-signal`（`?sync=true` 同步、否则 202+job_id）、`GET .../status`（校验 job 的 kbd_id 一致）、`GET /kbd/{id}/root-signal-conflicts`（只读回算 stored/live 冲突度，不回写）；Prompt 一律经 `StrictPromptLoader.load_and_validate` **每次热加载**（零缓存，契约不符即抛错、绝不静默降级），`prompt_revision = "sha256:"` 取自模板散列并写入产物；LLM 调用走 `call_with_llm_governance` 舱壁 + `observe_llm_generation` + `KBD_LLM_REQUESTS_TOTAL/TOKENS_TOTAL`，`max_retries=0` 且单次超时小于网关 240s。③门禁 `app/services/root_signal_extractor.py`：单信号门禁、executable 六类拒收、text 关键词扁平/去重/纯数字拒收、违规**回喂重试**、失败不写库也不清空既有信号；冲突度**不采信 LLM 自报**，一律用 `ConflictCorpus.conflicts()` 按「同一判定串 ∧ 同一采集具体资源」全库回算。④批量与回刷：`admin.py` 的 `/batch/extract-root-signal` 与 `/batch/extract-root-signal/backfill`（where 含 `char_length(root_cause)>=30`、`root_signal_json IS NULL`、可选分类前缀，并用子查询排除在飞的 pending/running 条目），单批上限 100、内部并发 8，四处枚举同步（`_submit_batch_job`/`_BATCH_PROCESSORS`/`allowed_job_types`/DB CHECK）；根因信号使用**独立** `SignalJobManager(max_concurrent=4)` 实例，避免与关键信号按 `kbd_id` 去重导致拿错 job_id/结果。⑤admin UI：`KbdReviewView.vue` 新增「根因信号（结论侧定位锚点）」面板（唯一性标签/采集方式/匹配关键词/confidence/needs_review 与全库冲突度对照）、单篇抽取按钮与全库回刷弹窗；纯逻辑抽到 `frontend/admin/src/utils/rootSignal.ts`（含与后端同值的 `MIN_ROOT_CAUSE_LEN`）；`PromptManageView.vue` 的 KEY tab 自动枚举新 Prompt 并补 `{category_path}` 占位符提示。
  - **陷阱（种子位置）**：Prompt 种子**必须**放 `database/seeds/02_system_prompts.sql`（Helm **db-seed-job** PostSync 应用）。阶段一曾放 `database/atlas-migrations/`——该目录按仓库规范「仅 Schema」，且 `scripts/db-migrate.sh` 只做 `atlas schema apply --to desired_schema.sql` + data-migrations + `desired_extras.sql`，**从不应用 atlas-migrations**（与 2026-09-04 条目同一陷阱）；放错位置会让生产路由 100% 抛 `PromptLoadError`。故该迁移文件已删除、种子并入 seeds/02，`load_prompt_template_from_sql` 相应改为**按 Prompt 名定位**（同一文件含多个 `$TEMPLATE$` 块，不能取首个）。
  - **测试守护**：`test_root_signal_extractor.py`（28 例：门禁/归一/回喂/golden 端到端/冲突计数口径/种子占位符契约/目录含 args 契约）；`test_extract_root_signal_route.py`（14 例：三端点契约、mock `_call_llm_messages` **显式全参数**而非 `**kwargs` 以暴露契约漂移、`TestCrossLanguageContract` 跨语言常量一致性校验）；网关 `test_p1_authz.py`（匿名 403 证路由已注册非 404 + admin 200 断言转发下游 URL 与 `?sync=true` 透传）；前端 `rootSignal.spec.ts`（14 例，含 `MIN_ROOT_CAUSE_LEN` 钉值断言）。热加载另以 `.local/verify_root_signal_hot_reload.py` 对真实 DB+LLM 四轮实测：原模板→改库即生效（revision 变化、进程未重启）→写坏占位符严格阻断且**既有信号不被污染**→恢复无残留。
- **根因信号人工逐条编辑（v2.1，2026-10-17，详见方案文档 §7.5.1）**：抽取信号不可避免有错（形态误判/关键词不溯/采集参数猜错），故对齐关键信号专家编辑体验，新增 `PATCH /kbd/{id}/root-signal`（保存即**全量替换** root_signal_json 数组：删除＝不含它的数组、新增＝追加、空数组＝清空）。**关键决策**：人工编辑与自动抽取**同标准硬门禁**——复用 `gate_signals`+`normalize_root_signal`（executable 的 `qfk_*` 词表/args 契约/只读安全门+溯源逐字命中；text 的 keyword/regex+逐字溯源），任一不合即整体 422 `ROOT_SIGNAL_NOT_CONFIRMING` 回传 `signal_text`+原因，绝不部分落库；冲突度仍按现场语料实测不采信前端自报；`prompt_revision=manual-edit`；与回刷同理不产生 `kbd_revision`、不走 `require_mutable_kbd`。网关必须同步补 `@kbd_router.patch("/{kbd_id}/root-signal")` 代理（复用 collapse-to-leaf 404 老雷教训：kb-service 新增 admin 可达路由漏代理即「存在但 404」），下游 422 经 `_kbd_json_response` 原样透传。前端 `rootSignal.ts` 抽 `splitKeywords`/`joinKeywords`/`buildEditPayload`（textarea⇄`match.expected`、executable JSON⇄`acquire.args`、text 强制剥 acquire、非法 JSON 就地挡错），`KbdReviewView.vue` 加编辑对话框，成功后先拉新详情再关对话框（遵循「清空/推进展示态须先于成功确认」状态机教训）。测试：后端 `TestEditRootSignal` 6 例 + 网关匿名 PATCH 403/admin 转发命中 + 前端 buildEditPayload 四态。
  - **CI 首轮真实暴露的两处（本地全量测试覆盖不到的拓扑差异，详见方案文档 §7.7）**：
    - **`ck_kbd_batch_job_type` 有两个事实源，extras 才是现行赢家**：`db-migration-test.yml` 的执行序列是
      `extras → atlas apply(desired_schema) → extras`，而 `database/desired_extras.sql` 对该 CHECK 做**无条件
      DROP + ADD**，总会覆盖 atlas 刚写好的定义；只改 `desired_schema.sql` 会使「apply 后仍有 diff」而幂等门禁失败。
      规则：**凡 extras 里无条件重建的对象（尤其枚举型 CHECK），扩值必须 `desired_schema.sql` 与 `desired_extras.sql` 同改。**
    - **前端单测不得跨语言读后端源码**（已登记避坑 `docs/verify/pitfalls/frontend.md` V-023）：「前端检查」作业用
      `sparse-checkout: frontend`（作业注释明写「前端门禁不消费后端源码」），仓根只有 `frontend/`，所以
      “从 `process.cwd()` 向上找 `backend/` 再 `readFileSync`”一类用例本地全绿、CI 必炸。跨语言常量一致性校验
      必须放到**全量检出**的后端测试（`TestCrossLanguageContract` 读 `rootSignal.ts` 比对 `route.MIN_ROOT_CAUSE_LEN`），
      前端侧只留钉值断言；该后端守护已用变异测试（把前端阈值改 31 → 该例失败）验证不是空跑。
      **教训：「本地全量测试全绿」不等于 CI 拓扑下可跑，写跨目录读写/幂等断言前必须先核对该 CI 作业的 checkout 与执行序列。**
  - **v2 多信号重设计（推翻上述 v1 具体描述，以本条为准；详见方案文档 §10）**：内部评审三要求——
    ①**不限条数**：`root_signal_json` 由 v1 单对象改为 **v2 顶层 JSON 数组**，`desired_schema.sql` CHECK
    改为 `jsonb_typeof='array'`，v1 两个标量 partial 索引（`->>'match_type'`/`provenance->>'needs_review'`）对
    数组失效→换 **GIN `idx_kbd_entry_root_signal_gin`（`jsonb_path_ops`）**；`gate_single_signal`→`gate_signals`
    （接受 1..N、按 signal_text 去重、封顶 max_signals=8），**部分成功**：单条门禁失败只丢该条记 `gate_errors`。
    ②**输入放开整篇**：抽取输入由仅 `root_cause` 改为整篇 `content_md`（缺失回退章节拼接），Prompt
    重命名 `kbd_extract_root_signal_v1`→`_v2`、占位符 `root_cause`→`kbd_content`（`ROOT_SIGNAL_PLACEHOLDERS` 同步）；
    ③**只抽能确证根因的信号 + 溯源分层 `trace_tier`**：evidence 逐字命中 `root_cause`＝主锚（高信任）、
    仅命中整篇其余章节＝`content` 兑底（**强制 `needs_review=true`**）、整篇溯不到＝拒；主锚优先。
    **已废除 `MIN_ROOT_CAUSE_LEN` 跨语言契约**（输入放开整篇后根因短不再阻塞）：前后端钉值、`extractBlockReason`
    与 `TestCrossLanguageContract` 均删除，避坑 V-023 改写为退位记录；回刷 where 不再含 `char_length(root_cause)>=30`。
    admin UI：`rootSignal.ts` 新增 `parseSignals`（数组/旧对象兼容），`KbdReviewView.vue` 单卡片→**信号列表**。
    实测锁定策略（n=100×3）：S1 主锚确证命中 96.9%/噪声 3.1%/唯一定位 90.8%，S2/S3 命中 65.5%/噪声
    34.5%（S2 增量噪声 52.7%）→ 采纳选项 B。测试：`test_root_signal_extractor.py` 36 例 +
    `test_extract_root_signal_route.py` 13 例，后端 680 passed / admin 162 passed + `vue-tsc --noEmit` 无错。
- **根因信号运行时两轨消费（PR B，#2，2026-10-17，详见 `docs/solution/knowledge-base/知识库设计.md`《根因信号运行时两轨消费》与 `docs/solution/架构设计.md`）**：把根因信号接入运行时，按 `match_type` 分两轨。**轨道一（executable）**：`playbooks._signals` 返回 `normalize(signals_json) ∪ [normalize_root_for_runtime(r) for executable 根因]`，与关键信号**同等执行/判定**（默认 required/MUST，复用全部 CDD 链路，不新增判定维度）；**轨道二（text）**：`serialized_kbds.root_display_anchors`→`KBD.root_display_anchors`→`kbd_differential.py` 渲染「根因特征（请人工核对）」小节，**不进 signals/门禁/召回**（无 acquire、关键词校 KBD 静态原文，是结论侧描述，区别于 `qkv_case_context`）。**核心难点（转换而非透传）**：根因简单 `match={type,mode,expected:[关键词]}` ≠ 运行时 CDD Matcher 契约（运行时读 `pattern`、`expected` 为布尔、强制 `extract`，缺 extract→`evaluate_matcher` 直返 `matched=None`→UNKNOWN、KBD 永不 supported），故 `_root_match_to_cdd`：单词/any→keyword matcher；**多关键词 all**（允许跨行）→ **lookahead 正则** `(?=.*kw1)(?=.*kw2)`（regex 分支以整段 stdout+`re.DOTALL` 跨行求值，逐行 keyword and 只认同行无法表达）；`mode` 显式翻译 all→and/any→or（原样透传 all 会落 else→or 误判）；数值型无法无损还原则保守丢弃（试点 executable 根因 100% keyword）。**第二难点（仅根因不可执行退化）**：`_execution_issues`/`diagnosis_capability`/`recommendable` 原先读空原始文档→`is_reference_only` 将仅 executable 根因的 KBD 误判 reference_only→永不进 CDD（违背轨道一）；修：非语义 live 路径均以「关键∪根因」并集文档判能力。**语义边界**：Phase 1 语义/frozen 路径不并入 live 根因，保持快照可复现。**收紧风险**：并集只影响携带 executable 根因信号的 KBD（“两者都有”需全部 SATISFIED），当前评估语料/种子 KBD 均无根因信号→并集为恒等、定论率不漂移；若过度收紧再议 supporting 降级开关。测试：`test_playbooks_root_signal_runtime.py` 37 例 + `backend/shared/tests/test_root_signal_runtime_cdd.py` 5 例（AND 收紧/anchors 零 SignalRef/无根因恒等） + `test_kbd_differential.py` 3 例（报告渲染）；离线 golden 回放 0 回归。
- **KBD 关键信号抽取「orchestrate 显式 null 整单全拒」修复（避坑 `docs/verify/pitfalls/python.md` V-026）**：
  - **根因**：Prompt 输出格式写 `"action": "<可选>"`，LLM 把「可选」理解为可显式输出 `null`；而 v2 契约 `orchestrate.action` 是 `{"type":"string"}`（JSON 的 null 与键缺失不同值，键存在即拒）。平台已有的「显式 null→缺失」归一 `normalize_optional_matcher_nulls` 只覆盖 `match` 段，`orchestrate` 段是盲区（且旧实现对 `match: null` 的 qkv 信号直接 continue 连累同信号 orchestrate 被跳过）；叠加 PR #1159 的 `SIGNAL_ALL_REJECTED` 收口，每条候选必死→整单 422，`retryable=True` 只引导用户徒劳重试（KBD 16368 实锤 15 次全灭；48h 日志 61 条被拒候选全带 `orchestrate.action=null`，而全库已保存 14 条带 action 键信号 0 条 null，证明是系统性契约缝隙非坏候选个例）。另 `humanize_signal_validation_error` 的 `type` 分支裁掉 `field_path` 与原始原因，文案「执行编排的值类型不正确」无法定位。
  - **修复**：①同一归一函数扩展覆盖 orchestrate 的字符串槽位 `phase/action/source/target/container`（null→缺失，缺失语义由抽取链路默认值如 `phase=diagnostic` 补齐；抽取/保存/发布入口共用该边界自动全覆盖），数组结构位 `produces/requires/output_processing` 的 null 属真实错误不归一；match 与 orchestrate 两段归一改为互相独立。②`type/anyOf/oneOf` 拒收消息改为携带字段路径+实际值+期望类型（中文渲染，不泄漏 Python repr/Schema 分支列表）。
  - **测试守护**：`test_signal_text_extract_schema.py` 新增 4 例（KBD 16368 实锤形态归一后可过契约/phase=null 归一/produces=null 保留如实拒绝/type 消息含 field_path+实际值+期望类型）。教训：凡「可选但只接受具体类型」的字段必须预设 LLM 直出 null 并在保存边界确定性归一，不能指望 Prompt 措辞或重试。
- **分类树拖拽重排即时物化 code（解A「顺序即序号」落地）**（详见 `docs/solution/admin-ui/events/2026-10-11-分类树拖拽重排即时物化code方案.md`）：
  - **根因**：解A 铁律「同级顺序即 code，无独立 sort 字段」下，`CategoryManageView.vue` 的 `sortTreeNodes` 每次 refetch 按 `code.localeCompare` 重排；而 `handleNodeDrop` 只发 `{parent_id}` 不改 code、不带位置 → 拖出来的视觉顺序与 code 无关，刷新即被拍回原形，表现为「拖拽完全无效果」。
  - **修复**：`category_repo.py` 新增 `resequence_domain`（前序 DFS 按目标父新兄弟序整域重编号，单事务内用唯一临时码 `__tmp__||id` 规避置换碰撞 + 摘/重建 FK·CHECK + remap **全库按 code 引用分类的列**（`kbd_entry.category_id`/`ai_category_id`、`sop_document.category_id`、`conversation.category_id`、`offline_signal_collector_mapping.category_scope`、`diagnosis_candidate/run/session` 的 code 列，共 10 列；存 label/域名的列排除）+ 悬空仅对有 FK 列置 NULL）；`category_service.py` 新增 `update_parent_and_resequence` → 下沉 `category_repo.py` 的 `move_and_resequence`（**单事务原子**：换父 `_do_update_parent` flush 不提交 + 重排 `resequence_domain(ext_session=)` 复用同一 session，**唯一 commit**，任一环节失败整体回滚，消除旧版两步分提交的“换父成功但顺序未物化”部分成功态）；`categories.py` `CategoryParentUpdateRequest` 扩 `sibling_order`、返回 `resequence`；前端 `handleNodeDrop` 读目标父 `childNodes` 新兄弟 code 全序下发，重排后按稳定 `id_in_db` 复位选中。
  - **前端展开态持久化（修复拖拽/点击后折叠分支被全开）**：主树原写死 `default-expand-all`，任何 refetch 重建 `:data` 或右侧 `v-if`/`v-else` 卸载重挂载都会强制全展开。改为 `node-key` 用稳定 `id_in_db`（重排只改 code 不改 id）+ `:default-expanded-keys="expandedKeys"`，`@node-expand`/`@node-collapse` 实时维护；`expandedKeys` 跨重取/重挂载永不重置 → 没动的分支保持原样；首次加载仅展开域根。
  - **展开/折叠事件回写空操作修复（折叠分组再展开兄弟项时被同步重弹）**（PR 承接 #1136/#1138）：
    - **根因**：Element Plus 2.13.5 的 `node-expand`/`node-collapse` 事件签名为 `emit('node-collapse', node.data, node, instance)`——**第一个实参是节点 data 对象本身，非 Node 实例**。而 `handleNodeExpand`/`handleNodeCollapse` 误按 Node 实例取 `arg?.data?.id_in_db`（即 `data.data.id_in_db`，`KbCategory` 无 `.data` 字段）恒为 `undefined`，两处理器沦为空操作：手动展开/折叠从不写回 `expandedKeys`。于是选中某分类经 `expandAncestorsOf` 并入的 key 在「折叠」后仍**残留**在数组里，一旦后续任何操作改变 `expandedKeys` 引用（如点击其他项触发 `selectCategory`），el-tree 的 `defaultExpandedKeys` watcher 调 `setDefaultExpandedKeys` 会把数组内全部 key（含残留的已折叠分组）重新 `expand`，表现为「折叠 A→展开兄弟 B/点击其他项，A 又自动展开」。
    - **修复**：新增纯函数 `useCategories.resolveCategoryNodeKey(data)`（读 `data.id_in_db ?? data.id`，非对象/空值返回 `null`，`id_in_db=0` 视为有效），`handleNodeExpand`/`handleNodeCollapse` 改从事件第一个实参（data）正确取 key 写回/移除 `expandedKeys`，令折叠真正生效、消除残留重弹。测试守护：`useCategories.spec.ts` 新增 5 例覆盖取值/回退/0 值/空值/「折叠后展开兄弟不残留重弹」回归链路。
  - **基线文件与 DB 同步（v2.0 全树快照，消除拖拽不回写导致的漂移）**：确立「DB 为唯一事实源、`category_baseline.yaml` 为只读快照」。统一方言为 **v2.0 全树**（`code/name/domain/level/parent/path`，含域根与分组节点），**单一事实源生成器** `backend/shared/utils/category_baseline.py::render_baseline_yaml` 被 `/export`（`_generate_export_yaml` 由仅叶子改产全树）与 `scripts/refresh_category_baseline.py` 共用；`/import` 新增 `_parse_baseline_fulltree`（**照抄显式 code、绝不重编号**，父关系仍 Phase 2 按 `path_labels` 反查）+ `import_from_yaml` 首条含 `code` 自动识别格式（含 `id` 仍走 v1.0 向后兼容）；`verify_kb_category_tree.py::load_baseline_leaves` 改为只读 v2.0 `code` + 按结构判叶（不再兼容 v1.0 死代码）；`database/desired_schema.sql` 新增 CHECK `kb_category_code_format`（钉死 code 仅为 `NULL`/`{域}-L1`/`{域}-{纯数字}`，但对 `domain='测试'` 的合成/隔离分类（E2E `semantic-e2e-prepare.py` + 2 集成测试的多连字符码）予以豁免，不稀释生产不变量）。闭环：拖拽物化 code → `refresh_category_baseline.py` 重生成并提交基线 → `verify_kb_category_tree.py --strict` 校验零漂移（**不做拖拽时自动写盘**）。测试守护：`test_export_yaml_emits_full_tree_v2`/`test_parse_baseline_fulltree_preserves_explicit_codes`/`test_parse_baseline_fulltree_rejects_bad_inputs`/`test_baseline_render_parse_roundtrip_lossless` + verify/refresh 两脚本 `--self-test`；live DB 已验：全库 317 行 0 违规可安全加 CHECK，重生成 316 节点 `--strict` error=0/warning=0，往返 0 错误 code/path 全保留。
  - **测试守护**：`test_categories.py` 新增 `test_resequence_materializes_group_adjacency`（组随子树相邻、序号连续）、`test_resequence_noop_when_already_canonical`（合规即 changed=0 不写库）、`test_resequence_rejects_sibling_order_mismatch`（`sibling_order` 缺项即抛错）、`test_resequence_remaps_all_code_referencing_columns`（断言 remap 覆盖含 `category_scope`/诊断列在内的全 10 列、且悬空清理不越界触碰含通配 `*` 的 `category_scope`）、`test_move_and_resequence_rolls_back_when_resequence_fails`（重排失败 `commits==0`，断单事务回滚）、`test_move_and_resequence_commits_once_on_shared_session`（成功 `commits==1` 且重排复用同一 `ext_session`）。
- **分类叶子绑定治理：错挂分组的 KBD 重分类 + 写入门禁防回潮**（承接解A，详见 `docs/solution/knowledge-base/events/2026-10-11-分类基线命名规范治理与叶子统一(解A)方案.md` §8）：
  - **根因**：解A 规定 KBD/SOP 只能挂叶子（结构 `NOT EXISTS(parent_id)` 判定），但历史上 85 条 KBD 仍绑在多子分组上（`虚拟机-009` 迁移组 83、`虚拟机-049` 网络组 1、`虚拟机-065` 软件组 1；注 `虚拟机-015` 是叶子非分组），脱离叶子检索锚点。上一版 `review_required/042_kbd_reclassify_off_groups.sql`（LLM 逐条重分类）从未应用且含 42 条把 KBD 又绑回分组的缺陷，属地雷产物。
  - **修复**：删除缺陷 042 及其 CSV，改为规则化、幂等、跨环境可移植的 `review_required/042_rebalance_group_bound_kbds.sql`（唯一调用链 `category_rebalance_group_bound_v1`）：用有序 UPDATE 在 SQL 内复刻关键词规则（跨集群/集群内×冷/热×跨/同存储）把 85 条重挂到 009→010~015、049→050~053、065→066~069 叶子；顺带修正 049 命名错配（名为勒索病毒实为网络分组→更名「虚拟机网络异常」+ 递归重建 `path_labels`）；内置 DO verify 门禁（仍有已发布 KBD/SOP 绑非叶子即整体回滚）。
  - **写入门禁防回潮**：`category_repo.is_category_leaf(session, code)`（沿用 NOT EXISTS 单一判据）在 `admin.approve_kbd_entry`（KBD 发布 `effective_category_id` 落库前）与 `sop_ingest.ingest_sop_document`（SOP 建/改/upsert 三处 `category_id` 写入前）拒绝绑定不存在码或分组节点，返回 422（`KBD_CATEGORY_NOT_LEAF`/`SOP_CATEGORY_NOT_LEAF`）。仅改 kbd/sop 的 `category_id`、不改 `kb_category.code`，外键始终满足。基线 `category_baseline.yaml` 同步 049 名与 049~053 path。测试守护：`test_category_leaf_guard.py`（叶子/分组/不存在三态）；迁移以 live staging 回滚式 dry-run 实测分布 010:3/011:27/012:50/015:3/052:1/068:1=85、零残留。
- **分类树「合并子分类到本节点（折叠为叶子）」原子操作**（详见 `docs/solution/admin-ui/events/2026-10-12-分类树合并子分类折叠为叶子方案.md`）：
  - **根因**：运维想「删掉某分组下的 L4 叶子、让分组本身变叶子」。但 `kbd_entry/sop_document.category_id` 对 `kb_category(code)` 是 `ON DELETE NO ACTION`（不 CASCADE/不 SET NULL），解A 又要求 KBD/SOP 只绑叶子（`is_category_leaf` 422 门禁）+ 已发布 KBD 受 `require_mutable_kbd` 409 写保护——故「先批量把叶子 KBD 设为父（父此刻仍是分组）→ 再删叶子」的两步 UI 流程必然撞墙，且会留下「KBD 挂分组」脏态。
  - **修复（原子折叠，等价 `review_required/043` 迁移的产品化）**：`category_repo.py` 新增 `collapse_subtree_to_leaf(code)`——单事务内 ①父存在性+域根拒绝 ②递归 CTE 取全部后代（空则幂等 no-op 不提交）③全库 10 个按 code 引用列（`_CATEGORY_CODE_REF_COLUMNS`，与 `resequence_domain` 同事实源）后代码→父码改绑 ④单条 `DELETE ... WHERE id IN (SELECT id FROM d)` 删后代 ⑤递归重建 `level`/`path_labels` ⑥verify 门禁（父确成叶子 + 全库零悬空，否则抛 `ValueError` 回滚）⑦唯一 commit；`category_service.collapse_to_leaf` 成功后刷缓存；路由 `POST /api/kb/categories/{code}/collapse-to-leaf`。前端 `CategoryManageView.vue` 仅对分组节点（`data.children?.length`）在主树+分支树两处操作区显示「合并子分类到本节点」按钮（`FolderDelete`），点击**不直接执行**，先弹 `ElMessageBox` 详述影响面（删除数/引用改绑/转叶子/语义粒度丢失+待删清单）并二次确认；纯计数逻辑抽为 `useCategories.collectSubtreeInfo` 便于单测。
  - **网关代理链路（首版遗漏致 404，已补）**：admin 前端 `/api/kb/categories/*` **不直连 kb-service**，而是经 api-gateway `routes/kb.py` 的 `categories_router`（prefix `/api/kb/categories`，`Depends(require_admin)`）**逐条枚举**代理（`/{code}`、`/{code}/parent`、`/{code}/hit`、`/import` 等）。首版仅在 kb-service 加了端点、**漏加网关代理路由**，使浏览器点击「确认合并」时网关返回 FastAPI 风格 `404 {"detail":"Not Found"}`（直连 pod 能命中、经网关 404 即此症状）。修复：`routes/kb.py` 新增 `@categories_router.post("/{code}/collapse-to-leaf")` 以 `_internal_auth_headers()` 代理到 kb-service 同名下游（无请求体）。**教训：kb-service 新增任何 admin 前端可达路由，必须同步在网关 `categories_router`/对应 router 补代理，否则端点“存在但不可达”。**
  - **确认框宽度**：`ElMessageBox` 传 `customStyle:{width:'640px',maxWidth:'92vw'}` 放宽弹框、减少正文换行。
  - **折叠后整域重排（消除 code 编号空洞，承接解A）**：首版 `collapse_subtree_to_leaf` 只「改绑+删后代+重建 level/path」，**未调 `resequence_domain`**，导致删后代后同级 code 留下编号空洞（如折叠 `009` 后 `006` 兄弟显示 `007/008/009/016`、`010~015` 消失却不回填），违反解A「顺序即 code、连续无空洞」。修复：在同一事务、verify 通过后、唯一 commit **之前**复用 `resequence_domain(domain, parent_id=None, sibling_order=None, ext_session=session)`（`sibling_order=None` 不覆盖任何父序、仅按当前相对顺序整域 DFS 连续重编号 + remap 全库 10 列），折叠与重排共享唯一提交点，任一失败整体回滚；commit 后按稳定主键回取新码返回。**教训：凡改变分类存在性/顺序的写操作都必须在同事务内走 `resequence_domain` 物化 code，否则编号出现空洞即破解A 不变量。**
  - **折叠父节点被重弹修复（点 `006` 箭头无反应）**：树用 `:default-expanded-keys="expandedKeys"`（`node-key=id_in_db`）维展开态。Element Plus 的 `default-expanded-keys` watcher 在数组变化时会对每个 key 调 `node.expand(null, autoExpandParent=true)`——**展开子节点会向上冒泡自动展开父**。当选中了 `006` 子树内的节点（如刚折叠的 `009`）、其 key 残留于 `expandedKeys`；旧 `handleNodeCollapse` 仅移除父自身 key，watcher 重跑仍会对残留子孙 key 冒泡把父重新弹开（`020` 无被钉子孙故正常）。修复：`useCategories.ts` 新增纯函数 `collectSubtreeKeys(node)`（递归取自身+全子孙 `id_in_db ?? id`），`handleNodeCollapse` 改为折叠时把**整棵子树 key** 一并移出，令 watcher 无可冒泡的残留子 key。
  - **测试守护**：后端 `test_categories.py`（父不存在不提交/已叶子幂等 no-op/成功 `commit==1`+10 列改绑+**整域重排**（断 `ext_session is session`/`parent_id is None`/`sibling_order is None`）/悬空检出回滚/**重排抛错不提交回滚**）；前端 `useCategories.spec.ts`（`collectSubtreeInfo` 3 例 + `collectSubtreeKeys` 4 例含「009 回归：子孙 key 必入折叠集」）；网关 `test_p1_authz.py` 2 例（匿名 POST `/{code}/collapse-to-leaf`→403 证路由已注册非 404、admin 令牌→200 且断言转发下游 URL）。
- **工单 Q2026100812343「硬盘坏道」SOP 编排执行门禁加固与 terminal_bridge 可观测性增强**（详见 `docs/solution/agent/SOP编排执行门禁约束设计与需求.md`、`docs/solution/terminal-bridge-observability-enhancement.md`、避坑 `docs/verify/pitfalls/dispatcher.md` V-017）：
  - **根因**：SOP 引擎变量自动取值链路 `engine.py` 的 `tool_call` 策略调用 `tool_executor.execute(...)` 时**未透传 `conversation_id`**，`BridgeRelayExecutor` 因缺上下文全线静默失败；而泛化 `except Exception` 又把真实原因吞成笼统错误。变量未解析的 `tool_call/skill_call` 仅作为 `soft_hints` 不硬阻断（`is_guarded` 只覆盖 `env_injection/user_input/user_confirm`），于是引擎把控制权交回模型，模型在客户生产环境即兴手工敲 `acli_exec/bash_exec/qkv_*`，并在关键变量（`disk_dev` 空、`check_meth` 未取得）未确认时产出带幻觉的定量结论（臆造 `8001563222016 字节`）。
  - **修复**：
    - **A 取值链路**：`engine.py` `tool_call` 显式透传 `conversation_id`；新增 `_classify_acquisition_error`/`_classify_tool_result_error` 按异常类型分流精确 `error_type`（contract_error/tool_capability_missing/timeout/node_unreachable/executor_unavailable/tool_error），不再伪装成笼统取值失败。
    - **B 守卫门 + 失败即接管**：`nav.py` `_find_missing_guarded_variables` 用 `parse_strategy` 归一后把 `tool_call/skill_call` 从 `soft_hints` 提升为 `hard_blocked`（对裸工具调用返回 `sop_variable_gate_blocked`→`next_tool_call=sop_request_variable`）；`engine.py` `env_injection/derived/skill_call/json_extract` 失败分支删除 `_should_fallback_to_user_input` 门，一律 `_request_user_input(...)` 转人工弹框，绝不交回模型。
    - **C 终局反幻觉门**：`nav.py` 新增 `_is_conclusion_node`，`sop_advance` 进入 solution/叶结论节点时把当前节点窗口未解析声明变量并入阻断集，返回 `sop_conclusion_blocked_unresolved_variables`（禁止数值结论+转人工）；`react_engine.py` 终局检测升级为：结论含 `ungrounded_numbers`（无证据来源数值）也强制触发溯源 re-run。
    - **D/E terminal_bridge 观测**：`main.go` `ExecResult` 加 `ErrorType`；新增 `classifyExecFailure`（jq ONIGURUMA/command not found→`tool_capability_missing`）；标记模式结果消费者以 ERROR 级落 `error_type`；新增 `runUnreachabilityProbes`（TCP:22/SSH 握手/bridge `/health/live`/`acli --version`/jq ONIGURUMA/lsblk，各自 `event=probe.*` 结构化日志，用 `context.WithoutCancel` 防 goroutine 被取消）；`upload_failed` 日志补 `capture_id`+`upload_status`。
    - **F 环境/存量数据**：`main.py` `CompositeToolExecutor` 补 `qkv` 分类路由到 `qkv_exec`（原落到「无对应执行器」）；数据迁移 `database/data-migrations/040_sop_disk_badtrack_align_and_fallback.sql` 以 `variable_schema` 为事实源重编译 SOP#4 `tree_json` 的 `source`（消除残留中文别名「工具调用」），为链路根变量（`alert_logs/node_ip/asan_disks/disk_dev`）声明 `fallback_strategy="user_input"`。
  - **测试守护**：`engine` 单测覆盖 `tool_call` 必须携带 `conversation_id`（缺失即 `contract_error`）、异常 `error_type` 分流、失败转人工；门禁单测覆盖 `tool_call/skill_call` 未解析时裸工具被 `sop_variable_gate_blocked`、`sop_advance` 结论节点被 `sop_conclusion_blocked_unresolved_variables` 阻断；`terminal_bridge` Go 测试覆盖 `classifyExecFailure` 分类与 `probe.*`/`upload_failed` 结构化字段。
  - **增量加固（后续 PR，补齐上条遗漏项）**：
    - **`device_absent` 精确分类落地**（原 V-017 §4 只写未实现）：`engine.py` `_classify_acquisition_error`/`_classify_tool_result_error` 新增 `_is_device_absent`（`No such device`/`cannot open /dev/`/`设备不存在` 等，早于工具能力与退出码判别，区分“盘没了”与“命令没跑成”）；`terminal_bridge/main.go` `classifyExecFailure` 增 `device_absent` 分支，`runUnreachabilityProbes` 新增 `deviceAbsentFromProbe`（命令引用的 `/dev` 盘符与 `lsblk` 整盘清单交叉比对，全部未命中即产出 `probe.conclusion=device_absent`）。
    - **采集镜像带 OnigURUMA 的 jq**（原 V-017 §6 只写未做）：`hci_sim/Dockerfile` 运行层 `apk add --no-cache jq`（Alpine jq 默认链 oniguruma），使模拟/采集环境 `jq test()/match()` 可用；并在 §6 明确“真实客户节点 jq 不受本仓库构建控制，只能探测+归类+下推 `-k` 规避”。
    - **反幻觉由 re-run 升级为硬拦截**（原 V-017 §3.3 “缺证据即拦截”仅 re-run+追注）：`react_engine.py` 新增 `ungrounded_conclusion_block`/`CONCLUSION_MARKERS`/`UNGROUNDED_CONCLUSION_BLOCK_TEXT`，在**流式输出前**将“结论语境 + 无溯源数值”的定量结论整段替换为不含数字的受控“该步受阻、已转人工确认”文本，并记 `hallucination_hard_blocked_ungrounded_numbers` 错误事件与指标。
    - **失败即接管维持 pending 回归**（原 V-017 §3.2 依赖既有机制、无显式测试）：补 `engine` 单测验证“变量处于 pending 且无值时再次请求仍转人工、不伪造 ok；人工补值后缓存命中续跑”。
    - **测试守护**：新增 `test_sop_variable_acquire_guard.py`（device_absent + 维持 pending）、`test_react_engine_hallucination_block.py`（硬拦截）、`probe_and_classify_test.go`（device_absent 分类 + lsblk 交叉探测）均通过；设计文档 §3.3/§4/§5/§6/§8 同步更新以保持单一事实源。
- **terminal_bridge 桌面端口冲突自愈（主 9999 + 备 47324）**：
  - **根因**：客户 Windows 桌面环境端口 `9999` 被占用时，`terminal_bridge/main.go` 的 `main()` 中 `server.ListenAndServe()` 返回 EADDRINUSE 直接 `log.Fatal` 崩溃退出——无预检、无占用者识别、无友好诊断、无回退；最常见的「上一个 Bridge 没退净/双击重复启动」被当成陌生错误崩溃，表现为“双击打不开”。
  - **修复**：
    - Go 端新增 `acquireListener`：主端口 bind 失败后分类处置——`/health/live` 探测到存活 Bridge则**幂等退出**（exit 0）；被陌生进程占用则回退到固定备端口 `47324`（IANA 未分配、低于 Windows 出站动态端口下界 49152）；主备皆占则 **fail-fast**输出占用地址 + best-effort 占用 PID + `netstat`/`lsof` 处置指引并 exit≠0；`listenWithRetry` 指数退避抹平崩溃重启后的 TIME_WAIT；仅默认端口的桌面模式启用回退，显式自定义端口/cluster 只绑该端口不扩散。
    - WS `handle()` 连接即下发 `bridge_hello` 握手首包；新增 `bridge.listening`/`bridge.already_running`/`bridge.backup_port_activated`/`bridge.port_conflict` 结构化日志事件。
    - 前端 `frontend/customer/src/api/terminal.ts` 新增 `resolveBridgeUrl`：桌面模式探测 `9999 → 47324` 两个已知端口，以收到 `bridge_hello` 回包为准判定“对端确实是 Bridge”（不能只看 WS 能否 open，避免误连陌生进程），命中即缓存；`checkBridgeRunning`/`checkBridgeBeforeOpen` 接入并在失效时清缓存；cluster 注入配置零探测、nginx/helm/docker 单端口零改动。
    - 测试守护：`terminal_bridge/port_recovery_test.go` 覆盖占用判定/退避重试/存活识别/幂等退出/主备回退/自定义端口不扩散/fail-fast；`terminal.spec.ts` 新增双端口探测 6 例并保留 `getBridgeUrl` 默认回归。
- **工单 Q2026101496655「提交 node_ip 后弹框假报错『请输入值（必填）』」前端状态机修复**：
  - **根因**：SOP 变量卡片 `handleInteractiveFreeText` 在 `POST /interactive-response` 成功（变量已写入 `sop_execution.context_variables`）后先清空输入框、再调 `chatStore.sendMessage(text, {kind:'interactive_response'})` 触发续跑；而 `chat.ts` 的 `sendMessage` 首行守卫 `if (!content.trim() || isStreaming.value) return` 在本轮 SSE 流式期间（长工具链可持续数分钟）把该触发消息**无提示静默吞掉**。于是：①`interactiveSubmitted`（纯按「弹框后是否存在 interactive_response 用户消息」推断）永远为 false，弹框不转「已提交」态；②输入框已被清空 + `isTouched=true` 组合出红字「请输入值（必填）」假报错，诱导用户反复重提（实测 3 次全部后端 200 成功）。本质是「提交结果反馈」与「续跑触发消息」两条通道耦合在同一个静默守卫上。
  - **修复**（纯前端，后端写入链路本身无缺陷）：
    - `chat.ts` `sendMessage` 改为返回 `Promise<boolean>`（守卫吞掉/工单关闭返回 false，真正发出返回 true），消除静默失败；既有调用方不消费返回值保持兼容。
    - `MessageBubble.vue` 新增 `interactiveLocalSubmitted` 本地已提交标志：`/interactive-response` POST 成功即置 true（先于清空输入框），`interactiveSubmitted` 计算属性优先读本地态，不再依赖「后续触发消息是否存在」的脆弱推断；`sendMessage` 返回 `false` 时变量类卡片给 `ElMessage.info`「值已提交成功、诊断进行中」的诚实反馈，`sop_fallback` 被吞掉时给 warning 且不标记已提交（选择需可重做）；`interactiveEvent` 切换新请求时重置本地标志。`handleConfirmRecommendValue` 同步处理。
  - **测试守护**：`MessageBubble.variable-submit.spec.ts` 新增 5 例（流式吞掉仍转已提交+提示/正常发出不重复提示/提交失败不清空输入不标已提交/历史 interactive_response 向后兼容/sop_fallback 被吞可重选）；`chat.interactive.spec.ts` 新增 `sendMessage` 返回值语义 2 例（isStreaming 时 false 且不发请求/正常流 true）。
  - **教训：前端任何“成功后的副作用触发”若经过可能被守卫吞掉的通道，必须先把 UI 状态推进到与后端一致，再尽力触发，且吞掉时必须有用户可感知的反馈；“清空输入框”与“保留已提交态展示”绝不可先于状态确认执行。**
- **工单 Q2026092053425 排障交互卡片无响应与容器执行失败修复**：
  - **根因**：
    - **点击交互无反应与超时取消**：`conversation-service` 转发用户交互确认到 `agent-service` 时漏传内部 `Authorization` Token，导致 401 失败并在前端静默吞咽，最终 Redis 等待 120 秒超时判定为操作已取消；
    - **容器/宿主机命令执行异常**：`terminal_bridge` 缺少容器非交互式伪终端参数 `-d`（报 `input is not a terminal`），且非交互 SSH 宿主机缺省 PATH 未包含 `/sf/bin:/sf/sbin`（报 `acli: command not found`）；
    - **告警已输入仍强制弹出变量填写**：SOP ID=4 变量声明 Markdown 表格列对齐错位，导致 `alert_logs` 被误解析为 `user_input` 且生成悬空依赖 `告警原始数据`。
  - **修复**：
    - `AgentClient` 初始化及交互/恢复请求统一补齐 `Authorization` 头部；
    - 前端 `MessageBubble.vue` 增加统一 `ElMessage.error` 失败反馈与错误捕获；
    - `terminal_bridge/main.go` 在 `wrapContainerCommand` 统一追加 `-d` 与环境 PATH；
    - `backend/shared` 扩展中文策略别名支持，并订正数据库 SOP 4 的 `content_md` 与 `variable_schema`。
- **terminal_bridge 仿真会话跳过 `export PATH` 前缀（打通「硬盘坏道」live E2E 真采集到隔离 hci-sim）**（承接 Q2026092053425 的 PATH 前缀修复）：
  - **根因**：`terminal_bridge/main.go` 的 `wrapContainerCommand` 对含 `acli`/`smartctl`/`container_exec` 的 host 命令统一前缀 `export PATH="$PATH:/sf/bin:/sf/sbin:/usr/sbin:/usr/local/bin"; `（真机非交互 SSH 修 `acli: command not found` 所必需）。但仿真租约会话（`execution_mode=sim-ssh`+`auth_type=lease`）的目标是 `hci_sim` fixture 服务器——它**按 argv 字符串匹配路由、从不 exec 真实二进制**，且其受限 lexer（`fixture.go::Lex`）拒绝一切 shell 操作符（含 `;`）。于是前缀使每条裸命令（约 41B）被包成 107B 复合串 → `policy_denied` → **全线 `exit 126`**，真采集无法回灌，SOP 停在取值步。
  - **修复**（产品级最小改动，真实机器行为零变化）：`wrapContainerCommand` 新增 `simulation bool` 形参，两个 host 命令 exec 处理点（`s.simulation` 在作用域）传入；仅当 `simulation==false` 才加 PATH 前缀，仿真会话下发**裸命令**。容器分支（`container_exec -n ... -d`）不变。
  - **测试守护**：`terminal_bridge/wrap_container_test.go::TestWrapContainerCommandSimulationSkipsPathPrefix`（断 `simulation=false` 保留前缀 / `=true` 返回裸命令 / 容器分支仍走 `container_exec`）。
  - **端到端实证**：在 k3s 集群内用 `golang:1.26.6-alpine` 构建 Pod（挂载宿主机 `~/go/pkg/mod` 离线编译 `GOPROXY=off`）产出含修复的静态 linux 二进制，热替换隔离 bridge Pod 后重跑 live E2E smoke —— `exec.request` 命令为**裸串**（`acli --formatter json alert get -k 磁盘` 等，无 PATH 前缀），`exec.done` `exit_code=0`/`success=true`/`stdout_len` 367·279 真回灌，`asan_disks` 经 `jit_auto_acquire` 写入 SOP 上下文。至此「命令真经 terminal_bridge 下发到隔离 hci-sim 节点真采集输出回灌」全链路验证通过（残余仅 staging 外部模型 skill_call `AI_TIMEOUT`，非本仓代码）。
- **terminal_bridge 仿真会话解容器派发壳内层命令 + live E2E 变量预种/观测合并（「硬盘坏道」端到端首取 GREEN ✅）**（承接上条 PATH 修复，同属 live E2E 真采集链路）：
  - **根因（容器派发壳双包裹 → `fixture_not_found exit127`）**：容器类 SOP 变量（smart_info）命令由 agent 侧 [container_exec.py](backend/agent-service/app/tools/acli/container_exec.py) 生成自包含 POSIX sh 派发壳（`HCI_CONTAINER='x'; HCI_USER_COMMAND='<裸命令>'; … case … esac`），[executor.py](backend/agent-service/app/tools/acli/executor.py) 把 `built_command`（派发壳）连同 `container=vs-cp-manager` 下发；[agent_exec.py](backend/conversation-service/app/routes/agent_exec.py) `pending_context.command = body.built_command or body.command` 转发的正是派发壳，bridge `wrapContainerCommand` 又对其 `container_exec -n vs-cp-manager -c '<派发壳>' -d` **二次包裹** → hci-sim `fixture.go::Lex` 见整段为 tool=container_exec 的畸形 argv → 无匹配 → **`exit127`**。仿真会话（lease `container=host`、hci-sim 无真实容器、只按裸 argv 匹配）本就不该有任何容器包裹。
  - **修复（bridge 产品级，真实容器路径零变化）**：`wrapContainerCommand` 的 `simulation==true` 分支改为 `return extractContainerUserCommand(command)`——识别派发壳、用 `scanFirstShellToken` 解出内层 `HCI_USER_COMMAND` 裸命令下发（内层仍含 marker 则递归再解），绝不再 `container_exec` 包裹、绝不加 PATH 前缀；`simulation==false`（真机）保持原逻辑。`scanFirstShellToken` 正确处置单引号 `'\''` shlex 转义、双引号内 ` $ ` ` ` ` \ ` 换行转义、顶层反斜杠、`;|&<>()` 操作符边界。
  - **变量预种（harness 通用能力，解 flaky LLM 取值）**：`evaluation/live_e2e/driver.py::_seed_sop_variables` + 场景 `seed_variables`——建会话后经 `POST /sop/variable-response`（`status==active` 不校验 pending 直写任意变量）预种 `disk_name`/`disk_dev=sdb`（裸盘符，因 SOP#4 模板硬编码 `/dev/{disk_dev}`），绕开 skill_call(MaaS `AI_TIMEOUT`) 与自然派生(双 `/dev/`)，令「采集→下发→回灌→结论」确定性推进；跨场景通用（任何场景可声明 `seed_variables`），`_` 前缀键作注释跳过。
  - **观测合并修复（harness 假阴性，非产品缺陷）**：`driver.py` 原 `obs.observed_category_code = _scan_terminal_signals(...)` 无条件覆盖——S0 走候选澄清时第 531 行已按用户点选记录正确码，但点选确认后的 SOP 续跑终态事件未必再带 `category_code`，scan 返回 `None` 把它冲掉（`S0_category_confirmed observed=None` 假阴性）。抽出纯函数 `merge_observed_signals`（已确证值非空优先保留，缺失才用扫描兜底，兼容「模型直接确认不经点选」）。
  - **测试守护**：Go 侧 `terminal_bridge/wrap_container_test.go`（`TestWrapContainerCommandSimulationUnwrapsDispatcher` 派发壳解出裸命令且无 shell 操作符 / `TestWrapContainerCommandRealContainerStillWraps` 真机 container≠host 仍 `container_exec` / `TestScanFirstShellToken` 转义边界）；Python 侧 `tests/unit/test_live_e2e_driver.py::test_merge_observed_signals_keeps_candidate_selection_over_none_scan`（点选码不被 None 冲掉 + 兜底/优先四态）。
  - **端到端实证**：集群内重建静态 ELF、热替换 `live-e2e-bridge` 后重跑 smoke（`--mode real --sim --attempts 3`）：容器 smartctl 从 `exit127`→`exit0 out[1101]`（命中 sig-003 fixture），三条命令全部真采集回灌；`多数表决 2/3 → GREEN ✅`（attempt1/3 全绿含 `S0_category_confirmed`/`S1_sop_hit`/`V017_tool_call_collected`，attempt2 为 S0 候选非确定性属预期随机源）。
- **guidance_only 语义画像 KBD 发布门禁放宽**：
  - **根因**：KBD 发布门禁强制要求至少一条 QFK 消费者信号，导致 `guidance_only` 类型的语义画像 KBD（如案例 15936 "使用ISO镜像安装2016提示缺少计算机所需介质驱动程序"）无法发布。这类 KBD 只有 `case_context` 等受限生产者信号，设计上不进入自动执行 CDD，而是通过语义兜底路径请求人工补充证据。
  - **修复**：在 `backend/kb-service/app/routes/admin.py` 的发布验证中，当 KBD 的 `semantic_entry_profile.diagnosis_capability == "guidance_only"` 时，跳过 QFK 消费者信号要求。
- **INCONCLUSIVE 回复死循环修复**：
  - **根因**：`_semantic_guidance_messages()` 中 `manual_evidence_request` 列表无条件全量输出，未检查用户是否已在对话中提供了对应信息。即使用户按请求提供了 ISO 文件名、SHA1、磁盘控制器类型等信息，系统仍逐字重复同一条"请求补充证据"回复，进入死循环。工单 Q2026091523611、Q2026091524854 均复现。
  - **修复**：
    - 新增 `_evidence_request_satisfied()` 方法，使用字符 bigram 匹配策略（无需中文分词）检测用户消息中是否已包含 evidence request 要求的信息；`_semantic_guidance_messages()` 过滤掉已满足的请求项。
    - 新增循环中断机制：快捷路径中统计 guidance marker 出现次数，≥3 轮用户补充后仍无法推进时，输出确认收到 + 建议转人工的消息，不再重复请求。
  - **测试守护**：新增 4 个单元测试覆盖 bigram 匹配、已提供过滤、无关消息不误判等场景。
- **S0 意图识别「已正确识别却报无法识别」JSON 结构化输出改造**（工单 Q2026101493534 复盘；详见 `docs/verify/pitfalls/debugging.md` V-020）：
  - **根因**：旧范式是「Prompt 约定文本标记 + 下游正则抠字」——`triage_agent._parse_intent_result` 的 `confirmed_pattern` 只认字面量「已确认故障分类/故障分类」、`candidate_pattern` 只认行首 ①②③④，该契约对默认模型 glm-5 的输出习惯**隐式过拟合**。切换到 Qwen3.8-Flash 后书写风格漂移（改写「确认分类：」缺「故障」、用 `**粗体**` 包裹分类码、用 `- ` 短横线代替圈号候选行），任一即令正则提取集为空，被静默降级为「无法识别」的**假阴性**。
  - **修复（解耦「模型文本风格」与「解析契约」）**：
    - **主链路**：S0 由 `chat_completion_stream` 改为 `ai_client.invoke(..., response_format={"type":"json_object"})`，Prompt 强制只输出 JSON 对象 `{reasoning, decision, category_code, candidate_codes, clarification_question}`；harness 经 `_extract_json_object` → `_intent_from_structured` 读字段并用权威分类字典 `cat_map` 校验。
    - **多层兜底**：JSON 解析失败 → 剥 ` ```json ` 围栏/截取首个 `{...}` 重试 → 模型无关归一化文本解析（`_normalize_reply` 剥离 `**`/反引号、`_CONFIRM_MARKERS` 兼容「确认分类」同义词、`_BULLET` 兼容 `- * • ①-⑳ 1.`）→ 确定性关键词候选 → 受控降级，故「新代码 + 旧文本 Prompt」也能靠归一化兜底正常工作，向后兼容迁移滞后。
    - **可观测性**：新增 `intent_contract_drift`（模型未按 JSON 契约返回但走了文本兜底），与真实 `intent_parse_failed`（无任何可用分类）区分。
    - **Prompt 三处同步**：`database/seeds/02_system_prompts.sql`、`database/data-migrations/045_s0_intent_structured_output_prompt.sql`、`backend/shared/utils/prompt_loader.py` mock 模板保持一致；模板 JSON 字面花括号必须写成 `{{ }}` 以通过 `str.format`。
  - **测试守护**：`backend/agent-service/tests/unit/test_triage_agent.py` 的 `TestExtractJsonObject`/`TestIntentFromStructured`/`TestNormalizationFallback`，含真实 Qwen 漂移样本（`确认分类：**虚拟机-017 …**`、`- ` 短横线候选）与「契约漂移端到端仍弹确认卡」用例。
- **工单 Q2026101511258「硬盘坏道」S2/S3 假设生成 user_questions 幻觉追问事实源约束**（避坑 V-021）：
  - **根因**：用户已通过 SOP 变量弹框提交 `disk_dev`（后端 [conversation_service.py](backend/conversation-service/app/services/conversation_service.py) `submit_interactive_response`→[sop_execution_repository.py](backend/conversation-service/app/repositories/sop_execution_repository.py) `set_variable` 已写库、清 `pending_variable_name`、状态回 active，前端「已提交」态正确），但下一轮进入 S2 假设生成时，`ReasoningOutput.user_questions`（[shared/models/reliability.py](backend/shared/models/reliability.py)）仍续写出「请正式提交 disk_dev，该槽位仍为 needs_input」并索要硬盘坏道 SOP#4 根本未声明的 `disk_sn`/`disk_type`。本质：`user_questions` 是模型自由编写的字符串列表，**不经任何变量池/门禁校验**，模型从对话历史里 S1 阶段遗留的失败原文（asan_disks 截断、smartctl 报错、「请人工提供该变量值」）中「续写」出需求清单，而非以当前变量池为事实源；现有反幻觉硬拦截（V-017 §3.3）只覆盖「结论语境+无溯源数值」，对「提问语境+无依据需求项」无拦截，仅追加「待验证」标注仍原样抵达用户。
  - **修复（引擎侧确定性过滤为主，Prompt 加固为辅，双层兜底）**：
    - **引擎侧模型无关过滤**：[react_engine.py](backend/agent-service/app/adapters/agents/htp/react_engine.py) 新增纯函数 `filter_ungrounded_user_questions(questions, resolved_variables, declared_variables)`，以「已就绪变量名 + SOP 声明变量名」为事实源，剔除两类幻觉：①命中已就绪变量（重复索取已提交变量）②引用既未声明也未就绪的变量样 token（`\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b`，凭空编造字段）；**带无事实源护栏**：`declared_variables` 为空（`variable_schema` 加载失败）时整体不过滤，避免空 known 误删全部含下划线 token 的合法追问。`ReactEngine.execute` 新增 `sop_resolved_variables`/`sop_declared_variables` 参数，在 `ReasoningOutput` 校验成功后过滤并 `model_dump_json()` 回写 `invoke_result.content`，令后续硬拦截与流式直出均基于事实源一致内容，记 `sop_user_questions_grounded_filter` 日志与 `ungrounded_user_question` 指标。
    - **上下文注入**：[investigation_agent.py](backend/agent-service/app/adapters/agents/htp/investigation_agent.py) `_process_sop_mode` 从 `context_variables` 计算已就绪变量集、从 `kb_client.get_sop_document().variable_schema` 计算已声明变量集，仅 `response_schema is ReasoningOutput` 时拉取，传入 `execute`。
    - **Prompt 加固**：统一输出约束 `s1_react_output_constraint_v1`（S1~S4 共用）追加【已知变量权威】规则，声明「已知变量」列表优先级高于历史失败原文，严禁在 user_questions 重复索取已就绪变量或索要未声明字段；种子 [02_system_prompts.sql](database/seeds/02_system_prompts.sql) 与热更新迁移 [046_update_react_output_constraint_known_vars_authority.sql](database/data-migrations/046_update_react_output_constraint_known_vars_authority.sql)（条件追加、幂等、含校验门禁 `migration:20261015000001`）同步。
  - **测试守护**：[test_sop_user_questions_grounding.py](backend/agent-service/tests/unit/test_sop_user_questions_grounding.py) 7 例覆盖「重复索取已就绪变量丢弃/凭空索要未声明变量丢弃/已声明未就绪变量保留/无变量 token 保留/无事实源护栏幂等/Q2026101511258 三问句端到端只剔幻觉」。
- **硬盘坏道 Live E2E Smoke（L3 跨层契约缝隙回归，发布前 gate）**（避坑 V-022）：
  - **根因**：Q2026101511258 会话控制链路反复挤牙膏（#1149 前端态机 / V-020 S0 解析 / V-021 S2 幻觉追问），每次单点修复各补隔离单测，但「各层各自测、层间契约缝隙无人测」——SSE `event: interactive_request` kind 契约、签名身份头、变量池 `context_variables` 写入时序、引擎侧过滤守卫是否真触发，始终处于盲区，只能靠线上工单暴露。
  - **能力**：`evaluation/live_e2e/`（`asserts.py` 纯逻辑 SSE 解析/交互卡归一化/提问落地性/5 条不变量、`driver.py` httpx 异步驱动建案→SSE→变量交互提交（复刻 #1149「先确认后端落库再触发续跑」时序）→终态、`smoke.py` CLI + `--attempts` 多数表决、`scenarios/disk_badtrack.json`）；`scripts/hci-sim/disk_badtrack_llm_stub.py` 确定性对抗桩把历史坏输出真注入 agent-service 逼守卫触发（实现『破坏缝隙能验红』）；`scripts/verify/live_e2e_disk_badtrack.sh` + `make live-e2e-disk-badtrack` 全栈 compose 编排（端口暴露覆盖 `evaluation/live_e2e/docker-compose.live-e2e.yml` 置于 evaluation/ 下不进 CI/config-contract-check），`trap` teardown。**断言只打结构不变量 + 守卫事件**（`agent_hallucination_detected_total{hallucination_type="ungrounded_user_question"}` 计数增量、`sop/execution.context_variables` 落库），绝不断言逐字文本。
  - **定位**：发布前 gate，不进每 PR CI（依赖 docker+LLM+bridge，慢且非确定性）；端到端实跑须在具备全栈的宿主机执行，运行手册 `docs/verify/evaluation/live-e2e-disk-badtrack.md`。
  - **测试守护（进 CI，脱网）**：`tests/unit/test_live_e2e_asserts.py`（含 `test_evaluate_invariants_red_when_filter_bypassed` 验红）、`tests/unit/test_live_e2e_driver.py`（SSE 分派/终态扫描/已就绪变量纯逻辑）、`tests/unit/test_disk_badtrack_llm_stub.py`（桩产出的 JSON 必须能被真实 `ReasoningOutput` schema 解析）。
- **Langfuse 子路径导航丢失修复（NEXT_PUBLIC_BASE_PATH 补齐）**（PR #1035）：
  - **根因**：Langfuse (Next.js) 通过 Traefik `stripPrefix` 中间件挂载在 `/langfuse` 子路径，但容器内未配置 `NEXT_PUBLIC_BASE_PATH`，导致 Next.js 前端生成的内部链接（页面跳转、tRPC 请求）不含 `/langfuse` 前缀。首页直接访问 `/langfuse` 能命中 Ingress 规则，但点击项目/设置等页面后，浏览器 URL 变为 `/project/xxx` 或 `/api/trpc/...`，脱离 Ingress 路由范围导致导航丢失。
  - **修复**：在 `deploy/helm/hci-platform-obs/templates/langfuse.yaml` 的 `langfuse-server` 环境变量中增加 `NEXT_PUBLIC_BASE_PATH=/langfuse`（subdomain 模式下不设置），使 Next.js 前端生成的内部链接（页面跳转、静态资源、tRPC 请求）自动带 `/langfuse` 前缀。**注意**：Next.js `basePath` 不影响 `/api/` 路由，健康端点始终在 `/api/public/health`，探针路径保持不变。Docker Compose 无需修改（本地 `localhost:13000` 无子路径前缀）。
- **语义入口画像级联删除与切换清理提示**（PR #1027, #1028）：
  - **背景**：`semantic_entry_profile` 与 `qkv_case_context` 信号必须成对共存（后端校验）。删除信号后画像成为孤儿，或切换唯一上下文信号时，用户不知道需要清理画像，导致保存失败。
  - **修复**：
    - **后端级联删除**：`_delete_signal_from_document` 删除 `qkv_case_context` 时自动移除孤儿画像；`_prepare_expert_draft_signals` 工具改走后自动移除孤儿画像。
    - **前端清理提示**：唯一上下文信号切换时弹出确认对话框，提示用户将一并移除语义入口画像；`deleteSemanticEntry` 支持 `skipConfirm` 参数避免重复确认。
    - **切换保留信号**：切换信号类型时**保留切换后的新信号**而非删除，仅清理语义入口画像，用户无需重新添加信号（PR #1028）。
    - **删除按钮提示**：点击"删除语义入口"按钮时明确提示将级联删除信号。
  - **测试守护**：新增测试用例验证级联删除行为与孤儿画像清理。
- **QFK 日志时间窗口变量占位符被 T→空格替换破坏修复**：
  - **根因**：`backend/shared/schemas/log_source_catalog.py` 的 `normalize_absolute_log_time()` 为把 ISO 日期时间的 `T` 分隔符转为 aCLI 接受的空格（`2026-09-04T10:00:00` → `2026-09-04 10:00:00`），对入参无条件执行 `value.replace("T", " ", 1)`。当 `time_window` 是变量占位符时，`{{DATE}}` 中变量名里的第一个 `T` 被替换成空格，编译出的命令模板变成 `-t '{{DA E}}'`，执行前变量替换永远无法命中。该函数同时被 agent-service 的 `LogKeywordHandler.build_commands()`（真实执行路径）、`shared/resolution/resolvers.py`（Shared Resolution Runtime）与 QFK 命令预览接口调用，所有变量名含 `T` 的时间占位符（`{{DATE}}`、`{{DATETIME}}` 等）均受影响；`{{END}}` 因不含 `T` 而未暴露。
  - **修复**：占位符（匹配 `_PLACEHOLDER` 正则）直接原样返回，`T`→空格替换只对真实 ISO 日期时间生效；`validate_absolute_log_time()` 内部的同名替换无此问题（占位符分支已提前返回）。
  - **测试守护**：在 `backend/kb-service/tests/test_log_source_catalog.py` 新增 `test_normalize_absolute_log_time_preserves_placeholders` 参数化回归测试，覆盖 `{{DATE}}`/`{{END}}`/`{{DATETIME}}` 原样保留与 ISO `T` 分隔符正常转换。
  - **存量修复**：已编译落库的 Bundle 若包含被破坏的 `{{DA E}}`，通过 Admin UI「Bundle 迁移」功能触发重新编译即可修正。
- **BridgeRelayExecutor 冷启动并发自愈与探针加固**：
  - **背景与根因**：在节点/集群统一重启（冷启动）时，Pod 并发启动导致 `agent-service` 连接 Redis 遭遇短暂网络时差；原 FastAPI `lifespan` 仅尝试一次建连，连接失败后进入降级逻辑，跳过了 `set_executor(...)` 全局注册，且缺乏运行时自愈机制。导致 `BridgeRelayExecutor` 永久置空，后续所有 KBD 关键信号排障（QKV/QFK）100% 报错“BridgeRelayExecutor 尚未初始化”；同时 `/v1/agent/health` 缺乏对执行器状态的可观测性感知。
  - **优化落地**：
    - **启动期有限退避重试**：在 `app/main.py` 的 Redis 连接流程中引入指数退避重试回路（最多 5 次），抹平冷启动依赖拉起时差。
    - **运行时双重检查锁惰性自愈**：在 `app/tools/acli/executor.py` 中引入线程/协程安全的 `get_or_init_executor()`，若执行器未注入，在首次工具或信号调用时自动读取配置重连 Redis 并完成全局自愈注册，打通唯一调用链日志追踪；QKV/QFK 与 `kbd_differential` 统一接入该获取器。
    - **探针可观测性暴露**：在 `/v1/agent/health` 中透传 `bridge_relay_executor` 状态，消除运维盲区。
- **QKV 产出变量处理默认行为对齐高频场景（断言判断与关键字匹配）**：
  - **背景与根因**：在 QKV 关键信号的变量后处理（`orchestrate.output_processing`）场景中，运维故障排查最频繁的诉求是对已采集的具体字段直接进行断言筛选（如检测任务报错、告警描述中包含特定故障关键字），极少在 QKV 中做变量二次派生；此前”处理方式”默认值为”派生变量”（`derive`），断言”判断类型”默认值为”数值阈值”（`threshold`），导致专家每次添加处理都必须进行多次下拉菜单切换，配置操作冗余。
  - **优化落地**：
    - **处理方式默认值切换为断言判断**：`OutputProcessingEditor.vue` 中添加处理单元（`add`）默认 mode 调整为 `assert`，缺省渲染断言分支，并将下拉选项中”断言判断”优先展示。
    - **判断类型默认值切换为关键字匹配**：`defaultAssert()` 初始 matcher 类型调整为 `keyword`（`pattern: ''`, `mode: 'or'`, `expected: true`），同时对齐 `formatAssertSummary` 工具函数的缺省 matcher 类型。
    - **向后兼容与测试守护**：保留对既有 `derive` 存量配置与手动切换的完整兼容；更新并扩充前端单元测试，全量覆盖默认断言契约、类型推导与模式切换。
- **QKV 生产者信号断言判断支持变量引用**（PR #1029）：
  - **背景与根因**：QKV 生产者信号的 `output_processing` 断言判断中，关键字匹配、正则表达式、状态判定、数值阈值等 matcher 配置无法引用变量，导致两个问题：引用上游生产者产出的变量时报”未声明变量”错误；引用当前信号产出的变量时同样被误判。
  - **修复**：
    - **变量引用识别增强**：`derive_signal_requires` 函数新增收集断言判断 `match` 中的变量引用，确保关键字匹配的 pattern、正则表达式的 pattern、状态判定的期望状态、数值阈值的 value 等配置中的变量都能被正确识别为依赖。
    - **跨信号变量引用支持**：`_validate_qfk_match_or_produces` 预先收集所有信号的产出变量，支持跨信号引用（上游生产者产出的变量）和同信号内引用（当前信号产出的变量）。
    - **错误消息优化**：优化错误消息，准确反映变量引用位置。
  - **影响范围**：支持在 keyword/regex/state/threshold/delta/trend 等 matcher 配置中引用变量；支持跨信号变量引用和同信号内变量引用。
  - **测试守护**：新增两个测试用例验证修复效果，全部通过（44个测试用例）。
- **JSON 取值路径门禁校验增强、友好报错与验证 Agent 自愈能力加固**：
  - **背景与根因**：以 16998 案例（sig_002）为例，LLM 由于预训练通用先验偏置，在 JSON 取值时习惯输出带 `$` 根节点前缀的路径（如 `$.conf.idle_duration`），而平台底层取值器（`_read_json_path`）采用受控点号路径规范（禁止 `$` 前缀，正确值为 `conf.idle_duration`）。原门禁体系未对 `extract.path` 语法进行校验，导致非法路径直通入库并在运行时报错崩溃；同时验证链路缺乏结构化友好报错与指引，自愈调度器存在自愈成功后被拒候选未清理的幽灵残留缺陷。
  - **加固落地**：
    - **门禁刚性拦截与友好指引**：在 `extract_signals.py` 中新增 `_json_extract_path_violation` 校验，对 `match.extract` 与 `produces` 中的 JSON 取值路径进行严格门禁拦截；一旦发现包含 `$`、通配符或非受控点号语法，立即拦截并生成包含“违规符号”、“平台规范解释”、“改进指导”及“正误示例对照”的高可读结构化友好错误信号。
    - **验证 Agent 闭环自愈**：在 `kbd_signal_verify_v1` 系统提示词中明确注入 JSON 路径自愈契约；验证 Agent 接收到门禁错误信号后，执行去 `$` 纠偏自愈并重新提交门禁复检。
    - **消除自愈幽灵状态残留**：修复 `SignalExtractionOrchestrator.run_verify_and_self_heal`，自愈成功复检通过后，动态剔除 `all_rejected` 中已被成功自愈的候选，彻底杜绝状态不一致与幽灵残留。
    - **源头建模契约收敛**：在 `kbd_signal_model_v1` 提示词中明确补充 JSON 受控点号路径契约与正反范式，从源头降低违规率。
- **模板实例库扩展与菜单规范对齐（Bundle库 + Signal库，console审计更名）**（PR #1001）：
  - **背景**：新增的 `signal_modeling_template`（13类工具标准建模契约模板）与 `signal_best_practice`（专家审核黄金排障实例 Few-Shot）两个表在 admin-ui 缺失管理与阅览入口；原菜单名称“控制台截图审计”与“Bundle资产库”认知范围局限。
  - **重构**：
    - **菜单与路由规范更名**：侧边栏与页面内“控制台截图审计”统一更名为“console审计”；原“Bundle 资产库”统一更名为“模板实例库”。
    - **模板实例库双子库架构**：顶层引入 `Bundle 库` 与 `Signal 库` 双 Tab 切换，100% 完整保留 Bundle 仿真资产的原有编辑向导、版本切换与发布能力。
    - **Signal 库资产沉浸式阅览**：子模块支持【13类标准模板】与【专家最佳实践】，提供分类/工具/案例 Support ID 多维过滤、分页承载、Schema/变量协议/反模式规约高亮呈现，以及 Few-Shot 样本证据与 `signal_json` 完整预览及一键复制。
    - **全链路后端透传**：`kb-service` 暴露 `/api/admin/signal-assets/*` 只读查询接口，`api-gateway` 提供 `/api/v1/signal-assets/*` 安全代理透传并注入 Internal Token。
- **生产者信号（qkv_alert与qkv_task）END变量自动派生DATE变量与日志门禁治理**（PR #1002）：
  - **根因**：`qkv_alert` 和 `qkv_task` 生产的 `END` 是精确到秒的绝对时间（`YYYY-MM-DD HH:MM:SS`），而下游最大消费者 `qfk_log`（`acli log get`）所消费的底层日志是按自然日（如 `1.tar.zst` ~ `31.tar.zst`）轮转存储的。`acli log get -t` 若传入精确到秒的时间做 grep 过滤，命中行数极少甚至为 0，现场日志定位必须按天过滤日志。
  - **修复**：
    - **抽取与建模层**：在 `DEFAULT_VARIABLE_SCHEMA` 与 `DEFAULT_SHARED_VARIABLES` 中登记 `DATE`；当生产者声明产出 `END` 时，自动在 `orchestrate.produces` 中派生追加 `{"name": "DATE", "path": "end"}`；在调度器阶段 3.2 动态变量符号表与阶段 4 DAG 闭包可达性中，只要识别到 `END`，自动将 `DATE` 连通至下游消费者上下文。
    - **执行与解析层**：在 `agent-service` 的 `_extract_by_produces` 与 `_extract_hardcoded` 中，提取到 `end` 时自动派生 `date`（`YYYY-MM-DD`）；在 `_fill_pool_from_qkv` 写入变量池时，若写入了 `END`，自动向变量池派生写入 `DATE`（继承生产者优先级）。
    - **信号门禁严苛校验**：在 `extract_signals.py` 中引入强门禁，若生产者产出 `END`（派生 `DATE`），候选集中所有 `qfk_log` 消费者必须显式配置 `-t {{DATE}}`（`time_window: "{{DATE}}"`）；未配置或仍使用旧的秒级 `{{END}}`，直接拦截并加入 `rejected` 候选列表，标明原因供专家人工复核。
    - **KBD 页面交互与预览**：在 KBD 编辑页面（`KbdReviewView.vue` 和 `ProducesEditor.vue`）中，当生产者选中产出 `END` 变量时，右侧自动弹出高亮徽标 `派生 DATE` 并在下方提供提示卡片；阅览模式下在右侧自动呈现派生芯片 `DATE (派生)`；`qfk_log` 表单提示收敛为推荐使用 `{{DATE}}`。
    - **抽取 Prompt 与规范收口**：在 `kbd_signal_model_v1` 和 `kbd_signal_verify_v1` 提示词模板中收敛为正向确定性指令，明确 `qfk_log` 必须使用 `time_window: "{{DATE}}"`。
- **生产者信号 END 自动派生 DATE 变量依赖图校验与持久化归一加固（PR #1002 遗漏项修复）**：
  - **根因**：PR #1002 仅在 AI 重新抽取管道（`extract_signals.py`）中执行了 `_normalize_derived_date_variables`，且前端在 `KbdReviewView.vue` 中仅通过 `v-if` Mock 渲染派生 DATE 芯片而未真实写入 payload；底层 Schema 门禁（`signal_schema.py` 中的 `_collect_variable_sources`）未同步感知 `END -> DATE` 派生产出。导致存量 KBD、人工编辑保存的 KBD 在审核发布时，下游引用 `{{DATE}}` 被误判为“输入变量 DATE 没有上游产出或外部声明”，发生虚假阻断报错。
  - **修复**：
    - **底层 Schema 符号表加固**：在 `backend/shared/schemas/signal_schema.py` 的 `_collect_variable_sources` 中，当生产者信号（`tool.startswith("qkv_")`）产出集合包含 `END` 时，自动将其派生的 `DATE` 变量加入 `produces` 集合，使底层 DAG 闭包校验彻底消除悬空误报。
    - **核心规整函数共享化与持久化归一**：在 `signal_schema.py` 中导出 `normalize_derived_date_variables`；在 `kb-service` 的 `_prepare_expert_draft_signals`、`review_kbd_signals`、`_validate_kbd_publishable_signals_json` 及 `_validate_kbd_draft_signals_json` 中统一接入归一化规整，确保保存与审查时持久化补齐 `DATE`。
    - **前端保存归一化闭环**：在 `frontend/admin/src/views/KbdReviewView.vue` 的 `normalizeSignalProduces` 中，若生产者信号产出 `END` 且未显式声明 `DATE`，自动补齐 `{ name: 'DATE', path: endItem?.path || 'end' }`，实现“所见即所得”的数据持久化闭环。
    - **线上环境 Prompt 数据迁移闭环**：新增 `034_update_multi_agent_extract_prompts_date.sql` 数据迁移（带有唯一调用链 `migration:20260904000002`），更新已部署的 `kbd_signal_model_v1` 与 `kbd_signal_verify_v1`，收敛 qfk_log 时间窗口为 `time_window: "{{DATE}}"`，并同步更新 `signal_modeling_template` 的 `variable_protocol`。解决此前仅在 Atlas 目录修改导致 Staging 数据库未实际生效的问题。
- **KBD 批量任务默认收起折叠与可观测性「信号抽取」Tab 异常复盘透传**：
  - **背景**：KBD 知识条目管理页面的「批量任务」区域默认平铺展开占用大量首屏空间，影响专家对核心 KBD 条目的日常阅览；同时抽取链路异常表 `signal_failure_extraction` 缺乏直观的前端检索入口。
  - **优化**：
    - **批量任务卡片折叠交互**：KBD 管理页面「批量任务」卡片改造为可平滑展开/收起，默认设为收起状态；Header 紧凑展示运行中任务与总数摘要 Badge，提供明确展开/收起按钮与旋转微动画；当专家主动触发新批量任务时，智能联动自动展开，兼顾空间释放与任务状态可达。
    - **可观测性「信号抽取」Tab 新增**：在 Admin UI 可观测性模块的「审查日志」右侧新增「信号抽取」Tab，直连后端 `GET /api/v1/signal-assets/failures` 接口；支持按 Support 案例号、KBD、阶段（count/classify/model/verify）、原因分类及 Trace ID 综合检索与分页，提供异常复盘详情抽屉，展示失败诊断、原文上下文和原始 Payload JSON。
    - **后端接口与反向代理透传**：`kb-service` 在 `signal_assets` 路由中提供 `/failures` 与 `/failures/{failure_id}` 分页与详情接口（LEFT JOIN `kbd_entry` 获取案例号与标题）；`api-gateway` 在 `signal_assets_router` 完成安全代理。
- **关键信号多 Agent 分层抽取与 DAG 变量闭包穿透**（PR #1000）：
  - **根因**：PR #999 在真实 23 篇 KBD 上复测发现多 Agent 串行死锁、单体兜底短路、缺少 trace_id、丢失图片证据，且消费者信号无法感知生产者动态变量导致变量闭包校验大面积被拒。
  - **修复**：
    - 将单体回退条件修正为 `if not validated:`，确保未产生有效信号时 100% 触发单体兜底，默认开关改为 fail-closed。
    - 补齐 `signal_failure_extraction` 等表 `trace_id` 字段及索引；传入 `image_evidence_text` 打通图片诊断上下文。
    - 阶段 3 升级为 DAG 拓扑分层建模：优先并发建模 Producer 信号，动态导出已声明的变量符号表（如 `VM`, `HOST`, `DISK_ID`），再注入 Consumer 信号建模上下文，彻底根治跨候选变量断裂。
    - 强化 Few-Shot 样本防污染与证据自愈：证据轻微改写时自动纠偏回填候选原文，自动清洗悬空未闭合变量（如 `VM_DISK_PATH`）。
- **QFK 日志关键字粗筛下推与 RouteKey 细粒度判重**：
  - **根因**：同一 KBD 下针对同一日志文件匹配不同特征行时，由于 `build_log_selector` 与 `review_signal_document` 编译期未将 `match.extract.rows.include` 下推为 `acli log get -k` 正则粗筛参数，导致多个不同判定逻辑的 Signal 退化为完全相同的无 `-k` 命令行，在 Bundle 编译器和离线模拟器中触发 `重复 RouteKey` 报错。
  - **修复**：
    - 在 `build_log_selector` 中支持从 `matcher.extract.rows.include` 自动提取关键词并编译为 `-E -k "k1|k2"`，并在 `exists` 模式下优先下推关键词粗筛。
    - 在 `review_signal_document` 编译期提取 `match` 与 `produces` 的 `rows.include`，注入 `qfk_log` 的 `keyword` 和 `extended_regex`，确保不同 Signal 生成精确的专属采集指令。
- **QFK 日志关键字从 produces[].extract.rows.include 提取**：
  - **根因**：KBD 44374 案例的信号将关键字放在 `produces[].extract.rows.include` 而非 `match` 中，但 `_compile_qfk_signal_to_command` 函数只从 `match.pattern` 提取关键字，导致 `qfk_log 必须提供关键字 matcher、resource_keyword 或 request_id` 报错。
  - **修复**：在 `_compile_qfk_signal_to_command` 中新增从 `orchestrate.produces[].extract.rows.include` 提取 `filter_keywords` 的逻辑，与离线编译器 `offline_acquisition_compiler.py` 的行为对齐。
- **Bundle 工厂版本迁移路由注册修复**：
  - **根因**：`backend/diagnosis-service/app/routes/migration.py` 已创建但未在 `main.py` 中注册，导致 Admin UI Bundle 迁移功能报 Not Found。
  - **修复**：在 `diagnosis-service/app/main.py` 中导入并注册 `migration.router`。
- **Bundle 工厂版本迁移功能完整实现**：
  - **根因**：Bundle 编译器升级后，已发布的 Bundle 不会自动更新。用户从已发布版本创建 draft 再发布时，只是复制旧的 Bundle 数据，没有触发重新编译，导致旧编译器的 bug（变量替换顺序错误）仍然存在。例如 KBD 26980 信号 10 失败（CONTRADICTED）而信号 5 成功（SATISFIED），根因是旧版编译器对 `{{VM}}` 变量的 `re.escape()` 处理顺序错误导致 `fixture_not_found`。
  - **修复**：
    - 在 `bundle_migration.py` 中实现 `get_outdated_bundles()` 查询 `bundle_metadata` 表获取工厂版本过期的 Bundle 列表。
    - 实现 `migrate_bundle()` 单个迁移、`batch_migrate_bundles()` 批量迁移（支持 `dry_run` 预览模式）、`check_factory_version_health()` 健康状态检查。
    - 在 `offline_resource_sync_service.py` 的 `_publish_change()` 方法中，当发布 KBD Collector 时自动调用 `_record_bundle_metadata()` 记录 `factory_version` 到 `bundle_metadata` 表。
    - 在 `diagnosis-service/app/main.py` 中注册 `migration.router`。
    - 前端 `BundleFactoryView.vue` 更新 TypeScript 接口匹配新 API 响应格式（`current_factory_version`、`outdated_bundles`、`outdated_bundle_details`）。
  - **效果**：用户可以通过 Admin UI 的"Bundle 迁移"功能查看过时 Bundle 列表（包含 KBD ID、Support ID、当前版本、期望版本），并选择预览或执行迁移。迁移会触发 Bundle 重新编译，使其使用最新的编译器版本。
- **Bundle 迁移 API Gateway 路由代理修复**：
  - **根因**：前端 BundleFactoryView.vue 使用 `/api/hci-sim/v1/bundle-migration/*` 路径访问 Bundle 迁移 API，但该路由未在 API Gateway 中注册，导致 404 错误。
  - **修复**：在 `backend/api-gateway/app/routes/simulations.py` 中添加 bundle-migration 路由代理，将 `/api/hci-sim/v1/bundle-migration/*` 转发到 diagnosis-service 的 `/api/v1/bundle-migration/*`。
  - **效果**：Admin UI 可以正常访问 Bundle 迁移 API。
- **信号匹配器评估详细日志记录**：
  - **根因**：KBD 仿真执行时，信号状态为 CONTRADICTED 的原因难以定位，缺少详细的匹配日志，无法看到关键字命中情况和期望值。
  - **修复**：在 `kbd_differential.py` 中添加 `matcher_evaluation_result` 事件日志，记录信号 ID、匹配器类型、期望结果、最终判定、命中关键字、原始输出预览等。
  - **效果**：通过日志可以清楚看到目标关键字、实际命中关键字、原始命中结果、期望 expected 和最终判定，便于排查信号状态与预期矛盾的原因。
- **QFK AI 提取 value_mode 配置过滤与 Langfuse 可观测性集成**（PR #903）：
  - **根因**：AI 提取调用确定性 Extractor 获取候选行时，`_deterministic_spec` 未过滤 `value_mode` 配置，导致 `extract_output_values` 尝试将整行文本转换为数值类型而抛出 `QFK_TYPE_CAST_FAILED`，AI 提取在 0.8ms 内立即失败，根本没有机会调用 LLM。
  - **修复**：
    - 在 `_deterministic_spec` 中将 `value_mode` 纳入过滤集，确保确定性选择阶段只获取文本候选行，类型转换延迟到 AI 提取结果阶段处理。
    - 为 AI 提取过程添加 Langfuse `observe_llm_generation` 观测，记录 instruction、expected_type、candidate_lines 等输入和 value、evidence_lines 等输出，使 AI 提取过程可追踪。
  - **效果**：AI 提取能够正常调用 LLM 从日志行中摘取数值，类型转换在 AI 结果返回后按声明的 `value_type` 执行，Langfuse trace 完整呈现信号执行全过程。
- **QFK AI 提取 Langfuse 可观测性增强**（PR #904）：
  - **根因**：AI 提取失败时，Langfuse trace 不包含 LLM 的实际响应内容，只记录 token 统计。例如工单 Q2026082687344 报错 `QFK_AI_EXTRACT_INVALID_RESPONSE: AI 提取结果必须是非空字符串`，但无法看到 LLM 返回了什么，导致排查困难。
  - **修复**：
    - 在 LLM 调用成功后立即保存 `raw_response`，确保后续验证失败时也能获取原始响应。
    - JSON 解析失败、验证失败、成功三种情况都会调用 `update_observation` 记录实际响应内容。
    - 失败时额外记录 `parsed_payload`、`payload_value_type`、`payload_value_preview` 便于调试。
    - 限制响应长度为 2000 字符，防止超大响应影响性能。
  - **效果**：现在可以在 Langfuse 中直接看到 LLM 返回的完整 JSON，包括 `value` 字段的实际类型和内容，便于快速定位问题根因（如 LLM 返回数组而非字符串、返回 null、返回错误 JSON 格式等）。
- **QFK AI 提取数组格式兼容与提示词优化**（PR #905）：
  - **根因**：工单 Q2026082601409 失败，LLM (glm-5) 返回了数组格式的 value `[0, 0, 0]` 而非字符串 `"0, 0, 0"`，导致验证失败。提示词中 "value":"原样值或类型化数组" 表述有歧义，LLM 误解为可以返回数组类型。
  - **修复**（双重保障）：
    - **方案 A**：优化系统提示词，明确强调 "value 字段必须是字符串类型，绝不能是数组或对象"，提供清晰示例并删除歧义表述。
    - **方案 B**：增强后处理逻辑，在 `_cast_grounded_value` 中添加对数组类型的兼容处理，如果 value 是数值数组则自动转换为逗号分隔的字符串（例如 `[0, 0, 0]` → `"0, 0, 0"`）。
  - **效果**：新提示词引导 LLM 正确返回字符串格式，即使 LLM 仍返回数组，后处理逻辑也能自动兼容，彻底解决 `QFK_AI_EXTRACT_INVALID_RESPONSE` 错误。
- **QFK AI 提取 threshold matcher 多值聚合支持**：
  - **根因**：`ai_value_type_for_matcher("threshold")` 返回 `"number"`，期望单个数值。当用户配置 AI 提取多个数值（如日志中所有 `unaligned` 和 `invalid` 值）然后通过聚合求和时，AI 返回 `"0, 1, 2, 3, 4, 5"` 这样的逗号分隔字符串，但 `_cast_grounded_value` 尝试将其解析为单个数值而失败，抛出 `QFK_AI_EXTRACT_INVALID_RESPONSE: AI 结果 '...' 不是数字`。
  - **修复**：修改 `ai_value_type_for_matcher` 函数，让 `threshold` matcher 也返回 `"array<number>"`，与 `delta`/`trend` 一致。AI 可以返回多个数值，通过聚合方式（sum/max/min）转换为单个数值后再进行阈值比较。
  - **效果**：threshold matcher 现在完整支持"多值提取 + 聚合 + 阈值判断"的流程。
- **QFK AI 提取 array<number> 字符串格式兼容**：
  - **根因**：系统提示词要求 LLM 返回字符串格式的 value（`"0, 1, 2, 3"`），但 `array<number>` 类型期望列表格式（`[0, 1, 2, 3]`）。LLM 遵循提示词返回字符串，但代码验证期望列表，导致工单 Q2026082632708 报错 `AI array<number> 结果必须为非空数组`。
  - **修复**：
    - 在 `_cast_grounded_value` 中为 `array<number>` 添加字符串兼容解析，自动将 `"0, 1, 2, 3"` 解析为 `[0.0, 1.0, 2.0, 3.0]`。
    - 在 `_assert_grounded` 中增强逐字回查逻辑，对逗号分隔字符串分割后逐个检查每个值是否在证据行中出现。
  - **效果**：系统提示词与代码验证不再冲突，LLM 返回字符串或列表格式都能正确处理。
- **Langfuse observation 父子关系修复**（PR #924）：
  - **根因**：工单 Q2026082764962 Langfuse trace 中 `llm.ai_extract` observation 下没有 `llm-invoke` 子节点。`observe_llm_generation` 创建 `llm.ai_extract` observation，然后调用 `ai_client.invoke()`，后者内部调用 `observe_invoke()` 创建独立的 `(htp-agent-invoke, llm-invoke)` 结构，两者没有建立父子关系。
  - **修复**：
    - 修改 `observe_llm_generation`：在 yield 前设置 `_current_workflow_observation` context var
    - 修改 `observe_invoke` 和 `observe_stream_start`：检查 context var，如有父节点则创建子节点而非独立的根 span
  - **效果**：Langfuse trace 正确呈现 `llm.ai_extract → llm-invoke` 嵌套结构，完整追踪 AI 提取的 LLM 调用链路。
- **QFK AI 提取 UNGROUNDED 错误可观测性增强**：
  - **根因**：工单 Q2026082725128 AI 提取报错 `QFK_AI_EXTRACT_UNGROUNDED`，错误信息只有 "原文取值 output 无法从 evidence 原文回查"，无法判断是 AI 幻觉还是数据收集层污染导致 candidate_lines 包含错误数据。
  - **修复**：
    - 错误信息中输出候选行摘要，便于快速判断问题来源。
    - Langfuse observation output 中记录完整的 `output_value`、`evidence_lines` 和 `candidate_lines`，确保排查时能看到完整上下文。
  - **效果**：UNGROUNDED 错误现在包含候选行摘要，便于区分 AI 幻觉和数据收集层污染。
- **Langfuse 与前端错误可见性增强**（PR #941）：
  - **根因**：Langfuse observation 异常时只设置了 `status_message` 字段，但 Langfuse UI 主要展示 `output` 字段内容，导致错误原因在 UI 中不可见。同时，前端仿真会话失败时直接覆盖已有对话内容，用户可能看不到失败前的对话和具体错误原因。
  - **修复**：
    - 后端 `observe_workflow` 和 `observe_llm_generation` 异常时同时设置 `output` 和 `status_message` 字段，确保 Langfuse UI 可直接查看错误详情。
    - 前端 `SimulationConversation.vue` 将错误信息追加到已有内容之后，用户始终能看到完整对话和失败原因。
  - **效果**：Langfuse trace 和前端界面都能清晰展示错误信息，提升问题排查效率。
- **WebSocket 保活心跳机制**：
  - **根因**：仿真测试执行超时（60秒），前端页面保持打开但 WebSocket 仍断开。`terminal_bridge` 使用 `golang.org/x/net/websocket` 库，该库没有内置 ping/pong 保活机制；前端也没有心跳发送逻辑。当 WebSocket 空闲时，云厂商负载均衡器或 Traefik 的空闲超时机制会关闭连接。
  - **修复**：
    - terminal_bridge 启动定时器，每 30 秒发送 `ping` 消息（带时间戳）
    - 前端收到 `ping` 后立即回复 `pong` 消息（回显时间戳）
    - 保持连接活跃，防止中间层网络设备因空闲超时断开
  - **效果**：WebSocket 连接在空闲期间保持活跃，仿真测试等长时间等待场景不再因连接断开而失败。
- **关键信号多 Agent 分层建模与最佳实践库架构**（2026-09-04）：
  - **根因**：单体 Agent 承担实体切分、13 类工具分类、JSON 建模与 DAG 拓扑校验四重异构认知，导致漏抽率 37.3%、失真率 68.2%、threshold 覆盖率 0% 及伪信号问题。
  - **重构**：
    - 解耦为四个专属 Agent（计数 Agent、分类 Agent、建模 Agent、验证 Agent）；
    - 统一纳入 `system_prompt` 管理并通过 `StrictPromptLoader` 支持热加载（`kbd_signal_count_v1`, `kbd_signal_classify_v1`, `kbd_signal_model_v1`, `kbd_signal_verify_v1`）；
    - 新增标准模板库 `signal_modeling_template`、黄金最佳实践库 `signal_best_practice`（首批注入 67 个专家金标实例）和异常复盘表 `signal_failure_extraction`；
    - 验证层引入门禁反馈驱动的智能自愈回路。
  - **文档**：`docs/solution/agent/02-架构设计/关键信号多Agent分层抽取与建模模板库设计.md`
- **KBD 关键信号执行结果标题加入案例 ID 标识**：

  - **需求**：用户反馈关键信号执行结果列表中，信号标题只有序号和检查说明，无法快速识别信号归属于哪个 KBD 案例。
  - **修复**：在 `_format_step_evidence` 方法中新增 `support_id` 参数，标题格式从 `{index}. {icon} {title}` 改为 `{index}. {icon} [{support_id}] {title}`，方便用户识别信号归属。
  - **效果**：信号标题现在显示为 `1. ✅ [18906] 查看新建虚拟机任务详情确认失败报错信息`，清晰标识案例归属。
- **KBD 关键信号阅览与预览呈现完整性优化**：
  - **根因**：KBD 审查页面在非编辑态（阅览/预览模式）下，仅平铺展示基础参数，缺失了取值（`ValueExtract`，包括完整行/行列解析/JSON路径/行列选择/数量/AI提取等）与判断规则（`Matcher` / `Produces`，包括多 pattern 排版、比较条件、样本数、趋势方向、产出变量路径等）的完整结构，导致专家每次必须点击“编辑”才能获取全量判定细节。
  - **修复**：
    - QKV 生产者信号：产出变量由纯文本列表升级为芯片卡片，完整呈现变量名 `name` 与提取路径 `path`。
    - QFK 消费者信号：引入「执行结果处理」双步流式卡片（① 先取值 → ② 再判断/产出），结构化呈现取值方式、来源、路径/行列选择、结果数量、类型、AI提取指令及完整的判定类型、期望结论、模式内容列表、比较条件、最小样本数、趋势方向和产出变量单元。
- **Bundle 工厂整体布局与 Digest 展示优化**：
  - **根因**：原 Bundle 工厂页面左侧栏宽度过大（占 29% 以上）挤占了右侧核心仿真路由与 Manifest 区域；左侧表格中 KBD 列未设固定宽度被弹性撑大导致留白过多；右侧顶部长文本哈希 `sha256:xxxx` 裸露平铺、缺乏结构感且未提供快捷复制；关键校验参数未提供一键复制与视觉高亮。
  - **修复**：
    - 布局重构：左边栏宽度收敛固定为 280px，右侧详情区占 80%+ 视口空间以充分展示仿真路由表格与控制台。
    - 列宽精细化：KBD 列固定宽度为 76px（居中紧凑展示 support_id 与 rev 标识），状态列固定 78px，Digest 列自适应剩余宽度。
    - Digest 交互芯片：右侧 Header 引入 `digest-chip` 样式卡片，集成标签、等宽截断、Tooltip 悬浮全显与一键复制功能。
- **Admin 工单详情页 AI 上下文与 Prompt 审计接口对齐**：
  - **根因**：前端 `createPromptAuditApi` 历史遗留调用 `/cases/{caseId}/prompt-audit`（404），而后端统一重构为 `/audit-logs/prompts?case_id={caseId}` 并由 `audit_log`（`audit_type='prompt'`）承载；`createAuditLogApi` 缺少相对路径规范，导致 Admin 工单详情页的「AI 上下文」标签页始终显示为空。
  - **修复**：在 `@hci/shared` 中修正 `createPromptAuditApi` 映射至 `/audit-logs/prompts`，适配 `audit_log.payload` 字段结构；规范 `createAuditLogApi` 请求路径为 `/audit-logs`，确保 Prompt 上下文与工具执行审计正常加载。
- **KBD 分类与识图 LLM 超时配置优化**：
  - **根因**：data-pipeline 的 API_TIMEOUT（30s）小于 kb-service 的 LLM_TIMEOUT（60s），导致 LLM 在 30-60s 完成时客户端超时判定失败。识图提交超时也存在类似风险。
  - **修复**：
    - data-pipeline `API_TIMEOUT` 提升至 90s，`timeout_submit`（识图提交）提升至 180s
    - kb-service 新增独立超时配置：`LLM_CLASSIFY_TIMEOUT`（120s）和 `LLM_VISION_TIMEOUT`（120s）
    - Helm Chart 新增 `llmClassifyTimeoutSec` 和 `llmVisionTimeoutSec` 参数
  - **效果**：90s > 120s（Classify），180s > 120s（Vision），预留充足余量，避免上游成功但客户端超时
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
- **共享镜像源脚本（deploy/docker/base/setup-mirror.sh，禁止在各服务 Dockerfile 单独配镜像源）**：
  - 全部 8 个后端服务 Dockerfile 统一 `COPY` 并调用 `setup-mirror.sh`：apt 阿里镜像（deb + security 双 URI，deb822 格式必须同时替换）、uv/PyPI 阿里索引（`/etc/uv/uv.toml` + `/etc/pip.conf`）、Debian 安全升级均集中维护在该脚本一处。
  - 镜像源按构建环境开关（`MIRROR_MODE` build ARG）：本地默认 `on` 走阿里镜像（国内网络零配置）；CI 构建传入 `off` 走官方源——GitHub 托管 Runner 在海外，mirrors.aliyun.com 从海外访问高延迟且偶发限流。
  - **为何不用统一基础镜像**：实测 docker compose 的 `build`/`up --build` 均不保证跨服务构建顺序（无 depends_on 排序），统一基础镜像会使冷机首次 `up --build` 因 FROM 镜像尚未构建而随机失败；共享脚本方案各服务镜像自包含，冷构建确定性成功。
  - `resolve_image_build_plan.py` 将 `deploy/docker/base/` 变更视为与 `backend/shared/` 同级的全后端扩散输入。
- **db-seed PostSync Hook UNIQUE 约束修复**（hotfix/db-seed-system-prompt-unique-constraint）：
  - `desired_schema.sql` 补齐 `system_prompt.name` 字段的 `CONSTRAINT system_prompt_name_key UNIQUE (name)` 声明，使 `ON CONFLICT (name)` 种子 SQL 可正常执行。
  - `desired_extras.sql` 新增幂等 `DO $$` 块，存量环境（未包含该约束的旧部署）在下次 ArgoCD deploy 时自动补齐约束，无需人工干预。
  - staging 环境已直接热修复数据库约束并重命名为 `system_prompt_name_key`，db-seed Job 下次重建后可正常完成。
- **API 网关命令反馈 `exec-result` 路由与鉴权修复**：
  - 修复 API 网关 (`api-gateway`) 缺少 `/api/conversations/{conversation_id}/exec-result` 代理路由，导致前端命令执行结果无法回传的问题。
  - 支持对不携带 Bearer 鉴权头的客户侧匿名请求，在网关层自动填充 `Bearer client-session-placeholder-token` 进行安全绕过，契合 `conversation-service` 端 MVP 阶段的临时鉴权需求。
  - **【已移除 · SRC-L2 2026-10-08】** 上述占位符 token 兜底与 `exec-result` / `vm-console-*` / `bridge-logs` 的客户写入路由桩鉴权（`_check_user_session` / `_check_session_or_internal`）已全部移除，改为「网关签名 `X-Client-ID` + conversation-service 工单/会话归属校验」（防 IDOR）。`bridge-logs` 与 `vm-console-artifacts` 下载补 `require_user`；`scripts/verify/verify_identity_resign.py` 增加静态防回退守卫。详见 `docs/security/identity-signature.md` 的「SRC-L2 技术债修复」章节。**切勿再引入固定占位 token。**
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
- **KBD 审核页面批量操作功能**（PR #753）：
  - 支持批量选择 KBD 条目进行操作：重新识图、重新分类、抽取信号、批量通过、批量拒绝
  - 批量任务进度实时展示，支持查看逐条执行结果和失败原因
  - 失败任务可一键重试未成功记录
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
- **qkv_vm_console 虚拟机控制台视觉生产者信号落地**（2026-08-20，P0 双线）：
  - 新增第 4 种 QKV 生产者——**条件型实时视觉生产者**：可信获得 `HOST`/`VM_ID` 后以代码固定的 QEMU Monitor 操作采集控制台截图（screendump），补齐告警/任务/弹框观察不到的 Guest OS 内部现象（黑屏、蓝屏、Kernel Panic、启动卡住）。设计文档：`docs/solution/agent/虚拟机控制台视觉生产者信号设计与需求.md`。
  - **契约层**：`CONDITIONAL_PRODUCERS` 独立集合（**不在** `FRONTEND_TOOLS`，避免获得直接生产者语义）；发布门禁要求 HOST/VM_ID 来自上游 produces 或 verification_contract 外部声明；`qkv_vm_console.schema.json` timeout 独立 1-60s（gen-schemas 按相等性判断是否 $ref common_args）；`VmConsoleResolver` 只编译不可变 Capture Intent（`capture_baseline`/`wake_down_key` 两种 operation，绝不产出命令字符串）；新表 `vm_console_capture` / `vm_console_capture_artifact`（迁移 030，含每运行一次唤醒的唯一约束）。
  - **在线**：terminal_bridge `vm_console_op` 固定操作（Go 常量表构造 vtpsh，入参二次校验，未知 operation fail-closed）；PPM 经 HTTP 直传 conversation-service 制品端点（SHA-256 校验 + 确定性近黑质量检测），WS 只回元数据，未配置 `PLATFORM_ARTIFACT_URL` 时 `artifact_upload_disabled` 不降级 base64；agent-service `app/tools/vm_console/` 专用适配器（Inventory 校验、一次性唤醒令牌原子消费、§8 错误码全集），KBD 差异诊断遇 `qkv_vm_console` 路由到专用适配器，`_signal_to_qkv` 显式拒绝其进入自由文本 qkv_exec。
  - **离线**：`compile_vm_console_capture_intent` 专用编译入口（绝不产出 command_template）；Collector 候选 `executor=vm_console_capture`、`risk_level=controlled_interaction`（仅限该执行器）、16MB 上限；Go 采集器内置执行器（本机身份校验、TTY 唤醒确认——非交互/超时/拒绝一律 `wake_declined` 且无"自动同意"参数、nearblack 与 Python 常量同源、captures/ 打包 capture-result.json）；验包后派生 PNG + Vision 观察写入派生 evidence_item（原包零回写）。
  - **策略门禁**：`VM_CONSOLE_CAPTURE_ENABLED`（默认 false，agent 执行/capabilities/离线制品签发三处读取）、`VM_CONSOLE_VISION_ALLOWED`（默认 false，关闭时 `VISION_UNAVAILABLE_BY_POLICY` 保留图片不绕过）。§12 平台确认项（vtpsh RBAC、screendump 行为、sendkey 语义等）闭环前不得开启执行层。
  - **运营闭环**：Python 侧 Prometheus 指标全集（§10.2，mode 区分在线/离线）；Admin「控制台截图审计」页（脱敏摘要查询，原图访问走授权端点并记 `last_read_at/by` 审计）；Customer 完成态结果卡（画面状态/摘要/置信度/唤醒标记/复核提示/授权缩略图/失败可行动原因）；Helm 开关默认关闭（`terminalBridge.vmConsoleArtifactUploadEnabled` 控制 Bridge 直传注入）。
  - **审计/回放/验收**：`vm_console_audit_event` append-only 事件表（迁移 031）覆盖全链路节点并有 Admin 事件流抽屉；回放 Fixture 五态确定性回放；PNG 派生隔离进程执行（spawn + 资源上限）；在线编排进程内 E2E 与离线编译-渲染契约测试全绿，hci_sim 固定操作形态仿真。

- **qkv_effect 效果验证生产者信号落地**（2026-08-20，P0+P1）：
  - 新增第 5 种 QKV 生产者——**条件型效果验证生产者**：把“预期效果”从 Prompt 隐性语义升级为契约数据，捕获告警/任务/弹框/画面均无法表达的“执行成功但未达预期效果”现象，产出三态判定变量 `EFFECT_STATUS`（`achieved`/`not_achieved`/`inconclusive`，观察不足禁止坍缩）。设计文档：`docs/solution/agent/效果验证生产者信号设计与需求.md`。
  - **契约层**：`CONDITIONAL_PRODUCERS` 增加 `qkv_effect`（**不在** `FRONTEND_TOOLS`）；期望锚点 = 封闭观测通道（qkv_alert/task/dialog/vm_console 再查询）+ 7 类封闭 matcher（复用 `evaluate_matcher`，不新增自由文本判定）+ 受限窗口（settle 0-3600s / window 60-86400s / recheck 0-5）；发布门禁要求期望变量来源可达（`EFFECT_EXPECTATION_SOURCE_MISSING`）且 **qkv_effect 不得作为 KBD 唯一生产者**（`EFFECT_SOLE_PRODUCER_FORBIDDEN`）；`EffectVerificationResolver` 只编译不可变 Verification Intent（绝不产出命令字符串）；新表 `effect_verification` / `effect_verification_check`（迁移 032，check_seq 唯一约束 + 调度部分索引）。
  - **在线**：agent-service `app/tools/effect/` 专用适配器（settle 等待 → 观测委派 → 三态合成 → 有限复核），观测空结果编码为空串 + exists 负证据确定性补齐（观测原语已自证有效时 `QFK_OUTPUT_EMPTY` 不再不可定值）；KBD 差异诊断遇 `qkv_effect` 路由到专用适配器，`_signal_to_qkv` 显式拒绝其进入自由文本 qkv_exec；判定落 `diagnostic_item` type=`effect_verification`（stage=S5）供 S6 证据化与重诊断还原；结果卡经 `/internal/conversations/{id}/effect-result` 推送（message + SSE）。
  - **S6 证据化**：`send_s6_resolution_options` 附 `effect_evidence`（achieved 预填不代选 / not_achieved 建议回退重诊断 / inconclusive 如实说明）；conversation-service lifespan 启动回收非终态悬挂记录为 `inconclusive` + `ORPHANED_BY_RESTART`（fail-closed，禁止静默通过）。
  - **离线**：`compile_effect_verification_intent` 专用编译入口（冻结期望声明项，绝不产出 command_template，客户侧零执行）；`extract_requirements` 跳过 qkv_effect 不生成采集需求；验包后追溯判定为 P2。
  - **策略门禁**：`EFFECT_VERIFICATION_ENABLED`（默认 false，agent 执行/capabilities 读取；离线签发随 P2 追溯判定接入）。§12 平台确认项（效果潜伏期经验值、告警清除权威性、任务回执口径等）闭环前不得开启执行层。
  - **配套**：SOPNode 新增可选 `success_criteria`（期望在知识层的落点，向后兼容）；样例种子新增 `vm_effect_verify` 信号（seed_version 5）；Admin 审核页第 13 种信号（结构化期望表单）；Customer 效果复核结果卡（三态 tag + 可行动提示）；Helm `agentService.effectVerificationEnabled` 默认关闭。

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
- **db-migrate 镜像构建 cache export 失败修复**：
  - **根因**：`ci.yml` 的「构建 db-migrate 镜像（仅 schema 变更时）」job 使用 `type=gha` 缓存但未配置 `setup-buildx-action`，默认 docker 驱动不支持缓存导出（`Cache export is not supported for the docker driver`）。该 job 自 PR #139 引入后从未被触发（长期无 schema 变更 PR 合入 main），直到 #875 合并后首次执行暴露。
  - **修复**：在 build-push-action 前补齐 `docker/setup-buildx-action`（docker-container 驱动），与「构建并推送镜像」job 对齐。
- **output_processing 派生变量无法被下游消费者引用修复**：
  - **根因**：`_collect_variable_sources` 函数只查找 `target_variable` 字段，但 Schema 规范定义使用 `name` 字段，导致 qkv 生产者信号通过 `output_processing` 派生的变量无法被下游 qfk 消费者信号引用，保存时报错"输入变量没有上游产出或外部声明"。
  - **修复**：同时支持 `name`（规范字段）和 `target_variable`（历史兼容），与同文件重复校验逻辑保持一致。
- **qkv_dialog 试运行支持原始日志文本输入**：
  - **问题**：qkv_dialog 试运行只接受 JSON 格式输入，但 qkv_dialog 的实际输出是原始日志文本，用户需要手动转换格式才能测试。
  - **根因**：前端 `SignalDryRunDialog.vue` 第 204 行在请求发送前验证输入必须是 JSON，拦截了非 JSON 输入，导致后端修复无效。
  - **修复**：
    - 后端：`_normalize_qkv_records` 函数新增 tool 参数，对于 qkv_dialog 自动识别并解析原始日志文本，调用 `_extract_from_dialog_log` 提取 request_id、end 等信息。
    - 前端：`SignalDryRunDialog.vue` 验证逻辑新增 tool 类型判断，当 `tool.value === 'qkv_dialog'` 时允许原始文本输入，直接传给后端处理。

- **语义澄清 `semantic_disambiguation` 支持中性选项（neutral）**：
  - **背景**：澄清题选项 `effect` 此前只有 `support` / `exclude` 两种。当画像为 `guidance_only` 或唯一候选时，用户回复"否 / 无法判断"如果被设计方标记为 `exclude`，**唯一候选直接出池 → 候选池清空 → 既无可问的题也无证据请求 → 兜底文案**。
  - **修复**：
    - 路由层（`backend/shared/schemas/semantic_routing.py`）新增 `neutral` effect：既不加分也不剔除，仅记录用户回答。
    - 校验层 + JSON Schema（`signal.v2.schema.json`）+ 生成脚本（`gen-schemas.py`）同步把 `effect` 枚举扩为 `support | exclude | neutral`。
    - 测试夹具（`test_semantic_entry.py` / `test_semantic_routing.py`）覆盖三 effect。
    - 前端编辑器（`SemanticProfileEditor.vue`）三选一下拉、`kbdSignalTypes.ts` 类型同步，`KbdReviewView.vue` / `CategoryManageView.vue` 详情面板透出中性选项徽标。
  - **配置建议**：画像出现"无法判断 / 不适用 / 我不确定"等回退选项时，effect 应选 `neutral`；仅在语义上**确实反向**时（如"是其它产品 / 其它报错"）才用 `exclude`。
  - **设计意图**：`neutral` 答完后再让路由层走第 3 步 `clarifying_questions` / 第 4 步 `manual_evidence_request`，让候选池逐步收敛而不是一句话清零。
  - **配套测试约定**：`SemanticProfileEditor.spec.ts` 改用 `emitted('save')` 验证 draft 持久化，而非 `(wrapper.vm as ...).draft` 直接读取组件 ref，避免 vue-tsc TS2352；同时覆盖中性 effect 的保存路径。

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
- **给已存在数据的表新增 `NOT NULL` 列必须带 DEFAULT**：`atlas schema apply` 会直接下发
  `ADD COLUMN ... NOT NULL`，无默认值时 PostgreSQL 因存量行无法填值而整体失败
  （`column "x" contains null values`），进而阻塞 db-migrate 的 PreSync hook 与环境同步
- **`database/atlas-migrations/` 不会被打进 db-migrate 镜像**：`Dockerfile.migrations` 只
  `COPY database/data-migrations/`，写在该目录的迁移不会执行。存量兼容逻辑要么落在
  `database/data-migrations/`，要么直接表达在 `desired_schema.sql` 的列定义上

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
| 给存量数据表新增 `NOT NULL` 列却不带 DEFAULT | Atlas apply 失败 → db-migrate PreSync hook 失败 → Argo CD 同步卡死 |
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
