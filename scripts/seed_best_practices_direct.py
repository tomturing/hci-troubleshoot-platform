#!/usr/bin/env python3
"""
scripts/seed_best_practices_direct.py
读取已发布的 23 个 KBD 终稿信号，生成标准 SQL 并直接导入 staging 数据库 signal_best_practice 表。
"""
import json
import subprocess
import sys


def infer_pattern_category(tool: str, signal: dict) -> str:
    acquire = signal.get("acquire") or {}
    args = acquire.get("args") or {}
    match = signal.get("match") or {}
    match_type = match.get("type") if isinstance(match, dict) else "none"

    if tool == "qkv_task":
        kw = args.get("keyword", "")
        return f"任务失败捕获与变量提取({kw[:15]})"
    elif tool == "qkv_alert":
        return "平台异步告警查询"
    elif tool == "qkv_dialog":
        return "前端弹框复合取值"
    elif tool == "qfk_log":
        file_name = args.get("file", "unknown")
        if match_type == "threshold":
            return f"日志数值提取与阈值计算({file_name})"
        elif match_type == "regex":
            return f"日志正则模式匹配({file_name})"
        else:
            return f"日志关键错误行判定({file_name})"
    elif tool == "qfk_system":
        cmd = args.get("command", "")
        if match_type == "threshold":
            return f"系统命令输出阈值判定({cmd})"
        return f"系统命令状态检查({cmd})"
    elif tool == "qfk_vm":
        return "虚拟机只读配置与状态检查"
    elif tool == "qfk_storage":
        return "存储卷与磁盘状态探针"
    return f"{tool}标准排查探针"


def main():
    json_path = "/home/node/.gemini/antigravity-ide/brain/7da02bdb-c3a1-4e7c-9222-5e51c94c887f/scratch/kbd_published_23.json"
    with open(json_path, "r", encoding="utf-8") as f:
        kbds = json.load(f)

    # 1. 建立 template 映射 (由之前 migration 插入的 13 类)
    template_map_query = "SELECT id, tool_name FROM signal_modeling_template;"
    proc = subprocess.run(
        ["kubectl", "exec", "-i", "-n", "hci-staging", "postgres-0", "--",
         "psql", "-U", "hci_admin", "-d", "hci_troubleshoot", "-t", "-A", "-c", template_map_query],
        capture_output=True, text=True, check=True
    )
    template_ids = {}
    for line in proc.stdout.strip().split("\n"):
        if "|" in line:
            tid, tname = line.split("|")
            template_ids[tname.strip()] = int(tid.strip())

    print(f"Loaded {len(template_ids)} template mappings from database.")

    # 2. 构造 INSERT SQL
    sql_lines = [
        "BEGIN;",
        "DELETE FROM signal_best_practice WHERE source_kbd_id IS NOT NULL;"
    ]

    total_inserted = 0
    for kbd in kbds:
        kbd_id = kbd["id"]
        support_id = kbd["support_id"]
        title = kbd["title"]
        final_sigs_doc = kbd.get("final_signals") or {}
        sigs = final_sigs_doc.get("signals") if isinstance(final_sigs_doc, dict) else (final_sigs_doc if isinstance(final_sigs_doc, list) else [])

        for sig in sigs:
            if not isinstance(sig, dict):
                continue
            acquire = sig.get("acquire") or {}
            tool = acquire.get("tool")
            if not tool:
                continue

            template_id = template_ids.get(tool, "NULL")
            pattern_cat = infer_pattern_category(tool, sig).replace("'", "''")
            evidence = ((sig.get("provenance") or {}).get("evidence") or (acquire.get("args") or {}).get("instruction") or title).replace("'", "''")
            notes = f"来源：已发布 KBD {support_id} 专家最终审核版本".replace("'", "''")
            sig_json_str = json.dumps(sig, ensure_ascii=False).replace("'", "''")

            sql_lines.append(f"""
INSERT INTO signal_best_practice (
    template_id, tool_name, pattern_category, source_kbd_id, support_id,
    raw_evidence, signal_json, design_notes, completeness_score, trace_id
) VALUES (
    {template_id}, '{tool}', '{pattern_cat}', {kbd_id}, '{support_id}',
    '{evidence}', '{sig_json_str}'::jsonb, '{notes}', 10, 'seed-best-practice:{support_id}:{total_inserted}'
);
""")
            total_inserted += 1

    sql_lines.append("COMMIT;")
    full_sql = "\n".join(sql_lines)

    print(f"Generated SQL for {total_inserted} best practices. Executing on postgres-0...")
    proc_insert = subprocess.run(
        ["kubectl", "exec", "-i", "-n", "hci-staging", "postgres-0", "--",
         "psql", "-U", "hci_admin", "-d", "hci_troubleshoot"],
        input=full_sql, capture_output=True, text=True
    )
    if proc_insert.returncode != 0:
        print(f"Failed: {proc_insert.stderr}", file=sys.stderr)
        sys.exit(1)

    print(f"Successfully seeded {total_inserted} golden instances into signal_best_practice table!")


if __name__ == "__main__":
    main()
