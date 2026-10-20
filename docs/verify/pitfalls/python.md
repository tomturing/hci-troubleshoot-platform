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
