#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

cat >"$tmp_dir/psql" <<'FAKE_PSQL'
#!/usr/bin/env bash
set -euo pipefail

url="${1:?missing database URL}"
shift
if [ "$url" = "kbd://test" ]; then
  printf '27123\tpublished\t25\n99999\tpublished\t1\n'
  exit 0
fi

[ "$url" = "sim://test" ] || exit 64
query="$(cat)"
grep -Fq 'SELECT b.status, b.digest' <<<"$query"
if [ "${SIM_QUERY_FAIL:-0}" = "1" ]; then
  printf 'simulated SQL failure\n' >&2
  exit 42
fi

support_id=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "-v" ] && [ "$#" -ge 2 ] && [[ "$2" == support_id=* ]]; then
    support_id="${2#support_id=}"
    break
  fi
  shift
done

if [ "$support_id" = "27123" ]; then
  printf 'published\tsha256:c3bc5ad3c5d45aaa275e5e26acf1aa8e88ea7d632c13edafa5d67e8ffe4c77c7\n'
fi
FAKE_PSQL
chmod +x "$tmp_dir/psql"

output="$({
  PATH="$tmp_dir:$PATH" \
    HCI_KBD_DATABASE_URL='kbd://test' \
    HCI_SIM_DATABASE_URL='sim://test' \
    "$repo_root/scripts/hci-sim/capability-matrix.sh"
})"

grep -Fq $'27123\tpublished\t25\tpublished\tsha256:c3bc5ad3c5d45aaa275e5e26acf1aa8e88ea7d632c13edafa5d67e8ffe4c77c7\tpending\tpending\tpending\t\t\t' <<<"$output"
grep -Fq $'99999\tpublished\t1\tmissing\t\tpending\tpending\tpending\tcapability_gap: no published immutable Bundle\t\t' <<<"$output"

if PATH="$tmp_dir:$PATH" \
  HCI_KBD_DATABASE_URL='kbd://test' \
  HCI_SIM_DATABASE_URL='sim://test' \
  SIM_QUERY_FAIL=1 \
  "$repo_root/scripts/hci-sim/capability-matrix.sh" >/dev/null 2>&1; then
  echo 'capability matrix must fail when the Bundle query fails' >&2
  exit 1
fi

echo 'capability matrix tests passed'
