-- ============================================================================
-- 038_fix_sop_variable_output_path.sql
-- 描述：修复 SOP 文档 ID=5（内存条 ECC）的变量配置，为 check_meth 和 memory_tag 添加 output_path
-- 问题：SOP 变量 source="skill:memory-fault-detect" 但缺少 output_path，导致 Skill 执行后无法提取变量值
-- 影响：工单 Q2026092889831 等内存 ECC 故障排障失败
-- 幂等性：支持重复执行，使用 jsonb_set 更新
-- 唯一调用链：migration:20260928000001
-- ============================================================================

DO $$
DECLARE
    sop_id INTEGER := 5;
    updated_tree JSONB;
    var_count INTEGER;
BEGIN
    RAISE NOTICE '开始执行 038 数据迁移：修复 SOP 文档 ID=5 的变量 output_path 配置...';

    -- 获取当前 tree_json
    SELECT tree_json INTO updated_tree FROM sop_document WHERE id = sop_id;

    IF updated_tree IS NULL THEN
        RAISE NOTICE 'SOP 文档 ID=5 不存在（可能是新环境或 seed 数据未包含），跳过修复';
        RETURN;
    END IF;

    -- 检查 variables 数组
    IF NOT updated_tree ? 'variables' THEN
        RAISE NOTICE 'SOP 文档 ID=5 没有 variables 字段，跳过修复';
        RETURN;
    END IF;

    -- 更新 check_meth 变量：添加 output_path
    updated_tree := jsonb_set(
        updated_tree,
        '{variables}',
        (
            SELECT jsonb_agg(
                CASE
                    WHEN elem->>'name' = 'check_meth' THEN
                        jsonb_set(
                            jsonb_set(elem, '{output_path}', '"check_meth"'),
                            '{acquisition_tool}', '"memory-fault-detect"'
                        )
                    WHEN elem->>'name' = 'memory_tag' THEN
                        jsonb_set(
                            jsonb_set(elem, '{output_path}', '"memory_tag"'),
                            '{acquisition_tool}', '"memory-fault-detect"'
                        )
                    ELSE elem
                END
            )
            FROM jsonb_array_elements(updated_tree->'variables') AS elem
        )
    );

    -- 更新数据库
    UPDATE sop_document
    SET tree_json = updated_tree,
        updated_at = NOW()
    WHERE id = sop_id;

    -- 验证更新结果
    SELECT COUNT(*) INTO var_count
    FROM sop_document,
         jsonb_array_elements(tree_json->'variables') AS elem
    WHERE id = sop_id
      AND elem->>'name' IN ('check_meth', 'memory_tag')
      AND elem->>'output_path' IS NOT NULL;

    IF var_count = 2 THEN
        RAISE NOTICE '✓ 成功修复 SOP 文档 ID=5 的变量配置：check_meth 和 memory_tag 已添加 output_path';
    ELSE
        RAISE WARNING '⚠ 修复结果异常：预期更新 2 个变量，实际更新 % 个', var_count;
    END IF;

    RAISE NOTICE '完成 038 数据迁移（trace_id=migration:20260928000001）';
END $$;
