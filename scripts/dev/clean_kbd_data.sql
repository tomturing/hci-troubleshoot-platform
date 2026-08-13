-- ============================================================================
-- KBD 知识库数据清理脚本（匹配实际数据库 Schema）
-- 副作用: conversation.resolved_kbd_entry_id 自动 SET NULL (ON DELETE SET NULL)
-- ============================================================================

BEGIN;

DELETE FROM kbd_image;
DELETE FROM kbd_entry;
DELETE FROM dynamic_resource_active WHERE resource_type = 'kbd';
DELETE FROM dynamic_resource_revision WHERE resource_type = 'kbd';

COMMIT;
