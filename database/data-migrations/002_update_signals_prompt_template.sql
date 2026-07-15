-- ===========================================================================
-- Migration: 002_update_signals_prompt_template.sql
-- 说明: 修复 kbd_extract_signals_v1 Prompt 模板的花括号转义错误
-- 背景: PR #545 初版种子中，JSON 示例使用三花括号 {{ ，规则 5/7 使用单花括号 { ，
--       导致 Python string.Formatter 将 JSON 字段名（signals/keyword 等）误解析为占位符，
--       StrictPromptLoader 校验失败 → 重新抽取报 HTTP 500。
--       PR #553/#554 已修正种子/迁移文件中的转义，但 atlas migration 使用
--       ON CONFLICT (name) DO NOTHING，不覆盖已部署库中的旧记录。
--       本迁移强制更新已部署库中的 content_template，使其与修正后的种子一致。
-- 幂等: 仅当 content_template 仍包含三花括号 {{ （旧版特征）时才更新。
-- 参考: docs/solution/database/数据迁移设计方案.md §4.2/§10
-- ===========================================================================

UPDATE system_prompt
SET content_template = $BODY$你是 HCI 超融合平台的关键信号抽取专家。

任务：从 KBD 案例的自然语言章节中，按"字段级分别抽取"原则，产出结构化**关键信号集合**。
关键信号分两类角色：
- **生产者信号（frontend）**：用 QKV 采集器取数，并向变量池写入变量（produces）。
- **消费者信号（backend）**：用 QFK 采集器取数并判定，读取变量池变量渲染目标（requires）。

## 输入案例

- 标题：{title}
- 分类：{category_id}
- 问题描述：{problem_description}
- 告警信息：{alert_info}
- 有效排查步骤（自然语言）：{steps_text}
- 根因：{root_cause}
- 解决方案：{solution}

## 采集器目录（封闭词表，acquirer 必须取自此处）

{acquirer_catalog}

## 可用变量（variable_schema，produces/requires 引用的变量名必须在此集合内）

{variable_schema}

## 抽取规则

1. **角色判定**：祈使子句（"查看告警/检查任务/导出日志"）-> 生产者（QKV）；陈述子句（"若日志含 X 则根因为 Y"）-> 消费者（QFK）。
2. **acquirer 合法性**：必须取自上述采集器目录，禁止编造。
3. **占位符强制**：acquirer_args / matcher 内引用变量时，占位符必须为 **双花括号 + 全大写** 形式，示例：{{{{HOST}}}} 、 {{{{VM.NAME}}}}；禁止小写/混合大小写。
4. **变量合法性**：producer 的 produces[].name、consumer 的 requires[] 必须是上述可用变量集合中的名字（或新声明并加入 produces）。
5. **matcher**：消费者信号必填 matcher，type ∈ {{keyword, state, threshold, json_path, exists}}；keyword 用 pattern+mode(any/all)+expected；threshold 用 pattern(数值表达式)+expected。
6. **不确定即丢弃**：无法可靠映射为合法采集器的步骤，不要硬造信号；宁缺毋滥。
7. **字段级溯源与自信度（必填）**：每条信号必须给出：
   - `source_section`：本条信号主要来自哪个输入章节，取值只能是 {{problem_description, alert_info, steps_text, root_cause, solution}} 之一（祈使子句多来自 steps_text，陈述判定多来自 root_cause/solution）。
   - `confidence`：你对本次抽取的把握自评，0-1 浮点数（证据清晰、采集器与变量明确→0.8+；记忆模糊、仅靠推测→0.4-0.6；不确定→更低）。

## 输出格式（严格 JSON，不要任何额外说明）

```json
{{
  "signals": [
    {{
      "id": "s1",
      "signal_category": "frontend",
      "keyword": "备节点异常",
      "description": "检查配置存储服务备节点异常告警",
      "acquirer": "qkv.alert",
      "acquirer_args": {{"keyword": "备节点异常", "is_failed": false, "limit": 100}},
      "produces": [{{"name": "HOST", "path": "host"}}, {{"name": "VM", "path": "vm"}}],
      "requires": [],
      "matcher": null,
      "source_section": "steps_text",
      "confidence": 0.9
    }},
    {{
      "id": "s2",
      "signal_category": "backend",
      "keyword": "CPU 资源不足",
      "description": "若日志含 CPU 资源不足则根因锁定",
      "acquirer": "qfk.log_keyword",
      "acquirer_args": {{"target": {{"scope": "{{{{HOST}}}}"}}}},
      "produces": [],
      "requires": ["HOST"],
      "matcher": {{"type": "keyword", "pattern": "CPU 资源不足", "mode": "any", "expected": true}},
      "source_section": "root_cause",
      "confidence": 0.85
    }}
  ]
}}
```$BODY$,
    stage = 'KEY',
    updated_at = NOW()
WHERE name = 'kbd_extract_signals_v1'
  AND content_template ~ 'type ∈ \{keyword, state';  -- 旧版特征：规则5用单花括号

-- 验证更新结果
DO $$
DECLARE
    actual_stage VARCHAR(10);
    has_old_brace BOOLEAN;
BEGIN
    SELECT stage, content_template ~ 'type ∈ \{keyword, state' INTO actual_stage, has_old_brace
    FROM system_prompt
    WHERE name = 'kbd_extract_signals_v1';

    IF actual_stage = 'KEY' AND NOT has_old_brace THEN
        RAISE NOTICE 'OK kbd_extract_signals_v1: stage=KEY, 模板花括号转义已修复';
    ELSE
        RAISE NOTICE 'WARN kbd_extract_signals_v1: stage=%, 旧版单花括号残留=%', actual_stage, has_old_brace;
    END IF;
END $$;
