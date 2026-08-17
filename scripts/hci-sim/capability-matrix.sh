#!/usr/bin/env bash
set -euo pipefail

# 只读生成 KBD 可测性矩阵。数据库中有 KBD 记录不等于存在可运行 Bundle；
# 未找到 approved/published Bundle 的行必须保持 capability_gap。
: "${HCI_KBD_DATABASE_URL:?请设置 HCI_KBD_DATABASE_URL（hci_troubleshoot，只读）}"
: "${HCI_SIM_DATABASE_URL:?请设置 HCI_SIM_DATABASE_URL（hci_sim，只读）}"
command -v psql >/dev/null || { echo "psql is required" >&2; exit 127; }

printf 'support_id\tkbd_status\tactive_revision\tbundle_status\tbundle_digest\tbuild_result\tssh_result\tagent_result\tcapability_gap\towner\tlast_verified\n'
kbd_rows="$(psql "$HCI_KBD_DATABASE_URL" -AtF $'\t' -c "
  SELECT e.support_id, e.status,
         COALESCE((SELECT active_revision::text FROM dynamic_resource_active a
                   WHERE a.resource_type='kbd' AND a.resource_name IN (e.id::text, e.support_id)
                   ORDER BY a.updated_at DESC LIMIT 1), '')
  FROM kbd_entry e WHERE e.status='published' ORDER BY e.support_id
")"

while IFS=$'\t' read -r support_id kbd_status active_revision; do
  [ -n "$support_id" ] || continue
  # Bind database-derived values instead of interpolating them into SQL. The
  # source is expected to be numeric, but imported malformed rows must not turn
  # a read-only evidence tool into an injection primitive.
  bundle_row="$(psql "$HCI_SIM_DATABASE_URL" -v support_id="$support_id" -v active_revision="${active_revision:-0}" -AtF $'\t' <<'SQL'
SELECT b.status, b.digest
FROM control_plane.scenario s
JOIN fixture.bundle b ON b.scenario_id = s.id
WHERE s.support_id = :'support_id' AND s.kbd_revision = :active_revision
  AND s.status = 'published' AND b.status = 'published'
ORDER BY b.updated_at DESC, b.revision DESC
LIMIT 1;
SQL
  )"
  bundle_status="${bundle_row%%$'\t'*}"
  bundle_digest="${bundle_row#*$'\t'}"
  if [ "$bundle_row" = "$bundle_status" ]; then bundle_digest=""; fi
  gap=""
  if [ -z "$bundle_status" ] || [ "$bundle_status" != "published" ]; then
    gap="capability_gap: no published immutable Bundle"
  fi
  printf '%s\t%s\t%s\t%s\t%s\tpending\tpending\tpending\t%s\t\t\n' \
    "$support_id" "$kbd_status" "${active_revision:-}" "${bundle_status:-missing}" "$bundle_digest" "$gap"
done <<<"$kbd_rows"
