-- 对齐 line_count Matcher 与声明式 Extract 契约。
--
-- 历史 Prompt 的计数示例遗漏 match.extract，模型按示例生成的合法行数判定会被
-- Signal Schema 拒绝。仅更新活动 Prompt；历史 KBD/revision 保持不可变。
-- 024/025 已被其他迁移占用，本修复使用 026，避免存量环境按版本号错误跳过。

UPDATE system_prompt
SET content_template = content_template || $RULE$

# 补充规则 28：line_count 行数阈值取值契约（最高优先级）
- threshold + line_count 必须显式配置 match.extract={"type":"text","rows":{"mode":"all"},"cardinality":"all","source":"stdout"}，表示统计完整 stdout 的候选行数；禁止把输出第一个数字误当行数。
- 其他 threshold/delta/trend 数值判定仍必须根据证据明确声明 JSON 路径、文本行列或 AI 数值取值，不能套用完整 stdout 默认值。
$RULE$,
    version = '2.4',
    updated_at = NOW()
WHERE name = 'kbd_extract_signals_v2'
  AND content_template NOT LIKE '%补充规则 28：line_count 行数阈值取值契约%';

DO $verify$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM system_prompt WHERE name = 'kbd_extract_signals_v2'
    ) THEN
        RAISE NOTICE '未加载 kbd_extract_signals_v2 Prompt，空库将在种子阶段写入最新版本';
    ELSIF NOT EXISTS (
        SELECT 1
        FROM system_prompt
        WHERE name = 'kbd_extract_signals_v2'
          AND version = '2.4'
          AND content_template LIKE '%补充规则 28：line_count 行数阈值取值契约%'
          AND content_template LIKE '%threshold + line_count 必须显式配置 match.extract%'
    ) THEN
        RAISE EXCEPTION 'kbd_extract_signals_v2 line_count Extract 契约升级未生效';
    END IF;
END
$verify$;
