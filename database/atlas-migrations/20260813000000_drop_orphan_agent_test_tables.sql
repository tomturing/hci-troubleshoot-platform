-- 删除主库中遗留的 agent_test_* 孤儿表。
--
-- 背景（第一性原理）：hci-sim 已独立成库（见 database/hci-sim-migrations/000001_control_plane.sql），
-- 其 control plane 表以带 schema 前缀（control_plane/fixture/artifact/audit）的新命名在独立库建表，
-- 后端（backend/）对所有 agent_test_* 表零引用（已 grep 确认 0 命中）。主库中的 agent_test_* 仅由
-- 20260806000000/20260806000001 创建、并被 desired_schema.sql 声明，属迁移后遗留的僵尸 schema，
-- 既无业务代码读写，也无跨业务表外键依赖（仅彼此间 ON DELETE RESTRICT 自引用）。
--
-- 本迁移将其从主库彻底移除，使 Atlas 校验范围收敛到真实主库表，避免 hci-sim 改动反复触发主库 CI 漂移。
--
-- 幂等：DROP TABLE IF EXISTS；CASCADE 规避彼此间外键（RESTRICT）导致的删除顺序依赖。

DROP TABLE IF EXISTS
    agent_test_scenario,
    agent_test_artifact,
    agent_test_artifact_scan,
    agent_test_artifact_approval,
    agent_test_fixture_bundle,
    agent_test_fixture_dependency,
    agent_test_fixture_provenance,
    agent_test_fixture_approval,
    agent_test_fixture_audit,
    agent_test_fixture_stale_outbox,
    agent_test_run,
    agent_test_run_attempt,
    agent_test_run_event,
    agent_test_run_result,
    agent_test_runtime_instance
CASCADE;
