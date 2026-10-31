#!/usr/bin/env python3
"""Alloy(River) 配置语法门禁：把渲染出来的 config.alloy 交给真正的解析器。

为什么要这个门禁（2026-10-31 staging 事故）：
  合并 dc90ccf6 后 alloy 立刻 CrashLoopBackOff，staging 日志采集中断约 3 分钟。
  根因是重写 `loki.process "ingress_logs"` 时把 stage.json 与
  stage.structured_metadata 两张表的尾逗号全删了 —— River 的表项必须以逗号分隔，
  漏一个就 `missing ',' in field list`，Alloy 直接 `could not perform the initial
  load successfully`。
  而当时的检查只做正则白名单（「structured_metadata.values 里有没有 duration 这个键」），
  从不解析语法，于是它给了一个假绿。正则能证明「我写了我想写的字」，证明不了
  「这串字解析器接受」—— 后者只能交给解析器本身。

判据为什么是 `alloy fmt` 而不是 `alloy fmt --test`：
  --test 除了语法还要求「格式已经符合 alloy 自己的排版风格」，我们这份 helm
  渲染结果的缩进本来就不是它的风格。2026-10-31 实测：语法修好后 `fmt` rc=0，
  而 `fmt --test` 仍报 `not formatted correctly` —— 用 --test 会得到与语法无关的
  永久假红。所以这里只取 `alloy fmt`：解析失败才非 0。

为什么每次都要双向自证：
  一个只返回「通过」的门禁，和没有门禁等价。所以每次运行先跑两份常量夹具：
    - 缺逗号的夹具必须失败，且错误里出现 `missing ','`（确认解析器真的在解析）；
    - 逗号补齐的同一份夹具必须成功（确认不是把一切都判失败）。
  自证不过就整体失败 —— 「门禁自己坏了」必须响亮地报出来，不能退化成「配置没问题」。

解析器版本不写死：
  镜像坐标从渲染结果里 Alloy DaemonSet 的第一个容器取，与线上同版本，配置改了
  不会漂移。渲染结果里没有 DaemonSet 视为门禁对象丢失，直接失败。

用法：
  python3 scripts/ci/check_alloy_syntax.py /tmp/obs-rendered.yaml

  --parser-cmd 仅供本机没有 docker 时换成等价解析器（例如
  `kubectl -n hci-observability exec -i <pod> -- alloy fmt`），CI 不得传该参数。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

# 自证夹具：两份只差逗号，除此之外完全相同。
# 保持最小 —— 夹具不随被测配置变化，才不会被「配置改了就顺带失效」。
FIXTURE_MISSING_COMMA = """\
loki.process "anchor" {
  stage.json {
    expressions = {
      first  = "First"
      second = "Second"
    }
  }
}
"""

FIXTURE_VALID = """\
loki.process "anchor" {
  stage.json {
    expressions = {
      first  = "First",
      second = "Second",
    }
  }
}
"""

PARSER_TIMEOUT = 180


def _docs(path: Path) -> list[dict]:
    out = []
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if isinstance(doc, dict) and doc.get("kind"):
            out.append(doc)
    return out


def _extract(path: Path) -> tuple[str, str]:
    """从渲染结果里取出 (alloy 镜像, config.alloy 文本)。"""
    docs = _docs(path)
    alloy_cfg = ""
    for doc in docs:
        if doc.get("kind") == "ConfigMap":
            data = doc.get("data") or {}
            if data.get("config.alloy"):
                alloy_cfg = data["config.alloy"]
    image = ""
    for doc in docs:
        if doc.get("kind") == "DaemonSet" and doc.get("metadata", {}).get("name") == "alloy":
            containers = doc["spec"]["template"]["spec"].get("containers") or []
            if containers:
                image = containers[0].get("image", "")
    return image, alloy_cfg


def _parse(parser_cmd: str, config: str) -> tuple[int, str]:
    """把配置从 stdin 喂给解析器，返回 (rc, 合并后的 stderr+stdout)。"""
    proc = subprocess.run(
        parser_cmd,
        shell=True,
        input=config,
        capture_output=True,
        text=True,
        timeout=PARSER_TIMEOUT,
    )
    return proc.returncode, (proc.stderr or "") + (proc.stdout or "")


def check(path: Path, parser_cmd: str | None) -> list[str]:
    image, alloy_cfg = _extract(path)
    if not alloy_cfg:
        return [f"{path}: 渲染结果里找不到 config.alloy 的 ConfigMap，检查对象丢了"]

    if parser_cmd is None:
        if not shutil.which("docker"):
            return [
                "找不到 docker，无法启动 alloy 解析器。"
                "本机没有 docker 时用 --parser-cmd 指定等价解析器；CI 里必须走 docker。"
            ]
        if not image:
            return [f"{path}: 渲染结果里找不到 alloy DaemonSet，取不到解析器镜像（门禁对象丢失）"]
        # -i 让 stdin 透传：解析器从 stdin 读配置，这样无需挂载文件、也不受路径影响
        parser_cmd = f"docker run --rm -i {image} fmt"

    problems: list[str] = []
    # 第一道自证：解析器必须能判失败。失败原因还要对得上，
    # 否则「镜像拉不下来」「命令拼错」都会被误当成「检出语法错误」。
    rc_bad, out_bad = _parse(parser_cmd, FIXTURE_MISSING_COMMA)
    if rc_bad == 0:
        problems.append(
            "自证失败：缺逗号的夹具被解析器判成合法。解析器没在工作（或不校验语法），"
            f"本次检查不可信。解析器输出：{out_bad.strip()[:400]}"
        )
    elif "missing ','" not in out_bad:
        problems.append(
            "自证失败：夹具确实失败了，但错误不是 `missing ',' in field list`，"
            f"说明失败来自解析器自身（镜像/命令问题）而不是语法。解析器输出：{out_bad.strip()[:400]}"
        )
    if problems:
        return problems

    # 第二道自证：同一个夹具补上逗号必须通过。缺了这道，一个「永远判失败」的
    # 解析器也能满足第一道自证，那同样会把所有配置都拦住。
    rc_good, out_good = _parse(parser_cmd, FIXTURE_VALID)
    if rc_good != 0:
        problems.append(
            "自证失败：合法夹具被判成错误，解析器或调用方式有问题（会拦住所有配置）。"
            f"解析器输出：{out_good.strip()[:400]}"
        )
        return problems

    # 真正的检查项：线上的那份配置文件。
    rc, out = _parse(parser_cmd, alloy_cfg)
    if rc != 0:
        detail = "\n".join(line for line in out.strip().splitlines() if line.strip())[:1500]
        problems.append(
            "渲染出来的 config.alloy 无法被 Alloy 解析，上线即 CrashLoop"
            "（2026-10-31 就是这样把 staging 日志采集停了 3 分钟）。解析器输出：\n" + detail
        )
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description="Alloy 配置语法真解析门禁")
    ap.add_argument("rendered", type=Path, help="helm template 渲染出来的 yaml")
    ap.add_argument("--parser-cmd", default=None, help="覆盖默认解析器命令（stdin 进、rc 出）")
    args = ap.parse_args()

    if not args.rendered.is_file():
        print(f"输入文件不存在：{args.rendered}")
        return 1

    problems = check(args.rendered, args.parser_cmd)
    if problems:
        print("Alloy 配置语法门禁未通过：")
        for item in problems:
            print("  -", item)
        return 1

    print("✅ config.alloy 通过 alloy fmt 解析，且解析器双向自证通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
