#!/usr/bin/env bash
set -euo pipefail

# Stage-E controlled load harness. It is deliberately opt-in: running it
# against a real Runtime can create many TestRuns and must be explicitly
# authorized by the operator.
if [ "${HCI_SIM_STAGE_E_ENABLE:-}" != "1" ]; then
  echo 'blocked: set HCI_SIM_STAGE_E_ENABLE=1 after approving the target environment' >&2
  exit 2
fi
: "${HCI_SIM_CONTROL_URL:?请设置 HCI_SIM_CONTROL_URL，例如 http://api-gateway/api/hci-sim}"
: "${HCI_SIM_KBD_ID:?请设置 HCI_SIM_KBD_ID}"
: "${HCI_SIM_CONTROL_TOKEN:?请设置服务间控制 Token（不会打印）}"
repeat="${HCI_SIM_REPEAT:-20}"
capacity_list="${HCI_SIM_CAPACITY_LIST:-1,10,50,100,200}"
case "$repeat" in (*[!0-9]*|'') echo 'HCI_SIM_REPEAT must be a positive integer' >&2; exit 2;; esac

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT
printf 'capacity\titeration\tslot\ttest_run_id\tresult\n' | tee "$tmp_dir/results.tsv"

run_one() {
  local capacity="$1" iteration="$2" slot="$3"
  local idempotency response test_run_id
  idempotency="stage-e-${capacity}-${iteration}-${slot}-$(date +%s%N)"
  response="$(curl --fail-with-body --silent --show-error --max-time 30 \
    -X POST "${HCI_SIM_CONTROL_URL%/}/v1/simulations/build" \
    -H "Authorization: Bearer ${HCI_SIM_CONTROL_TOKEN}" \
    -H 'Content-Type: application/json' -H "Idempotency-Key: $idempotency" \
    --data "{\"kbd_id\":\"$HCI_SIM_KBD_ID\"}")" || {
      printf '%s\t%s\t%s\t\tbuild_failed\n' "$capacity" "$iteration" "$slot" >>"$tmp_dir/results.tsv"
      return
    }
  test_run_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("test_run_id", ""))' <<<"$response")"
  if [ -z "$test_run_id" ]; then
    printf '%s\t%s\t%s\t\tmissing_test_run_id\n' "$capacity" "$iteration" "$slot" >>"$tmp_dir/results.tsv"
    return
  fi
  printf '%s\t%s\t%s\t%s\tbuild_passed\n' "$capacity" "$iteration" "$slot" "$test_run_id" >>"$tmp_dir/results.tsv"
}

for capacity in ${capacity_list//,/ }; do
  case "$capacity" in (*[!0-9]*|'0') echo "invalid capacity: $capacity" >&2; exit 2;; esac
  for iteration in $(seq 1 "$repeat"); do
    for slot in $(seq 1 "$capacity"); do
      run_one "$capacity" "$iteration" "$slot" &
    done
    wait
  done
done

sort -t $'\t' -k1,1n -k2,2n -k3,3n "$tmp_dir/results.tsv" -o "$tmp_dir/results.tsv"
duplicate_count="$(awk -F '\t' '$4!="" && $4!="test_run_id" { seen[$4]++ } END { n=0; for (id in seen) if (seen[id]>1) n++ ; print n }' "$tmp_dir/results.tsv")"
if [ "$duplicate_count" != 0 ]; then
  echo "failed: duplicate TestRun IDs detected: $duplicate_count" >&2
  exit 1
fi
evidence_file="${HCI_SIM_EVIDENCE_FILE:-stage-e-$(date -u +%Y%m%dT%H%M%SZ).tsv}"
cp "$tmp_dir/results.tsv" "$evidence_file"
echo "evidence: $evidence_file"
