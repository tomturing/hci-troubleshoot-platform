#!/bin/sh
# 将旧 hci_troubleshoot.public.agent_test_* 控制面表复制到 hci_sim。
# 这是一次性、显式授权的 expand/copy 步骤：默认只做 inventory，绝不 DROP 源表。
set -eu

: "${SOURCE_DATABASE_URL:?SOURCE_DATABASE_URL must point to hci_troubleshoot}"
: "${TARGET_DATABASE_URL:?TARGET_DATABASE_URL must point to hci_sim}"
if [ "${HCI_SIM_ALLOW_COPY:-}" != "1" ]; then
  echo "拒绝执行复制：设置 HCI_SIM_ALLOW_COPY=1 后重试（源表不会被删除）" >&2
  exit 2
fi

tmp_dir=$(mktemp -d)
trap 'rm -rf "$tmp_dir"' EXIT INT TERM

tables="
agent_test_scenario
agent_test_fixture_bundle
agent_test_fixture_dependency
agent_test_fixture_provenance
agent_test_fixture_approval
agent_test_fixture_audit
agent_test_fixture_stale_outbox
agent_test_artifact
agent_test_artifact_scan
agent_test_artifact_approval
agent_test_run
agent_test_run_attempt
agent_test_run_event
agent_test_run_result
agent_test_runtime_instance
"

map_table() {
  case "$1" in
    agent_test_scenario) printf '%s' control_plane.scenario ;;
    agent_test_fixture_bundle) printf '%s' fixture.bundle ;;
    agent_test_fixture_dependency) printf '%s' fixture.dependency ;;
    agent_test_fixture_provenance) printf '%s' fixture.provenance ;;
    agent_test_fixture_approval) printf '%s' fixture.approval ;;
    agent_test_fixture_audit) printf '%s' audit.entity_event ;;
    agent_test_fixture_stale_outbox) printf '%s' fixture.stale_outbox ;;
    agent_test_artifact) printf '%s' artifact.metadata ;;
    agent_test_artifact_scan) printf '%s' artifact.scan ;;
    agent_test_artifact_approval) printf '%s' artifact.approval ;;
    agent_test_run) printf '%s' control_plane.run ;;
    agent_test_run_attempt) printf '%s' control_plane.run_attempt ;;
    agent_test_run_event) printf '%s' control_plane.run_event ;;
    agent_test_run_result) printf '%s' control_plane.run_result ;;
    agent_test_runtime_instance) printf '%s' control_plane.runtime_instance ;;
  esac
}

echo "源库: ${SOURCE_DATABASE_URL%%\?*}"
echo "目标库: ${TARGET_DATABASE_URL%%\?*}"
echo "表级 inventory 与复制开始（源表只读）"
for table in $tables; do
  exists=$(psql "$SOURCE_DATABASE_URL" -Atqc "SELECT to_regclass('public.$table') IS NOT NULL")
  [ "$exists" = t ] || { echo "skip $table (source table absent)"; continue; }
  target=$(map_table "$table")
  count=$(psql "$SOURCE_DATABASE_URL" -Atqc "SELECT count(*) FROM public.\"$table\"")
  echo "copy $table -> $target rows=$count"
  pg_dump "$SOURCE_DATABASE_URL" --data-only --column-inserts --no-owner --no-privileges --table="public.$table" \
    >"$tmp_dir/$table.sql"
  # pg_dump 的目标列顺序与迁移定义一致；只替换表名和序列名，不执行任意 SQL。
  sed -i \
    -e "s/public\\.${table}/${target}/g" \
    -e "s/public\\.${table}_/${target##*.}_/g" \
    "$tmp_dir/$table.sql"
  psql "$TARGET_DATABASE_URL" -v ON_ERROR_STOP=1 -f "$tmp_dir/$table.sql"
  target_count=$(psql "$TARGET_DATABASE_URL" -Atqc "SELECT count(*) FROM $target")
  [ "$count" = "$target_count" ] || { echo "count mismatch $table source=$count target=$target_count" >&2; exit 1; }
done

echo "复制完成：逐表行数一致；源表保留，下一步必须执行观察窗口与权限/恢复验证。"
