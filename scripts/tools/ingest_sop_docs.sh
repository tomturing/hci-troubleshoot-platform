#!/usr/bin/env bash
# 批量将 sop_skills/ 下所有章节 Markdown 文档摄入知识库
# 使用方式: ./scripts/ingest_sop_docs.sh
set -euo pipefail

API_BASE="http://192.168.0.4:4888"
HOST_HEADER="api.192.168.0.4.nip.io"
TOKEN="hci-dev-internal-token"
SOP_DIR="$(cd "$(dirname "$0")/.." && pwd)/data-pipeline/sop_skills"
INGEST_URL="${API_BASE}/api/v1/kb/ingest"

success=0
skip=0
fail=0

ingest_file() {
  local filepath="$1"
  local skill_id="$2"
  local title="$3"
  local category_l1="$4"
  local category_l2="${5:-}"

  local content
  content=$(cat "$filepath")

  # 构建 JSON payload
  local payload
  payload=$(python3 -c "
import json, sys
d = {
  'title': sys.argv[1],
  'content_md': sys.argv[2],
  'source_type': 'sop',
  'category_l1': sys.argv[3],
  'category_l2': sys.argv[4] if sys.argv[4] else None,
  'source_url': 'file://' + sys.argv[5],
}
print(json.dumps(d))
" "$title" "$content" "$category_l1" "$category_l2" "$filepath")

  local resp
  resp=$(curl -s -m30 -X POST \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "Host: ${HOST_HEADER}" \
    -d "$payload" \
    "${INGEST_URL}" 2>&1)

  if echo "$resp" | python3 -c "import sys,json;d=json.load(sys.stdin);exit(0 if 'document_id' in d else 1)" 2>/dev/null; then
    local doc_id chunks
    doc_id=$(echo "$resp" | python3 -c "import sys,json;print(json.load(sys.stdin)['document_id'])")
    chunks=$(echo "$resp" | python3 -c "import sys,json;print(json.load(sys.stdin).get('chunks_created',0))")
    skipped=$(echo "$resp" | python3 -c "import sys,json;print(json.load(sys.stdin).get('skipped',False))")
    if [ "$skipped" = "True" ]; then
      echo "  [SKIP] $title (doc_id=$doc_id, 已存在)"
      ((skip++)) || true
    else
      echo "  [OK]   $title (doc_id=$doc_id, chunks=$chunks)"
      ((success++)) || true
    fi
  else
    echo "  [FAIL] $title: $resp"
    ((fail++)) || true
  fi
}

echo "=========================================="
echo " HCI KB 批量 SOP 摄入"
echo " SOP 目录: $SOP_DIR"
echo " API: $INGEST_URL"
echo "=========================================="

# 遍历每个技能目录
for skill_dir in "$SOP_DIR"/*/; do
  [ -d "$skill_dir" ] || continue
  skill_id=$(basename "$skill_dir")

  # 技能目录分类映射
  case "$skill_id" in
    vm_boot_failure|vm_power_failure) category_l1="虚拟机" ;;
    network_failure) category_l1="网络" ;;
    storage_failure) category_l1="存储" ;;
    node_failure) category_l1="节点" ;;
    *) category_l1="其他" ;;
  esac

  echo ""
  echo "--- 技能: $skill_id ($category_l1) ---"

  # 摄入 chapters/ 下的章节文件
  chapters_dir="${skill_dir}chapters"
  if [ -d "$chapters_dir" ]; then
    for md_file in "$chapters_dir"/*.md; do
      [ -f "$md_file" ] || continue
      filename=$(basename "$md_file" .md)
      title="[SOP-${skill_id}] ${filename}"
      ingest_file "$md_file" "$skill_id" "$title" "$category_l1" "$skill_id"
    done
  fi

  # 摄入技能根目录下直接的 .md 文件（非 chapters 子目录）
  for md_file in "$skill_dir"*.md; do
    [ -f "$md_file" ] || continue
    filename=$(basename "$md_file" .md)
    title="[SOP-${skill_id}] ${filename}"
    ingest_file "$md_file" "$skill_id" "$title" "$category_l1" "$skill_id"
  done
done

# 也摄入 sop_skills 根目录下的零散 .md 文件
echo ""
echo "--- 根目录零散文档 ---"
for md_file in "$SOP_DIR"/*.md; do
  [ -f "$md_file" ] || continue
  filename=$(basename "$md_file" .md)
  title="[SOP] ${filename}"
  # 按文件名猜测分类
  category_l1="其他"
  case "$filename" in
    *vm*|*虚拟机*) category_l1="虚拟机" ;;
    *network*|*网络*) category_l1="网络" ;;
    *storage*|*存储*) category_l1="存储" ;;
    *node*|*节点*) category_l1="节点" ;;
  esac
  ingest_file "$md_file" "root" "$title" "$category_l1" ""
done

echo ""
echo "=========================================="
echo " 完成: 成功=${success} 跳过=${skip} 失败=${fail}"
echo "=========================================="
