#!/usr/bin/env python3
"""synthetic 批量编译管道（T1）：published KBD -> synthetic Bundle，零手工 JSON。

流程：
  1. 调用 hci-sim compile-batch（与 bootstrap 同一编译内核）从 C1 权威快照批量编译；
  2. 将产物复制到 Helm files 目录（已有 realistic manifest 的 KBD 跳过，realistic 优先）；
  3. --update-values 时同步 values.yaml 的 fixture.manifestFiles（保留注释与 realistic 条目）。

不签 lease、不写数据库；values.yaml 与 files/ 变更仍需人工 PR 审查（GitOps 门禁不变）。
环境变量与 scripts/hci-sim/diagnosis-lab.py 保持一致：
  HCI_SIM_CAPABILITIES_URL（默认 http://127.0.0.1:18004/api/kb/hci-sim/capabilities）
  INTERNAL_API_TOKEN（默认 hci-dev-internal-token）
  HCI_SIM_BIN（可选，指定已编译的 hci-sim 二进制）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HELM_FILES_DEFAULT = REPO_ROOT / "deploy/helm/hci-sim/files"
VALUES_DEFAULT = REPO_ROOT / "deploy/helm/hci-sim/values.yaml"

SYNTHETIC_SUFFIX = "-synthetic-fixture-manifest.json"
REALISTIC_SUFFIX = "-fixture-manifest.json"


def resolve_binary(explicit: str | None) -> str:
    """按 显式参数 > HCI_SIM_BIN > 本地 go build > docker golang 顺序解析编译器。"""

    if explicit:
        if not Path(explicit).is_file():
            raise SystemExit(f"--bin 指定的二进制不存在: {explicit}")
        return explicit
    env_bin = os.environ.get("HCI_SIM_BIN", "").strip()
    if env_bin and Path(env_bin).is_file():
        return env_bin
    local = REPO_ROOT / "hci_sim" / "hci-sim"
    if not local.is_file():
        local.parent.mkdir(parents=True, exist_ok=True)
        if shutil.which("go"):
            subprocess.run(
                ["go", "build", "-o", str(local), "./cmd/hci-sim"],
                cwd=REPO_ROOT / "hci_sim",
                check=True,
            )
        else:
            subprocess.run(
                [
                    "docker", "run", "--rm",
                    "-v", f"{REPO_ROOT / 'hci_sim'}:/src", "-w", "/src",
                    "-e", "GOFLAGS=-buildvcs=false",
                    "golang:1.20", "go", "build", "-o", "/src/hci-sim", "./cmd/hci-sim",
                ],
                check=True,
            )
    if not local.is_file():
        raise SystemExit("hci-sim 二进制编译失败；请显式传 --bin 或 HCI_SIM_BIN")
    return str(local)


def run_compile_batch(
    binary: str,
    capabilities_url: str,
    api_token: str,
    output_dir: Path,
    sample_suite: str,
) -> dict:
    """执行批量编译并返回 batch-report.json 内容。"""

    cmd = [
        binary, "compile-batch",
        "--capabilities-url", capabilities_url,
        "--api-token", api_token,
        "--output-dir", str(output_dir),
    ]
    if sample_suite:
        cmd += ["--sample-suite", sample_suite]
    result = subprocess.run(cmd, check=False, text=True, capture_output=True)
    sys.stderr.write(result.stderr or "")
    if result.returncode != 0:
        sys.stderr.write(result.stdout or "")
        raise SystemExit(f"compile-batch 退出码 {result.returncode}")
    report_path = output_dir / "batch-report.json"
    if not report_path.is_file():
        raise SystemExit(f"编译器未产出报告: {report_path}")
    return json.loads(report_path.read_text(encoding="utf-8"))


def realistic_ids_in(helm_files_dir: Path) -> set[str]:
    """已有 realistic manifest 的 support_id 集合；realistic 是黄金证据，优先于 synthetic。"""

    ids: set[str] = set()
    for path in helm_files_dir.glob(f"kbd-*{REALISTIC_SUFFIX}"):
        if path.name.endswith(SYNTHETIC_SUFFIX):
            continue
        support_id = path.name[len("kbd-") : -len(REALISTIC_SUFFIX)]
        if support_id:
            ids.add(support_id)
    return ids


def copy_to_helm_files(output_dir: Path, helm_files_dir: Path, dry_run: bool) -> list[str]:
    """把 synthetic manifest 复制进 Helm files 目录，返回新增的相对路径列表。"""

    copied: list[str] = []
    protected = realistic_ids_in(helm_files_dir)
    for manifest in sorted(output_dir.glob(f"kbd-*{SYNTHETIC_SUFFIX}")):
        support_id = manifest.name[len("kbd-") : -len(SYNTHETIC_SUFFIX)]
        if support_id in protected:
            print(f"  skip-copy {support_id}: 已存在 realistic manifest", file=sys.stderr)
            continue
        target = helm_files_dir / manifest.name
        if dry_run:
            print(f"  dry-run copy {manifest.name} -> {target}", file=sys.stderr)
        else:
            shutil.copyfile(manifest, target)
        copied.append(f"files/{manifest.name}")
    return copied


def update_values_manifest_files(values_path: Path, helm_files_dir: Path, synthetic_entries: list[str], dry_run: bool) -> bool:
    """重写 values.yaml 中 fixture.manifestFiles：保留注释、realistic 条目，追加 synthetic 条目。"""

    text = values_path.read_text(encoding="utf-8")
    pattern = re.compile(r"(?m)^([ \t]*manifestFiles:\n)((?:[ \t]+- .*\n)+)")
    match = pattern.search(text)
    if not match:
        raise SystemExit(f"values.yaml 中未找到 fixture.manifestFiles 列表: {values_path}")

    def entry_name(line: str) -> str:
        return line.strip()[2:].strip()

    entries = [entry_name(line) for line in match.group(2).splitlines()]
    realistic = [e for e in entries if not e.endswith(SYNTHETIC_SUFFIX)]
    synthetic = sorted({e for e in synthetic_entries if (helm_files_dir / Path(e).name).is_file()})
    merged = realistic + synthetic
    indent = re.match(r"[ \t]+", match.group(2)).group(0)
    replacement = match.group(1) + "".join(f"{indent}- {e}\n" for e in merged)
    if replacement == match.group(0):
        return False
    if not dry_run:
        values_path.write_text(text[: match.start()] + replacement + text[match.end() :], encoding="utf-8")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--capabilities-url", default=os.environ.get("HCI_SIM_CAPABILITIES_URL", "http://127.0.0.1:18004/api/kb/hci-sim/capabilities"))
    parser.add_argument("--api-token", default=os.environ.get("INTERNAL_API_TOKEN", "hci-dev-internal-token"))
    parser.add_argument("--sample-suite", default="", help="仅编译该 sample_suite 的 KBD（空=全部）")
    parser.add_argument("--output-dir", default=str(REPO_ROOT / ".hci-sim-batch"))
    parser.add_argument("--bin", default=None, help="hci-sim 二进制路径（默认自动解析/编译）")
    parser.add_argument("--helm-files-dir", default=str(HELM_FILES_DEFAULT))
    parser.add_argument("--update-values", action="store_true", help=f"同步 values.yaml 的 fixture.manifestFiles（默认 {VALUES_DEFAULT}）")
    parser.add_argument("--values-file", default=str(VALUES_DEFAULT))
    parser.add_argument("--dry-run", action="store_true", help="不写入 Helm files 与 values.yaml")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    helm_files_dir = Path(args.helm_files_dir)
    values_path = Path(args.values_file)

    # 编译前先探测 C1 服务，给出可操作的错误而不是 HTTP 000。
    probe = urllib.request.Request(args.capabilities_url, headers={"Authorization": f"Bearer {args.api_token}"})
    try:
        with urllib.request.urlopen(probe, timeout=8) as response:
            if response.status != 200:
                raise SystemExit(f"C1 服务探测失败: HTTP {response.status}")
    except SystemExit:
        raise
    except Exception as error:  # noqa: BLE001 - 统一转成可操作提示
        raise SystemExit(f"无法访问 C1 服务 {args.capabilities_url}: {error}\n请确认 kb-service 已启动且 INTERNAL_API_TOKEN 正确") from error

    binary = resolve_binary(args.bin)
    report = run_compile_batch(binary, args.capabilities_url, args.api_token, output_dir, args.sample_suite)
    compiled = report.get("compiled", [])
    skipped = report.get("skipped", [])
    print(f"compile-batch 完成: total={report.get('total')} compiled={len(compiled)} skipped={len(skipped)}")

    if not compiled:
        print("没有可编译的 ready KBD；跳过 Helm files 与 values.yaml 同步")
        return

    helm_files_dir.mkdir(parents=True, exist_ok=True)
    copied = copy_to_helm_files(output_dir, helm_files_dir, args.dry_run)
    if copied:
        print(f"Helm files {'(dry-run) ' if args.dry_run else ''}新增 {len(copied)} 个 synthetic manifest")
    if args.update_values:
        changed = update_values_manifest_files(values_path, helm_files_dir, copied, args.dry_run)
        print(f"values.yaml manifestFiles {'已更新' if changed and not args.dry_run else '无变化' if not changed else '(dry-run) 将更新'}")
    print("后续: git add deploy/helm/hci-sim 并走 PR 审查（GitOps 门禁不变）")


if __name__ == "__main__":
    main()
