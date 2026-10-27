---
status: active
category: verify
audience: developer
last_updated: 2026-10-26
owner: team
---

# Python 代码避坑

## PIT-003：SQLAlchemy ORM 懒加载在 async 上下文中报错

在 async 路由中访问关联对象时，必须使用 `selectinload` / `joinedload` 预加载，或在同步 session 中访问。

## PIT-004：Pydantic v2 与 v1 的 validator 写法不兼容

v2 使用 `@field_validator`，v1 使用 `@validator`，混用会静默失效。

## PIT-009：dataclass 默认值使用可变对象

```python
# 错误
@dataclass
class Foo:
    items: list = []  # 所有实例共享同一个列表

# 正确
from dataclasses import field
@dataclass
class Foo:
    items: list = field(default_factory=list)
```

## PIT-040：SQLAlchemy 模型使用保留属性名导致启动失败

SQLAlchemy Declarative Base 类有保留属性（如 `metadata`、`registry`），自定义列名不能与这些属性冲突。

```python
# 错误：metadata 是 Base 类的保留属性，用于存储表元数据
class MyModel(Base):
    metadata = Column(JSONB, ...)  # ❌ 启动时抛出 InvalidRequestError

# 正确：使用不同属性名，通过 Column("列名", ...) 指定数据库列名
class MyModel(Base):
    extra_metadata = Column("metadata", JSONB, ...)  # ✅ 属性名避开保留字，列名不变
```

**症状：**
- 服务启动时抛出 `sqlalchemy.exc.InvalidRequestError: Attribute name 'metadata' is reserved`
- Pod 进入 CrashLoopBackOff

**修复：**
- Python 属性名改为非保留字（如 `entry_metadata`）
- 数据库列名保持不变（无需数据迁移）
- 使用 `Column("原列名", 类型, ...)` 语法

## PIT-041：SQLAlchemy 模型重复定义导致 Table already defined 错误

同一个表在多个文件中定义 ORM 类会导致冲突：

```python
# models/kb_category.py
class KbCategory(Base):
    __tablename__ = "kb_category"
    # ...

# routes/classify.py — ❌ 错误：重复定义
class KbCategory(Base):
    __tablename__ = "kb_category"
    # ...
```

**症状：**
- 服务启动时抛出 `InvalidRequestError: Table 'kb_category' is already defined for this MetaData instance`

**修复：**
- 一个表只定义一个 ORM 类
- 其他文件通过 `from app.models import KbCategory` 导入
- 避免在路由文件中重复定义模型

## V-009：Signal 图片输入必须按章节白名单且与 Candidate 溯源闭环

**症状：** KBD 关键信号 Prompt 已将 `root_cause`、`solution` 直接字段置空，却仍生成来自删除配置、
重启服务等方案动作的 Candidate；Candidate 的 `source_section` 看似是 `steps_text`，但 evidence 不在
该段落或标注截图中。

**根因：** `images_json` 是另一条自然语言输入通道。若把所有图片的 OCR、observed facts、fields 或
`context_before/context_after` 无差别拼入 Prompt，solution/root_cause 所属图片会绕过字段级置空；仅把
LLM 自报的 `source_section` 限制为枚举也不能证明其 `source_refs/evidence` 真实可追溯。

**修复：** 在调用 LLM 前按诊断章节白名单过滤图片，未知 section fail closed；图片只传原子观察事实，
不传自由上下文、DESCRIPTION、inferences 或 legacy desc；由同一份过滤结果建立实际输入的
`source_ref` 集合，拒绝未进入输入的 source ref 和非诊断章节。

**预防：** 增加反例测试：solution/root_cause 截图含 `rm`/`restart` 文本时，序列化 Prompt 不得含该文本；
Candidate 引用该截图时必须进入 rejected candidates。不要把“写操作后置拒绝”误当成输入隔离，它只能防
执行，不能防标签泄漏。

## V-010：图片来源集合检查不得升级为正文 evidence 与 OCR 的跨来源逐字强绑定

**症状：** Candidate 的 `provenance.evidence` 是 `steps_text` 中逐字存在的诊断结论，并引用了本轮
实际进入 Prompt 的排查截图；服务端却报“evidence 无法在截图原子事实中逐字追溯”，提前拒绝候选。

**根因：** 单一 `provenance.evidence` 可以是正文引用、截图可见文字，或两者共同支持的诊断说明。若
仅因 Candidate 填写 `source_refs`，就要求完整 evidence 出现在每一张截图 OCR 中，会把“正文主证据 +
截图辅助事实”误判为越界来源，并遮蔽后续真实的 Matcher、Catalog 或运行语义问题。

**修复：** 图片来源门禁只校验 `source_refs` 是否属于按章节过滤后、实际送入 Prompt 的图片区域；保留
`source_section` 的诊断章节约束，但不比较正文 evidence 与图片 OCR。需要逐证据原子绑定来源时，先扩展
数据模型，不能复用单一 evidence 字段强行表达。

**预防：** 覆盖三类回归：允许“正文 evidence + 合法诊断图 source ref”；拒绝 solution/未知/截断而未进入
Prompt 的 source ref；KBD27736 型脱敏 Matcher 必须到达其自身的 Matcher 门禁，而不是被图片来源门禁抢先拒绝。

## V-024：自动生成的语义画像正反例必须与校验端复算口径一致，且候选全拒须在收口处显式失败

**症状：** 工单 KBD 15967 点「重新抽取关键信号」报 422 `SIGNAL_REVIEW_BLOCKED`，
`review.issues` 为 `SIGNAL_SCHEMA_INVALID：画像变更后正反例不再通过`，与本因（模型输出不合契约）毫无关系，
误导排查方向；而同一 KBD 在模型偶尔产出合规信号时又能 200 成功，表现为「时好时坏的自锁」。

**根因：** 两处叠加。①`propose_semantic_profile` 在问题描述无特征报错码/引号时，把**整句**（含中文逗号 `，`）
当作 `positive_anchors`，并据此构造正例 `routing_examples`（`expected_match=True`）。但 `affirmative_anchor_hit`
会先把待匹配文本按 `[，,。；\n]` 切成子句、再在**单个子句内**查找锚点——跨子句的整句锚点永远命中不了，
复算极性为 False，与 `expected_match=True` 自相矛盾，触发 `validate_semantic_entry_profile` 自锁。
②抽取链路在候选被契约/门禁**全部拒绝**（`validated` 为空）时，仍继续注入合成 `case_context` + 自动生成画像，
再撞上述无关的语义门禁，把「模型吐了非契约 JSON」这一确定性失败伪装成 `SIGNAL_REVIEW_BLOCKED`。

**修复：** ①`propose_semantic_profile` 的锚点必须**子句原子**（不含子句分隔符）：优先取报错码/引号内的原子片段，
无则退化到按 `[，,。；\n]` 切分的子句片段；并新增 `_self_consistent_routing_examples`——生成端与校验端共用同一个
`lexical_profile_score` 复算极性，只保留「复算==期望」的样本，令自动画像永不因正反例矛盾自锁（人工画像仍走完整校验）。
②在 `extract_signals_for_kbd` 唯一收口处：当 `raw_candidate_count>0 且 validated 为空` 时直接抛
`SIGNAL_ALL_REJECTED`（带 `rejected_candidates` 原因、`retryable=True`），不再退化到自动画像；
`raw_candidate_count==0`（模型未识别到候选）仍保留 guidance_only 正常降级。

**预防测试：** `test_semantic_entry.py` 钉「含逗号长句 → 锚点子句原子 + validate 不自锁」；
`test_signal_generation.py` 钉「候选全拒 → 显式 SIGNAL_ALL_REJECTED 且回传原因，绝不落到误导性的 SIGNAL_REVIEW_BLOCKED」。
教训：凡「生成端产出、校验端复算」的成对逻辑，必须共用同一判定函数，否则口径漂移即自锁；
失败要在离真因最近的收口处显式暴露，严禁用无关门禁掩盖上游契约失败。

## V-026：LLM 对「<可选>」字段直出显式 null 会撞 `type: string` 致 KBD 抽取整单全拒，保存边界必须确定性归一

**症状：** KBD 16368（support_id，kbd_entry.id=321）点「重新抽取关键信号」连续 15 次全部 422
`SIGNAL_ALL_REJECTED`，文案为「执行编排的值类型不正确，请重新填写或重新选择对应类型」；
同批次 kbd:305/308 同样中招。`retryable=True` 诱使用户反复重试，但失败是确定性的，重试无效。

**根因：** 两处叠加。①Prompt 输出格式里写的是 `"action": "<可选>"`，LLM 把「可选」理解为
可以显式输出 `null`；而 v2 契约 `signal.v2.schema.json` 的 `orchestrate.action` 是
`{"type": "string"}`——JSON 的 null 与键缺失不同值，只要键存在就必拒。平台已有的
「显式 null → 字段缺失」归一（`normalize_optional_matcher_nulls`）只覆盖 `match` 段，
`orchestrate` 段是归一盲区（且旧实现对 `match: null` 的 qkv 信号直接 continue，连累同信号
orchestrate 段被跳过）。②`humanize_signal_validation_error` 在 `type` 失败分支把
`field_path`（orchestrate.action）和原始原因（`None is not of type 'string'`）裁掉，
只留「值类型不正确」，专家无从定位。48h 日志统计：61 条被拒候选全部带 `orchestrate.action=null`，
而全库已保存的 14 条带 action 键信号 0 条为 null——凡模型没吐 null 的单据全部能过，证明是
系统性契约缝隙而非坏候选个例。

**修复：** ①扩展 `normalize_optional_matcher_nulls`：`phase/action/source/target/container`
的显式 null 归约为缺失（与 match 段同一函数、同一确定性边界，抽取/保存/发布入口自动全覆盖）；
数组结构位 `produces/requires/output_processing` 的 null 属真实错误，**不归一**继续报错；
两段归一改为互相独立，`match: null` 不再阻断 orchestrate 清理。②`type/anyOf/oneOf` 失败消息
改为携带字段路径、实际值与期望类型（如「执行编排（字段路径 orchestrate.phase）的值类型不正确，
实际值为 123，应为文本」），不泄漏 Python repr/Schema 分支列表。

**预防测试：** `test_signal_text_extract_schema.py` 钉「KBD 16368 实锤形态：action/source/target/container=null
归一后可过契约」「match=null 不阻断 orchestrate 归一」「produces=null 保留并如实拒绝」
「type 错误消息含 field_path/实际值/期望类型」。教训：凡契约声明为「可选但只接受具体类型」的字段，
必须预设 LLM 会直出 null，在保存边界做确定性归一，而不是指望 Prompt 措辞或重试改好模型行为；
拒收消息必须可定位到字段，否则「retryable」提示只会引导用户徒劳循环。

## V-027：从 Prompt 侧加固关键信号抽取须「去 null 诱因 + 零花括号自检段 + 锚定完整片段」，否则热加载即崩或迁移自锁

**症状：** PR #1162（V-026）已在服务端对 `orchestrate` 段做确定性 null 归一，但端到端回测仍见
`SIGNAL_ALL_REJECTED`——模型还会直出嵌套 `match.extract` 显式 null、并把 backend 信号的
`match` 与 `produces` 同时配置（违反 XOR 门禁）。这类候选是「生成侧本就不该产出」的形态，
只在保存边界兜底属治标。

**根因（加固本身有三个易踩的坑）：** ①`kbd_extract_signals_v2` 输出契约里保留的
`"action": "<可选>"` 是 null 诱因，须整段删除；但同 Prompt 的 `review.notes` 写作
`"notes": "<可选说明>"`，含子串 `<可选>`——若迁移用裸 `LIKE '%<可选>%'` 判 remove/verify，
会误伤 notes 且 verify 永远命中不到「已清除」态而 fail-loud 回滚。②Prompt 经
`StrictPromptLoader.load_and_validate` + `str.format` 渲染，追加的【输出前强制自检】段
若含任何 `{`/`}` 会被当作占位符，要么撞 `PromptValidationError`（占位符集合≠既定 10 个），
要么在 format 时抛 KeyError。③种子 `02_system_prompts.sql` 用 `ON CONFLICT (name) DO NOTHING`，
**不改存量库**——只改 seed 对 live 环境无效，必须另起 `data-migrations/0NN` 拉齐存量。

**修复：** ①seed 删掉 `"action": "<可选>", `，并在示例末追加【输出前强制自检】段
（禁显式 null / 禁空占位数组 / backend `match`⊕`produces` 二选一 / 逐条自检），
该段全中文零花括号不引入新占位符；version `'2.5'`→`'2.8'`。注意 live staging 观察到的版本为 `2.7`（无任何已提交迁移产出 2.6/2.7，属带外手工漂移），故本加固版本号必须取**高于 2.7 的 2.8**以免将存量库版本回退；`2.6` 因低于 live 而被排除。②`data-migrations/048`（唯一调用链
`migration:20261019000001`）对存量库幂等拉齐：`replace(content_template, '"action": "<可选>", ', '')`
**锚定完整片段**（含尾逗号空格）而非裸 `<可选>`；`content_template || $RULE$…$RULE$` 追加自检段
（`NOT LIKE '%【输出前强制自检】%'` 守卫）；空库 `IF NOT EXISTS` 优雅跳过；verify 门禁同时校验
「含自检段」且「已去 action 占位」否则 RAISE EXCEPTION。③保留 V-026 的 orchestrate 归一与
可定位消息作为安全网——Prompt 约束非确定性，个别 null 仍可能漏网。

**预防测试：** `test_system_prompt_contracts.py` 钉「seed 不含 `"action": "<可选>"`、含
【输出前强制自检】与二选一文案、且 `"notes": "<可选说明>"` 仍在（证明只精确摘 action 未误伤 notes）」
「迁移 048 的 `$RULE$` 追加块 `get_template_placeholders()==set()`、replace 锚定完整片段」；
既有 `test_signal_extract_prompt_json_examples_escape_format_braces`（占位符恒等于 10 集合）作
花括号安全网。教训：改 Prompt 输出契约时，任何「删除占位标记」的迁移都必须锚定**唯一完整片段**
（警惕他字段含同名子串）、追加文本必须对 `str.format` 零花括号、且 seed 与 data-migration 双改
才能让「新环境+存量库」同时生效。

## V-029：剪枝/去重的「集合包含」不等于语义蕴含——忽略匹配模式会把 REJECTED 剪成 SUPPORTED

**症状：** 根因信号 L3 最少性门禁（`gate_minimality`）首版以「candidate 的 `match.expected` 是
stronger 的严格子集 → candidate 冗余」为规则剪信号。单测全绿（用例全是 `mode=all`），但真实
数据里 `mode=any` 一样合法：`any[A]` 被 `any[A,B]` 严格包含时，`any[A,B]` 只要命中 B 即成立，
而 `any[A]` 并不成立。剪掉 `any[A]` 后，本应由「A 未出现」否证的现场不再有任何 MUST 被
`CONTRADICTED` → 判定从 REJECTED 翻成 SUPPORTED（把该排除的 KBD 判为命中）。

**根因：** 两条独立的语义漏洞叠加。①把「判据集合的包含关系」当成「成立条件的蕴含关系」，
而 `all` 才是「全部关键词必须出现」的合取形态（强），`any` 是析取（弱）：`any` 的超集不蕴含
子集。②同环节「证据子串」规则只按 evidence 长度排序，未校验判据方向，会剪掉「证据短但判据
更严」的信号（同样降低判别力）。这两类错误的共同点是**剪枝让判定变松**，且都只在混合
`mode`/混合判据强度的真实数据上暴露。

**修复：** `_finding_stronger` 的每条规则显式带上方向守卫——规则 1（判据严格子集）要求
`stronger.mode == "all"`；规则 2（运行时等价）要求判据集合**与** `mode` 均相同且采集一致；
规则 3（证据覆盖）要求 `candidate.tokens ⊆ stronger.tokens` 且 `stronger.mode == "all"`；
规则 4（双形态）只剪 text（text 根本不进运行时判定，剪它不影响任何结论）。
原则：**剪枝必须证明「被剪项在逻辑上更弱」，不能只证明「它更短」**；无法证明时保留（L3 是
成本优化，误剪是正确性事故）。同时 `gate_sufficiency` 的 `argv-only command` 判定改为取
`posixpath.basename`（LLM 常写 `/usr/bin/ps`，裸名比对会漏网）。

**预防测试：** `test_root_signal_extractor.py::TestGateMinimality` 新增三例钉死方向性——
`test_any_mode_superset_never_prunes_subset`（any 超集不剪子集）、
`test_all_mode_superset_prunes_any_mode_subset`（all 超集可剪 any 子集）、
`test_stronger_judgement_with_shorter_evidence_is_kept`（证据短但判据更严不得剪），
以及 `test_argv_only_command_matches_absolute_path_basename`。教训：写任何「去重/剪枝/合并」
规则前，先写出被剪项的**逻辑弱于**保留项的证明；证明依赖的语义维度（此处是 `mode`）必须
出现在条件里，否则单测只会验证你想到的那一种形态。

## V-030：第二套计划不得继承第一套链路的发布门禁契约，否则编译期全灭

**症状：** 新增根因链路时，把 kb-service 下发的 KBD 载荷（含 `signals_json` 的
`verification_contract` / `generation_metadata` / `publish_validation`）原样交给第二套计划，
只把 `signals` 换成根因信号。单测用的假载荷契约恰好为空或指向根因信号，全绿；真实 staging
数据下 3 个可执行根因 KBD **100% 编译失败**：`verification contract references missing signal: k1`、
`expert publish validation status is invalid`、`undeclared external variables` → 所有根因候选
`NOT_EXECUTABLE`，新链路静默零收益。

**根因：** `compile_signal_plan` 用 `verification_contract` 校验「引用的 signal_id 是否都在本计划」，
并检查发布校验状态。该契约描述的是**第一套信号集合**的发布门禁，与第二套计划无关；把它带过去，
等于要求第二套计划同时满足第一套的引用闭包与发布状态。这是「跨链路元数据纠缠」，与信号本身无关。

**修复：** 在编排层构造第二套候选时**显式清空**三个发布门禁字段：
`model_copy(update={"signals": root_signals, "verification_contract": {}, "generation_metadata": {}, "publish_validation": {}})`。
并用**反事实**验证必要性：保留契约 → 真实数据全部编译失败；清空 → `compile_errors={}`、refs/acquisitions 正常。
原则：凡「把同一批对象送进第二套校验器」，必须逐一确认每个字段的**归属校验器**，不能默认透传。

**预防测试：** `test_root_track.py::test_build_root_candidates_replaces_signals_and_strips_key_metadata`
（断言三个字段为空且输入载荷未被修改）+ `test_initial_assessments_of_root_plan_are_not_key_plan`
（两套计划 signal_id 集合不相交）。教训：跨链路复用数据对象时，先列出「哪些字段属于哪条链路的校验语义」，
不属于本链路的一律清空，而不是等编译错误逐个豁免。

## V-031：淘汰一条策略必须「删模块即可」——共享内核不得承载任一策略的专属字段

**症状：** 为根因信号在 `shared/cdd/kbd_model.py::KBD` 增加 `root_display_anchors` 等字段，并在
`plan_compiler` / `candidate_reducer` 增加 track 维度。功能可用、测试全绿，但两条链路从此
**无法独立淘汰**：删根因要改共享内核、改归约逻辑、改关键链路诊断器；且共享模型被迫理解
「根因」这一业务概念，两条链路的缺陷相互传染。

**根因：** 把「策略维度的数据/语义」放进了「两条策略共用的内核」。共享内核的最小契约应是
「KBD 有 signals，信号可编译、可归约、可出结论」，而不是「KBD 有哪几条策略的证据」。

**修复：** 共享内核字节回到引入第二套策略之前（`git diff <base> -- backend/shared/cdd` 为空）；
第二套策略的候选在**编排层**用 `model_copy` 构造（只替换 `signals`），展示字段由编排层自己的
纯渲染模块接收（零 `shared` 依赖）；两条链路各自调用同一套纯函数，互不感知对方。

**预防测试：** `test_signal_track_isolation.py` 四类守护——共享内核源码无根因 token、
`KBD.model_fields` 无 `root_*`、依赖方向单向（根因模块可依赖关键链路，反向禁止）、
**删除根因模块后关键链路仍可导入**。教训：判断「是否真的独立」，不看有没有开关，
而看「删掉一条的代码后，另一条是否还能 import、还能跑、还能测」。

## V-032：测试替身（fake）必须严格复刻基类的同步/异步形态，否则会把 fake 违约误判为产品缺陷

**症状：** e2e 脚本继承生产诊断器，只替换物理采集边界，同时把 PARTIAL 报告入口
`_build_partial_report` 写成 `async def`（因为同类的 `_generate_report` 是 async）。生产代码按基类
契约**同步**调用它，拿到的是 coroutine，直到报告字符串拼接阶段才抛
`TypeError: unsupported operand type(s) for +: 'coroutine' and 'str'`。现场看起来像「生产代码漏 await」，
于是给产品代码加了 `await` → 单测立刻炸 `object str can't be used in 'await' expression`。

**根因：** 同一个基类里两个同类入口的异步形态**不同**：`_generate_report` 是 `async def` 方法，
`_build_partial_report` 是**同步 `@staticmethod`**（`kbd_differential.py` 内同步调用）。测试替身凭直觉
统一写成 async，违约后错误在很远的下游才暴露，归因指向了错误的方向。

**修复：** ①测试替身严格复刻基类签名（部分报告用普通 `def`）；②产品代码按基类契约调用，不因
fake 的形态而改动；③用 `inspect.iscoroutinefunction` 钉死契约
（`test_report_entry_points_contract_matches_base_class`：`_generate_report` 为真、
`_build_partial_report` 为假）；④回归钉子用 `MagicMock` 而非 `AsyncMock`——真被 `await` 会立刻
`TypeError`，比事后断言更早失败。

**教训：** 当「测试替身 + 生产代码」出现形态冲突时，先确认**基类契约**（源码里怎么定义、怎么调用），
再判断是谁违约；不要用修改产品代码去迁就 fake。

## V-033：测试替身的 `**kwargs` 会吞掉调用契约漂移，必须用显式签名 + 真实类签名守卫

**症状：** 删除 `RootTrackDiagnostic.diagnose_root(write_diagnostic_items=...)` 参数后，agent-service
全量单测 **948 passed 全绿**；但拿真实脚本做离线回放（`verify_signal_dual_track_e2e.py --rows-file`）
第一轮即崩 `TypeError: diagnose_root() got an unexpected keyword argument 'write_diagnostic_items'`。
同一处改动还漏掉了 `snapshot_id` 透传（删除根因自算指纹后，根因链路会拿空串：fake 默认空串，单测无感）。

**根因：** 编排层调用的测试替身写成 `async def diagnose_root(self, **kwargs)`，`**kwargs` 把编排层传的
任何关键字都无条件收下，**参数名与实际必需性从未被校验**。真实类删掉参数、或编排层传错参数名，
fake 依旧“成功”，单测因此成为假绿：它验证的不是「两边契约一致」，而是「fake 什么都能吃」。

**修复：** ①测试替身一律改显式关键字签名（只列编排层实际传的参数，默认值与真实类对齐）；
②补一条 `inspect.signature(真实类.方法)` 的契约守卫（断言包含编排层传入的每个参数名），
防「fake 与真实类同时漂移」；③删/改生产签名后必须跑一次真实链路离线回放，不能只信单测。

**教训：** 凡「测试替身 + 编排层调用」的位置，`**kwargs` 一律禁止——它是假绿温床；
单测全绿只证明「都能被接收」，不证明「调用契约能跑通」。


## V-034：搬移 Markdown 或按「改动集合」扫描时，基准一变引用即断——治理脚本自己就是腐化源

**症状：** 文档瘦身把 `AGENTS.md` §1 与 `docs/README.md` 第一屏历史表搬到 `docs/` 子目录后，
直觉判断「新文件里没有死链」，但按目标位置回算发现 **23 条链接全断**（11 条 + 12 条）。
同一批次 CI 日志里 `check_docs_rot.py --changed-only` 输出「增量模式：仅检查 5 个改动文档」，
而本次实际改了 31 个 md——中文名文档一条都没进增量门禁。

**根因（两处独立的基准漂移）：**
① Markdown 相对链接以**所在文件目录**为基准：以仓库根为基准的 `backend/…` 搬进
`docs/solution/events/` 后需要 `../../../`，以 `docs/` 为基准的历史表链接搬进
`docs/archive/<批次>/` 后需要 `../../`。原地看是对的，搬完就全错。
② 脚本把 `git diff --name-only` 的输出直接和 `Path.rglob()` 结果做集合比较，而 git 默认
`core.quotePath` 会把非 ASCII 路径输出成带引号的八进制转义串（`"docs/\350\250\276….md"`），
与真实路径永不相等。实测：31 个改动文件中 26 个被转义，默认口径只命中 **5/31**；
加 `-c core.quotePath=off` 后命中 **30/31**（差的 1 个是本就被删除的文件，属正确排除）。

**修复：** ①任何搬移之后必须用「在新位置逐个解析 link target」的脚本回算，只对
「原基准下存在、新基准下不存在」的链接补前缀，并在文件头写明改写范围与逐字口径
（例：622 非空行中 616 行逐字命中，剩余 6 行仅差路径前缀，difflib 逐字符确认）；
②git 路径解析一律 `-c core.quotePath=off`（或 unquote + 去引号），并在脚本内断言
「改动 md 文件数 == 增量扫描命中数」，不等即失败关闭，避免门禁静默漏检。

**落地状态（2026-10-26）：② 已落到代码。** 在此之前 ② 只停留在本文档，
`scripts/ci/check_docs_rot.py` 一直沿用裸 `git diff --name-only`，
所以上述「只命中 5/31」对普通 PR 依旧成立，增量门禁（含 C6 密钥泄露扫描，
两者共用 `files` 入参）实际长期空转。已修复：

- 抽出 `list_changed_md_files()`，使用 `git diff --name-only -z --diff-filter=d`。
  `-z` 让 git 不做任何 quoting，比 `-c core.quotePath=off` 更彻底且不依赖
  运行者的 git 配置；`--diff-filter=d` 排除删除，否则「数量相等」断言会把
  被删文档计入而误报。
- 断言改为 **严格相等**（`len(matched) != len(changed_files)` 即抛错）。
  此处特意不用「完全不命中才报错」：漏检一部分（如 5/31）比完全不检更危险，
  因为输出依旧是「✅ 通过」。
- **解析失败不再降级为全量扫描，而是直接 exit 1。** 原先 `except` 分支只打印
  `⚠️ … 回退全量扫描` 就继续跑——这是同一个空转缺陷的**第二个入口**：全量模式下
  无法区分「本次引入」与「存量」，所有问题都落到「仅警告」，最终照样输出
  「✅ 门禁通过」。且它还会把 924 个全量文档打印成「仅检查 924 个**改动**文档」，
  误导性更强。无法确定「改了什么」时，增量门禁根本无法履行使命，唯一安全的
  行为是失败关闭。
- **阻断粒度从「文件」收紧到「行」。** 只凭文件级增量，一份有存量死链的长文档
  只要被改了任何一行，就会让整篇文档的历史死链变成阻断项——改个错别字就要被迫
  修几千行文档，结果是没人敢动文档，反而加速腐化。新增 `list_changed_md_lines()`
  解析 `git diff --unified=0` 的 hunk 头（`@@ -a,b +c,d @@` 的 `+c,d`），只对落在
  本次新增/修改行上的 C1/C2/C3 阻断，其余标为存量仅警告。
  该函数只能读文本 hunk 头、用不了 `-z`，故显式 `-c core.quotepath=false`；
  但仅靠配置不够可靠，因此用 `list_changed_md_files`（`-z`，不受配置影响）的
  结果做「两个解析器看到的文件集必须一致」校验，不一致即失败关闭——否则会
  退化成「全部算存量 → 只警告不阻断」。
- 回归测试 `scripts/ci/test_check_docs_rot_changed_only.py` 覆盖中文单/多路径、
  非 md 混合、删除排除，并保留一条**反证测试**锁定「裸 `--name-only` 会转义」
  这一因果（git 行为若变化，该测试会提醒重新评估）；另有 7 个用例覆盖行号解析：
  单行修改、文末追加、纯删除（不产生阻断区间）、多文档独立映射、两解析器文件集一致、
  纯改名无 hunk 头、改名同时改内容。
- **本体已纳入 ruff 门禁（2026-10-26）。** 此前 `check_docs_rot.py` 从未被任何
  ruff 门禁覆盖，同时违反 SIM110 / SIM102 / SIM108 / E741 / F841，且整体格式不符
  `ruff format`——这是它能带病长期存活的结构性原因：**docs 门禁依赖它，却没人检查它**。
  已逐处等价改写（`any()` 替代 for-return、合并嵌套 `if`、删除未使用的
  `business_terms`、`l` 改名 `line`、`if`-`else` 改三元），并在 `ci.yml` 的
  ruff check / ruff format 两份清单中登记。
  **零行为变更的证据**：对同一子树（`--dir docs`，376 个文档）分别跑格式化前后
  版本，含 `--json` 结构化输出在内逐字节相同（diff 行数 0）。
  注意对拍时两侧工作区文件集合必须一致：C3 检查依赖「文件是否存在」，本地未跟踪
  文件（如 data-pipeline/kbd 目录下的环境变量文件）会让一侧少报 8 条，曾因此误判为
  「行为有差异」。
  推论：**不要在文档里用反引号包裹被 gitignore 的路径**。CI 是干净 checkout，
  这类路径永远不存在，C3 会把它们当作失效引用拦住——本次就因把该路径写成行内
  代码而自伤一次，被门禁当场抓出。
- **纯改名 PR 曾被自己的严格断言误杀（同日修复）。** 上一版断言是「改动文件全集 ==
  hunk 头文件集」，而相似度 100% 的 rename 只输出 `rename from/to`，**不产生 hunk 头**，
  hunk 解析天然看不到它。实测一次文档瘦身 PR：18 个纯改名 + 9 个内容改动
  = 26 个改动文件，hunk 头只有 9 个，断言直接 fail-closed，把「正常的纯改名」
  判成「路径 quoting 回归」。后果是整个瘦身工程每批次都要撞这堵墙。
  修复：新增 `list_content_changed_md_files()`，用 `git diff --numstat -z` 取
  「真有几行变化」的集合作为断言对象——纯改名 add/del 恒为 0，天然排除；`-z`
  不做 quoting；rename 项路径字段为空，其后紧跟旧路径与新路径两个 NUL 段。
  断言口径变为「hunk 头文件集 == numstat 有内容变化文件集」：既不误杀改名，
  也仍能抓住真正的 quoting 回归（被污染的路径会变成对不上的野指针）。
  新增 2 条回归测试：`test_pure_rename_produces_no_hunk_header`、
  `test_rename_with_content_edit_counts_as_content_changed`。
  **教训升级：**fail-closed 断言必须对齐「不适用的合法情形」。断言越严，越要先问
  「哪些合法输入天然不产生这份观测」——否则治理脚本会拦住正确的治理动作。

**教训：** 「本地全绿」不等于「引用有效」。凡引用解析依赖基准（所在目录、改动集合、
checkout 范围——另见 V-023 的 sparse-checkout 同类坑），**改动基准的动作必须自带基准回算**。
适用边界：所有文档搬移 / docs 治理脚本 / 以 `git diff` 机器名做匹配的 CI 门禁。

---

## V-035：建议级检查项触发阻断级退出，会把整条门禁送上永久红灯——然后没人再跑它

**缺陷类别：** 门禁设计 / 严重度分级混淆（不是文档问题，也不是 Python 问题）

**根因：** `check_docs_naming.py` 的 `check_file()` 用前缀区分严重度——
`❌` 是违规、`⚠️` 是建议——但 `main()` 把两者塞进同一个 `errors` 列表，
只要列表非空就 `exit 1`。于是严重度的区分在出口处被抹平：「文件名干不在白名单」
这种建议，和「文件名带版本号」这种违规，拥有完全相同的阻断力。

而 `⚠️` 的唯一来源 `BRANCH_APPROVED_STEMS` 是一张**手写清单**，天然滞后于文档增长。
实测 main 上带着 6 条存量 ⚠️，`--full` 常年 `exit 1`。后果不是「多拦住几个 PR」，
而是**整条门禁报废**：既然全量模式永远是红的，就没人跑它；于是其中真正的
`❌` 检查（版本号后缀、点分隔符、泛化文件名）也一起失效——当时 main 上确实
躺着 4 个带 `_V3.1`/`_V1`/`_V2.1` 后缀的文档，无人知晓。

**修复形态：** 集中到一个 `split_findings()`，把结果分成 `(blocking, advisory)`：
`❌` 进 blocking（`exit 1`），`⚠️` 进 advisory（只打印）。划分逻辑必须只有一处，
否则「建议」会在某次改动里悄悄升级为「阻断」。配 6 条回归测试钉住分级行为，
其中一条直接用真实文件名断言「白名单未登记只能产生 ⚠️、不得产生 ❌」。

**可复用教训：** 一条永远失败的门禁与没有门禁等价，而且比没有更糟——
前者还多了一层「我们有门禁」的错觉，同时训练人忽略红叉。因此新增任何检查项前
必须回答两个问题：

1. 它会不会在**合法输入**上永久为真？（手写清单、跨仓路径、外部依赖版本都会）
2. 它的严重度有没有在**出口处**被抹平？

适用边界：所有分级门禁（命名 / 引用 / 覆盖率 / 依赖审计），尤其是带白名单、
豁免清单、或历史欠账的手写规则。

## V-036：规范里写着「必须」、却没有任何门禁执行，等于没有这条规范

**缺陷类别：** 治理设计 / 规范与执行脱节

**根因：** `docs/文档管理规范.md` §7.3 从 2026-04 起就规定现行全量文档必须有五个
frontmatter 字段（`status` / `category` / `audience` / `last_updated` / `owner`），
§7.1 规定必须有变更历史节——两条都写着「必须」。但
`grep -rl 'frontmatter\|last_updated\|变更历史' scripts/ci/` **零命中**：没有任何门禁检查它们。

后果是实测出来的：203 篇现行文档里有 118 篇（58%）完全没有 frontmatter；另有 4 篇把
`implemented` / `proposed` / `accepted` / `current` 写进 `status`（把「功能是否落地」
当成「文档生命周期」）、10 处 `category` 取词表外的值。这些缺陷存在了半年，没有一次
被拦住——因为从来没有人检查。

**为何「无牙条款」比「没有规范」更糟：** 没有规范时，读者知道规矩未定义，会自己核实；
有规范却没人执行时，读者会相信「所有文档都符合 §7.3」，于是放心按 `status` 过滤——
而过滤结果里缺了一半文档且毫无提示。静默漏比比报错危险得多。

这与 V-035 是同一枚硬币的两面：V-035 说「别让建议级误伤阻断」，本条说「别让必须级
失去阻断力」。两个方向做错都得到同一个结局——门禁或规范失去可信度。

**修复形态：** `scripts/ci/check_docs_frontmatter.py`，`--full` 全量、阻断级接入
`docs-governance`：

- 只认文件最开头的 `---` 块（正文里的水平线不是元数据）
- 逐条校验 §7.3.1 / §7.3.2 的值域，**不设建议级通道**——本契约没有「合法但滞后」的
  取值空间（不像白名单），写错就是写错
- 全量而非增量：203 篇小文件是毫秒级开销，而增量检查只能保证「新改的合规」，
  从没被碰过的坏文档会永远逃过检查
- 15 条回归测试，每条契约配一个「必须报错」用例——只验证「合规样本能通过」的门禁
  无法区分「正确」与「空转」
- **先补齐存量再上阻断**，顺序不能反：否则所有 PR 当即为历史欠账买单，门禁上线当天
  就被绕过或关掉

**可复用教训：** 写下一条「必须」时，同一次改动里就要给出它的执行机制（门禁 / 测试 /
自动化检查）。写不出执行机制的，只能降级写成「建议」——否则规范会积累一批无人执行
的条款，最终整份规范失去权威，连真正被执行的条款也不再被当真。

适用边界：任何靠人工遵守的规范、契约、清单——元数据字段、命名约定、目录结构约束、
提交信息格式。特别是「写在表格里看上去很正式」的那类条款，最容易被当成已生效。

---

## V-037：新增的门禁测试没登记进 CI 清单，就是一份不会执行的承诺

**缺陷类别：** 治理设计 / 检查器自身失效

**症状：** `scripts/ci/test_check_docs_rot_semantics.py`（23 项用例）已写入仓库、本地
`pytest` 全绿，而 `grep -n semantics .github/workflows/ci.yml` **零命中**。CI 的
paths-filter、`ruff check`、`ruff format --check`、`pytest` 四处都用**显式文件清单**
挑脚本（为避免全仓 lint 拖慢），漏登记不会报错——测试在 CI 里从未跑过，PR 却照样
显示「✅ 测试通过」。

**根因：** 「写了测试」与「测试被 CI 执行」是两个独立动作，后者依赖手工维护的四份
清单。同一批缺陷在 V-034 / V-036 已各记一笔：`check_docs_rot.py` 本体长期不在 ruff
清单里（违反 SIM110/SIM102/SIM108/E741/F841 也照样合并），docs 规范里的「必须」没有
任何脚本兑现。这不是个别疏忽，是**治理脚本自己不受治理**。

**顺带踩到的第二道坑：** 把该测试登记进 ruff 清单后，`uvx ruff check` 立刻报
**33 个 N802**——用例函数名原本用中文（`test_邻近否定语义词_豁免且必须带理由`），
而本仓库 `[tool.ruff.lint] select` 含 `N`（pep8-naming）。也就是说：**先登记后 lint
= CI 当场变红；先不登记 = 测试永不执行**。两条都不可接受，正确顺序是先按仓库惯例
改成 ASCII `snake_case`（中文语义放 docstring，可读性不损失），再登记。这个坑之所以
隐蔽，是因为仓库里既有的 `scripts/ci/test_*.py` 全是 ASCII 命名，没有任何文档写下
这条约定，只有 lint 会告诉你。

**修复：**
- 判定逻辑从 `main()` 抽出为纯函数 `select_blocking()` / `is_current_layer()`：
  内联版本要验证「现行层存量是否阻断」，必须手工构造 git 状态（真实分支 + 改动行 +
  merge-base），成本高压之下就只能靠人肉看输出——等于没测。
- 全部用例（当时 33 项，含后续补的堆词逃逸边界用例现共 34 项）改 ASCII 命名 + 中文 docstring，含逐条「不许放宽」的反向用例，以及一条
  专门守护「未开 `--current-blocking` 时行为与旧版逐字一致」的用例：新开关的默认
  路径若被偷偷收紧，等同于绕过评审改了门禁。
- 四处清单全部登记，并把硬门禁命令改为 `--changed-only --current-blocking`。

**可复用教训：** 交付「检查器 / 测试 / 门禁」这类东西时，验收动作必须包含一次
**反向确认它真的被执行**：在 CI 配置里 `grep` 自己的文件名，命中数应等于该文件需要
参与的 job 数（本仓库为 4）。命中数为 0 时，你写下的不是保障，是一份无人执行的承诺
——与 V-036 是同一种病，只是宿主从 markdown 换成了测试文件。

适用边界：任何用显式文件清单挑选范围的 CI（ruff / mypy / pytest / lint 白名单）、
以及新增的静态检查脚本与回归用例。若 CI 改用通配目录（如 `scripts/ci/`），本坑的
形式会变成「通配是否真的覆盖到新建子目录」，反向确认这一步依然不可省。
