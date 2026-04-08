-- migrate:up

-- =============================================================================
-- kb_category 补充字段 — SOP 路由命中追踪与软删除支持
-- 创建日期：2026-04-02
-- 变更内容：
--   1. hit_count INTEGER — S0 意图识别命中次数统计，用于分析热门/冷门分类
--   2. is_active BOOLEAN — 软删除标记，禁用的分类不参与 S0 意图识别
--
-- 背景：
--   远端 kb_category 已有 code/domain/path_labels/embedding 字段支持意图识别。
--   本地新增两个字段用于 S0 意图识别场景的运营分析能力：
--   - hit_count：统计每个分类被 LLM 确认的次数，指导 SOP 覆盖优先级
--   - is_active：允许临时禁用某些分类而不删除数据
--
-- 关联文档：22_S0意图识别与分类基线重构方案.md
-- =============================================================================

-- hit_count：意图识别命中次数统计
ALTER TABLE kb_category ADD COLUMN IF NOT EXISTS
    hit_count INTEGER DEFAULT 0;

-- is_active：软删除标记（默认 TRUE，禁用后不参与 S0 意图识别）
ALTER TABLE kb_category ADD COLUMN IF NOT EXISTS
    is_active BOOLEAN DEFAULT TRUE;

-- 索引：快速过滤活跃分类（S0 prompt 构建时使用）
CREATE INDEX IF NOT EXISTS idx_kb_category_is_active
    ON kb_category(is_active) WHERE is_active = TRUE;

-- 注释
COMMENT ON COLUMN kb_category.hit_count IS
    'S0 意图识别命中次数，每次 LLM 确认该分类时 +1，用于分析热门/冷门分类，指导 SOP 覆盖优先级';
COMMENT ON COLUMN kb_category.is_active IS
    '软删除标记，FALSE 时该分类不参与 S0 意图识别 prompt，但保留数据供历史查询';

-- migrate:down
-- 提供 rollback 路径
ALTER TABLE kb_category DROP COLUMN IF EXISTS hit_count;
ALTER TABLE kb_category DROP COLUMN IF EXISTS is_active;
DROP INDEX IF EXISTS idx_kb_category_is_active;