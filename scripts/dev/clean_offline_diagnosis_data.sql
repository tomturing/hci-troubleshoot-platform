-- ============================================================================
-- 离线诊断模式数据清理脚本
-- 用途: 清空所有离线诊断相关数据，准备重新执行 KBD 全量同步
-- 安全: 不影响 kbd_entry / kbd_revision / kbd_image / tool_definition / case 表
-- ============================================================================

BEGIN;

-- 1. 断开 collector_definition 的外部引用（RESTRICT 约束）
DELETE FROM offline_signal_collector_mapping;

-- 2. 清理诊断会话及其 CASCADE 子表：
--    diagnosis_report_revision, diagnosis_report, diagnosis_candidate,
--    signal_evaluation, diagnosis_run, supplement_plan,
--    diagnosis_processing_job, evidence_item, evidence_assessment,
--    diagnostic_evidence_bundle, diagnosis_upload_session,
--    collection_plan_item, collection_plan,
--    collector_artifact_item, collector_artifact,
--    diagnosis_deletion_job, diagnosis_management_audit, diagnosis_legal_hold_audit
DELETE FROM diagnosis_session;

-- 3. 清理采集器定义和采集画像（业务事实表）
DELETE FROM collector_definition;
DELETE FROM collection_profile_definition;

-- 4. 清理同步状态
DELETE FROM offline_resource_sync_event;
DELETE FROM offline_resource_sync_change;
DELETE FROM offline_resource_sync_batch;
DELETE FROM offline_resource_sync_state;

-- 5. 清理动态资源生效指针和不可变修订（仅离线诊断相关类型）
DELETE FROM dynamic_resource_active
 WHERE resource_type IN ('collection_profile', 'collector');

DELETE FROM dynamic_resource_revision
 WHERE resource_type IN ('collection_profile', 'collector');

COMMIT;

-- ============================================================================
-- 验证：以下查询应全部返回 0
-- ============================================================================
-- SELECT 'diagnosis_session' AS tbl, count(*) FROM diagnosis_session
-- UNION ALL SELECT 'collector_definition', count(*) FROM collector_definition
-- UNION ALL SELECT 'collection_profile_definition', count(*) FROM collection_profile_definition
-- UNION ALL SELECT 'offline_signal_collector_mapping', count(*) FROM offline_signal_collector_mapping
-- UNION ALL SELECT 'offline_resource_sync_state', count(*) FROM offline_resource_sync_state
-- UNION ALL SELECT 'offline_resource_sync_batch', count(*) FROM offline_resource_sync_batch
-- UNION ALL SELECT 'dynamic_resource_active (offline)', count(*) FROM dynamic_resource_active WHERE resource_type IN ('collection_profile','collector')
-- UNION ALL SELECT 'diagnosis_run', count(*) FROM diagnosis_run
-- UNION ALL SELECT 'signal_evaluation', count(*) FROM signal_evaluation
-- UNION ALL SELECT 'diagnosis_candidate', count(*) FROM diagnosis_candidate
-- UNION ALL SELECT 'evidence_item', count(*) FROM evidence_item
-- ;
