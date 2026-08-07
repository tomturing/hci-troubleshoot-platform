"""热加载 Prompt 必须与当前保存 Schema、截图分类契约同步。"""

from pathlib import Path

from shared.utils.prompt_loader import StrictPromptLoader

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_SEED_PATH = _REPOSITORY_ROOT / "database" / "seeds" / "02_system_prompts.sql"
_MIGRATION_PATH = (
    _REPOSITORY_ROOT / "database" / "data-migrations" / "015_align_signal_and_vision_prompts_to_current_contract.sql"
)
_MATCHER_MIGRATION_PATH = (
    _REPOSITORY_ROOT / "database" / "data-migrations" / "017_align_matcher_prompt_with_extract_contract.sql"
)
_QFK_SYSTEM_MIGRATION_PATH = (
    _REPOSITORY_ROOT / "database" / "data-migrations" / "018_align_qfk_system_prompt_command_model.sql"
)
_QFK_QUALITY_MIGRATION_PATH = (
    _REPOSITORY_ROOT / "database" / "data-migrations" / "019_align_task_and_qfk_producer_prompt_quality.sql"
)
_KBD_READ_ONLY_MIGRATION_PATH = (
    _REPOSITORY_ROOT / "database" / "data-migrations" / "020_enforce_kbd_signal_read_only_boundary.sql"
)
_SIGNAL_EXECUTABILITY_MIGRATION_PATH = (
    _REPOSITORY_ROOT / "database" / "data-migrations" / "021_enforce_signal_catalog_and_matcher_quality.sql"
)
_SIGNAL_PIPELINE_MIGRATION_PATH = (
    _REPOSITORY_ROOT / "database" / "data-migrations" / "022_unify_kbd_signal_filter_extract_output.sql"
)
_QKV_KEYWORD_MIGRATION_PATH = (
    _REPOSITORY_ROOT / "database" / "data-migrations" / "023_align_qkv_keyword_type_contract.sql"
)


def _seed_template(prompt_name: str) -> str:
    content = _SEED_PATH.read_text(encoding="utf-8")
    marker = f"'{prompt_name}',"
    start = content.index(marker)
    end = content.index("$TEMPLATE$", start)
    end = content.index("$TEMPLATE$", end + len("$TEMPLATE$"))
    return content[start:end]


def test_signal_extract_prompt_only_teaches_declarative_text_extract_and_current_match_modes():
    template = _seed_template("kbd_extract_signals_v2")

    assert '"parser":"whitespace_table"' in template
    assert '"rows"' in template
    assert '"columns"' in template
    assert '"value_key"' in template
    assert "column_mode" not in template
    assert '"mode": "any"' not in template
    assert '"mode": "or"' in template
    assert '"type": "<keyword|regex|state|threshold|delta|trend|exists>"' in template
    assert "json_path" not in template
    assert "判定模式的 match 必须包含 extract" in template
    assert "字段严格隔离：rows.scope 固定 same_record" in template
    assert "rows.include_mode 与 rows.exclude_mode" in template
    assert "绝不可把 any 或 all 写入 match.mode" in template
    assert '"pattern": "ClwDRDBClient", "mode": "or", "expected": true, "extract"' in template


def test_signal_extract_prompt_json_examples_escape_format_braces():
    template = _seed_template("kbd_extract_signals_v2")

    assert StrictPromptLoader.get_template_placeholders(template) == {
        "title",
        "category_id",
        "problem_description",
        "alert_info",
        "steps_text",
        "root_cause",
        "solution",
        "image_evidence",
        "acquirer_catalog",
        "variable_schema",
    }


def test_prompt_migration_escapes_declarative_extract_json_examples():
    migration = _MIGRATION_PATH.read_text(encoding="utf-8")
    supplemental_rule = migration.split("|| $RULE$", 1)[1].split("$RULE$,", 1)[0]

    assert StrictPromptLoader.get_template_placeholders(supplemental_rule) == set()


def test_matcher_prompt_migration_escapes_declarative_extract_json_examples():
    migration = _MATCHER_MIGRATION_PATH.read_text(encoding="utf-8")
    supplemental_rule = migration.split("|| $RULE$", 1)[1].split("$RULE$,", 1)[0]

    assert StrictPromptLoader.get_template_placeholders(supplemental_rule) == set()


def test_qfk_system_prompt_migration_teaches_current_command_model():
    migration = _QFK_SYSTEM_MIGRATION_PATH.read_text(encoding="utf-8")
    supplemental_rule = migration.split("|| $RULE$", 1)[1].split("$RULE$,", 1)[0]

    assert "补充规则 22：qfk_system 命令模型与资源字段隔离" in supplemental_rule
    assert "qfk_system 禁止 resource_keyword" in supplemental_rule
    assert "command_args 字符串数组" in supplemental_rule
    assert "produces.extract.rows.include" in supplemental_rule
    assert "timeout=120" in supplemental_rule
    assert StrictPromptLoader.get_template_placeholders(supplemental_rule) == set()


def test_signal_quality_prompt_migration_teaches_task_producer_and_dead_producer_boundary():
    migration = _QFK_QUALITY_MIGRATION_PATH.read_text(encoding="utf-8")
    supplemental_rule = migration.split("|| $RULE$", 1)[1].split("$RULE$,", 1)[0]

    assert "补充规则 23：任务上下文、QFK producer 与超时默认值" in supplemental_rule
    assert "必须先生成 qkv_task producer" in supplemental_rule
    assert "至少被一个下游信号 requires 消费" in supplemental_rule
    assert "配置文件全文产出为无人消费的变量" in supplemental_rule
    assert "timeout 默认写为 120" in supplemental_rule
    assert "source_refs 必须包含" in supplemental_rule
    assert StrictPromptLoader.get_template_placeholders(supplemental_rule) == set()


def test_seed_signal_prompt_contains_same_quality_boundaries():
    template = _seed_template("kbd_extract_signals_v2")

    assert "任务生产者、QFK 消费关系、超时与多图证据" in template
    assert "必须先生成 qkv_task producer" in template
    assert "配置文件全文产出为无人消费的变量" in template
    assert "timeout 默认写为 60" in template
    assert "produces.extract.rows.include 或 request_id" in template
    assert "rows.exclude_mode" in template


def test_signal_pipeline_prompt_migration_teaches_current_contract_without_template_placeholders():
    migration = _SIGNAL_PIPELINE_MIGRATION_PATH.read_text(encoding="utf-8")
    supplemental_rule = migration.split("|| $RULE$", 1)[1].split("$RULE$", 1)[0]

    assert "补充规则 26：统一候选过滤、取值与输出" in supplemental_rule
    assert "scope 固定 same_record" in supplemental_rule
    assert "exclude_mode" in supplemental_rule
    assert "service/action" in supplemental_rule
    assert "timeout 使用 60 秒" in supplemental_rule
    assert "expected=false/not" in supplemental_rule
    assert "未加载 kbd_extract_signals_v2 Prompt，跳过空库契约断言" in migration
    assert StrictPromptLoader.get_template_placeholders(supplemental_rule) == set()


def test_kbd_read_only_prompt_migration_replaces_old_solution_signal_guidance():
    migration = _KBD_READ_ONLY_MIGRATION_PATH.read_text(encoding="utf-8")
    supplemental_rule = migration.split("|| $RULE$", 1)[1].split("$RULE$,", 1)[0]

    assert "补充规则 24：KBD Signal 只读边界" in supplemental_rule
    assert "所有输出 Signal 的 orchestrate.phase 必须为 diagnostic" in supplemental_rule
    assert "不以 phase=solution 或 require_human_confirm 形式保留" in supplemental_rule
    assert "由专家修正源内容后重新抽取" in supplemental_rule
    assert "'\"phase\": \"<diagnostic|solution>\"'" in migration
    assert "'\"phase\": \"diagnostic\"'" in migration
    assert StrictPromptLoader.get_template_placeholders(supplemental_rule) == set()


def test_seed_signal_prompt_outputs_all_candidates_and_leaves_gate_to_service():
    template = _seed_template("kbd_extract_signals_v2")

    assert '"signals": [' in template
    assert '"phase": "<diagnostic|solution>"' in template
    assert "你只负责提出 Candidate，不得在生成阶段替服务端过滤或删除候选" in template
    assert "服务端会归入 Rejected Candidate/write_signal" in template
    assert "qkv_task 是查询历史任务的只读采集" in template
    assert "keyword 中的“启动/创建/迁移/删除”只是查询条件" in template
    assert "所有输出 Signal 的 phase 必须为 diagnostic" not in template
    assert "无法映射到 catalog 时不生成 Signal" not in template


def test_signal_executability_prompt_migration_teaches_catalog_and_matcher_boundaries():
    migration = _SIGNAL_EXECUTABILITY_MIGRATION_PATH.read_text(encoding="utf-8")
    supplemental_rule = migration.split("|| $RULE$", 1)[1].split("$RULE$", 1)[0]

    assert "补充规则 25：Candidate/Signal/Rejected Candidate 三态门禁" in supplemental_rule
    assert "模型不得替服务端过滤 Candidate" in supplemental_rule
    assert "服务端归入 write_signal" in supplemental_rule
    assert "服务端归入 not_exists" in supplemental_rule
    assert "统一视为 run_failed" in supplemental_rule
    assert "smartctl、ipmitool、dmidecode 属于 qfk_system" in supplemental_rule
    assert "禁止输出无参数的裸 smartctl" in supplemental_rule
    assert "不能把硬件/BMC 页面中的普通版本字段伪造成本机 messages 日志" in supplemental_rule
    assert "ipmitool mc info 只查看 BMC/MC 信息" in supplemental_rule
    assert "regex pattern 必须能实际命中逐字 evidence" in supplemental_rule
    assert "phase 描述 Candidate 自身执行的命令" in supplemental_rule
    assert "每个不同告警至少输出一个 qkv_alert Candidate" in supplemental_rule
    assert "不能把 .conf/.cfg/.ini/.json/.yaml 配置文件伪装成日志" in migration
    assert 'command="asan disk list"' in supplemental_rule
    assert "BMC/iBMC 管理页面中的事件日志不是 HCI 平台告警" in supplemental_rule
    assert "exists 只判断提取结果是否存在" in supplemental_rule
    assert "不得降级改写成 address、ip、error 等更宽泛关键词" in supplemental_rule
    assert "保留脱敏 pattern" in supplemental_rule
    assert "keyword pattern 数组中的每一项都必须能从逐字 evidence" in supplemental_rule
    assert StrictPromptLoader.get_template_placeholders(supplemental_rule) == set()


def test_signal_executability_prompt_migration_removes_all_model_side_filters():
    migration = _SIGNAL_EXECUTABILITY_MIGRATION_PATH.read_text(encoding="utf-8")

    assert "完整产出关键信号 Candidate 集合" in migration
    assert "你只负责提出 Candidate，不得在生成阶段替服务端过滤或删除候选" in migration
    assert "qkv_task 是查询历史任务的只读采集" in migration
    assert "补充规则 24：KBD Candidate 执行语义分流边界" in migration
    assert "仍保留最接近原意的 Candidate" in migration
    assert "extract_prompt LIKE '%补充规则 24：KBD Signal 只读边界%'" in migration
    assert "extract_prompt LIKE '%无法可靠映射为合法采集器的步骤，宁缺毋滥%'" in migration
    assert "extract_prompt LIKE '%宁可标记 needs_review 或不产出信号%'" in migration
    assert "extract_prompt LIKE '%且后续检查需要故障 HOST 或 VM 时%'" in migration


def test_seed_signal_prompt_contains_catalog_and_matcher_quality_boundaries():
    template = _seed_template("kbd_extract_signals_v2")

    assert "当前内置 aCLI catalog（生成时优先采用；缺失时仍须输出 Candidate" not in template
    assert "catalog 是知识参考，不是模型侧门禁" in template
    assert "不得把 smartctl/ipmitool 或 BMC Web 页面动作伪造成 qfk_hardware" in template
    assert "禁止输出无参数的裸 smartctl" in template
    assert "不能把硬件/BMC 页面中的普通版本字段伪造成本机 messages 日志" in template
    assert "ipmitool mc info 只查看 BMC/MC 信息" in template
    assert "regex pattern 必须能实际命中逐字 evidence" in template
    assert "phase 描述 Candidate 自身执行的命令" in template
    assert "每个不同告警至少输出一个 qkv_alert Candidate" in template
    assert "不能把 .conf/.cfg/.ini/.json/.yaml 配置文件伪装成日志" in template
    assert 'command="asan disk list"' in template
    assert "不得降级改写成 address、ip、error 等更宽泛关键词" in template
    assert "保留脱敏 pattern" in template
    assert "match.pattern 数组中的每一项都必须能从逐字 evidence" in template


def test_seed_signal_prompt_separates_qkv_keyword_from_qfk_arrays():
    template = _seed_template("kbd_extract_signals_v2")

    assert "acquire.args.keyword 必须是单个非空 string" in template
    assert "多个任务动作必须拆成多条 qkv Candidate" in template
    assert "只有 match.pattern 可以是 string 或 string[]" in template
    assert "只有 extract.rows.include/exclude 使用 string[]" in template
    assert "keyword pattern 数组" not in template
    assert "多关键字使用数组，页面按换行编辑" not in template


def test_qkv_keyword_prompt_migration_separates_qkv_and_qfk_array_semantics():
    migration = _QKV_KEYWORD_MIGRATION_PATH.read_text(encoding="utf-8")
    appended_rules = migration.split("|| E'\\n\\n# 补充规则 27", 1)[1].split(
        "version =", 1
    )[0]

    assert "acquire.args.keyword 必须是单个非空 string" in migration
    assert "match.pattern 可以是 string 或 string[]" in migration
    assert "extract.rows.include/exclude 使用 string[]" in migration
    assert "keyword pattern 数组" not in appended_rules
    assert "多个关键字使用数组，页面按换行编辑" not in appended_rules
    assert StrictPromptLoader.get_template_placeholders(appended_rules) == set()


def test_vision_prompt_gives_task_detail_modal_task_semantic_priority():
    template = _seed_template("kbd_vision_v1")

    assert "任务详情可以用弹窗展示" in template
    assert "任务详情弹窗必须判为任务截图" in template
    assert "只有没有可识别任务或告警记录的通用失败提示，才判为弹框截图" in template
