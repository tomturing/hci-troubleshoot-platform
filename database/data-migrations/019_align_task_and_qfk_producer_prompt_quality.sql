-- 补齐任务生产者、QFK producer 消费关系、超时与多图证据规则。
-- 不对 support_id 做特判，也不改写既有 KBD/revision；历史 Bad Case 通过重新抽取形成新 Proposal。

UPDATE system_prompt
SET content_template = content_template || $RULE$

# 补充规则 23：任务上下文、QFK producer 与超时默认值（最高优先级）
- 当标题、问题描述、任务详情或任务截图明确表达启动、创建、迁移虚拟机失败，且后续检查需要故障 HOST 或 VM 时，必须先生成 qkv_task producer。keyword 使用正文或截图中稳定的任务动作，is_failed=true，produces 至少声明 HOST 和 VM；后续 QFK 通过 requires 使用这些变量。能够从失败任务取得的 HOST/VM 不得降级为未声明外部变量。
- qfk_system 等 QFK producer 只允许产出至少被一个下游信号 requires 消费的变量。读取配置文件后直接判断字段存在、缺失或状态时，应生成带 match 的独立 matcher；禁止把配置文件全文产出为无人消费的变量。配置文件中代表不同诊断事实的字段应分别生成 matcher，不得用一个泛化 producer 替代。
- 故障主机截图与正常参考截图同时出现时，只对故障目标机上可执行、可验证的事实生成 QFK。正常参考截图只用于确定预期或辅助证据，不得把正常机专有字面值强制生成为故障主机必须命中的远程检查。
- 所有 qkv_ 和 qfk_ 信号的 timeout 默认写为 120。没有明确、可审计的特殊耗时依据时，禁止使用 10、30 等历史默认值。
- evidence 同时引用多张截图或同时比较故障图与参考图时，source_refs 必须包含 evidence 实际使用的全部截图引用；禁止文字声称比较多图而只记录一张图。
- 信息不足时标记 needs_review 或减少候选；不得为了凑数量生成无下游消费者的 producer。
$RULE$,
    description = concat_ws('；', NULLIF(COALESCE(description, ''), ''), '任务生产者、QFK 消费关系、120 秒超时与多图证据'),
    version = '1.8',
    updated_at = NOW()
WHERE name = 'kbd_extract_signals_v2'
  AND content_template NOT LIKE '%补充规则 23：任务上下文、QFK producer 与超时默认值%';

DO $verify$
DECLARE
    extract_prompt text;
BEGIN
    SELECT content_template INTO extract_prompt
    FROM system_prompt
    WHERE name = 'kbd_extract_signals_v2';

    IF extract_prompt IS NULL THEN
        RAISE NOTICE '未加载 kbd_extract_signals_v2 Prompt，跳过空库契约断言';
    ELSIF extract_prompt NOT LIKE '%补充规则 23：任务上下文、QFK producer 与超时默认值%'
       OR extract_prompt NOT LIKE '%必须先生成 qkv_task producer%'
       OR extract_prompt NOT LIKE '%至少被一个下游信号 requires 消费%'
       OR extract_prompt NOT LIKE '%配置文件全文产出为无人消费的变量%'
       OR extract_prompt NOT LIKE '%timeout 默认写为 120%'
       OR extract_prompt NOT LIKE '%source_refs 必须包含%' THEN
        RAISE EXCEPTION 'kbd_extract_signals_v2 未对齐任务生产者与 QFK 抽取质量契约';
    END IF;
END
$verify$;
