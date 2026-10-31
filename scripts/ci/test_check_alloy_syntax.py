"""check_alloy_syntax 的回归测试。

为什么这个门禁自己必须有测试：
  2026-10-31 的事故不是「配置写错」，而是「检查器只会说通过」。当时的门禁用正则
  查 `structured_metadata.values` 里有没有 duration 这个键，语法错了它照样绿。
  所以这里的重点不是「能不能判出错」，而是下面这些**会让门禁静默失真**的方向：

  1) 解析器永远返回 0（命令写错、包装了 `cat`、镜像里换成 echo）——必须报
     「自证失败」，绝不能一路走到「配置没问题」；
  2) 解析器因为环境问题失败（镜像拉不下来、daemon 没起）——错误文案里没有
     `missing ','`，不能当成「检出语法错误」，否则坏门禁会被当成好门禁；
  3) 解析器永远返回非 0——同样是被拦住，但拦截方式错误，必须报出来；
  4) 渲染结果里 ConfigMap / DaemonSet 丢了——检查对象丢失必须失败，不能对空
     输入沉默通过。

夹具解析器用 shim 而非真 alloy：这层测的是**调用与判定逻辑**，真解析器的语义
（`alloy fmt` 解析失败才非 0，`--test` 额外要求排版）已在 staging 用
grafana/alloy:v1.10.2 实测过，并由 CI 每次运行时的双向自证继续兜住。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import yaml

_SCRIPT = Path(__file__).resolve().parent / "check_alloy_syntax.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_alloy_syntax", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VALID_CONFIG = """\
loki.process "ingress_logs" {
  stage.json {
    expressions = {
      downstream_status = "DownstreamStatus",
      backend_service   = "ServiceName",
    }
  }
}
"""

# 只删一个逗号：精确复现 2026-10-31 事故的形态
BROKEN_CONFIG = VALID_CONFIG.replace('"DownstreamStatus",', '"DownstreamStatus"')


def _render(tmp_path: Path, config: str | None, with_daemonset: bool = True) -> Path:
    docs = []
    if config is not None:
        docs.append(
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "alloy-config"},
                "data": {"config.alloy": config},
            }
        )
    if with_daemonset:
        docs.append(
            {
                "apiVersion": "apps/v1",
                "kind": "DaemonSet",
                "metadata": {"name": "alloy"},
                "spec": {"template": {"spec": {"containers": [{"name": "alloy", "image": "grafana/alloy:v1.10.2"}]}}},
            }
        )
    path = tmp_path / "rendered.yaml"
    path.write_text(yaml.safe_dump_all(docs), encoding="utf-8")
    return path


def _shim(tmp_path: Path) -> str:
    """按 River 的规则判逗号：表项缺逗号时输出 alloy 的真实报错并 rc=1。

    判据必须与真解析器同形：形态是「`缩进 + 键 = \"字符串\"` 行尾没有逗号」。
    第一版这里写死了被测配置里的某个键名，结果连门禁自带的自证夹具都判不出来——
    测试当场就红了（shim 和解析器不同形，测出来的东西就是假的）。
    """
    path = tmp_path / "shim-alloy.sh"
    path.write_text(
        "#!/bin/sh\n"
        "cfg=$(cat)\n"
        'if printf \'%s\\n\' "$cfg" | grep -Eq \'^[[:space:]]*[a-z_]+[[:space:]]*=[[:space:]]*"[^"]*"$\'; then\n'
        "  echo \"<stdin>:3:45: missing ',' in field list\" >&2\n"
        "  exit 1\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return str(path)


def test_valid_config_passes(tmp_path):
    m = _load()
    rendered = _render(tmp_path, VALID_CONFIG)
    assert m.check(rendered, _shim(tmp_path)) == []


def test_missing_comma_is_caught(tmp_path):
    m = _load()
    rendered = _render(tmp_path, BROKEN_CONFIG)
    problems = m.check(rendered, _shim(tmp_path))
    assert len(problems) == 1
    assert "无法被 Alloy 解析" in problems[0]
    assert "missing ','" in problems[0]


def test_parser_that_never_fails_is_caught(tmp_path):
    """最常见的坏门禁形态：解析器永远 rc=0，检查器于是永远说「没问题」。"""
    m = _load()
    rendered = _render(tmp_path, BROKEN_CONFIG)
    problems = m.check(rendered, "cat")
    assert len(problems) == 1
    assert "自证失败" in problems[0]
    assert "判成合法" in problems[0]


def test_parser_environment_failure_is_not_mistaken_for_syntax_error(tmp_path):
    """镜像拉不下来 / docker daemon 没起：错误里不含 missing ','，不能被当成「检出 bug」。"""
    m = _load()
    rendered = _render(tmp_path, BROKEN_CONFIG)
    problems = m.check(rendered, "echo 'Cannot connect to the Docker daemon' >&2; exit 1")
    assert len(problems) == 1
    assert "解析器自身" in problems[0]


def test_parser_that_always_fails_is_caught(tmp_path):
    """永远判失败同样会把所有配置拦住，必须报「合法夹具被判成错误」。"""
    m = _load()
    rendered = _render(tmp_path, VALID_CONFIG)
    problems = m.check(rendered, "cat >/dev/null; echo \"missing ',' in field list\" >&2; exit 1")
    assert len(problems) == 1
    assert "合法夹具被判成错误" in problems[0]


def test_missing_config_alloy_fails(tmp_path):
    m = _load()
    rendered = _render(tmp_path, None)
    problems = m.check(rendered, _shim(tmp_path))
    assert problems and "找不到 config.alloy" in problems[0]


def test_missing_daemonset_fails_so_image_cannot_drift(tmp_path, monkeypatch):
    """解析器镜像从 DaemonSet 取；取不到就必须失败，不能悄悄换一个版本。"""
    m = _load()
    rendered = _render(tmp_path, VALID_CONFIG, with_daemonset=False)
    monkeypatch.setattr(m.shutil, "which", lambda _name: "/usr/bin/docker")
    problems = m.check(rendered, None)
    assert problems and "取不到解析器镜像" in problems[0]


def test_missing_input_file_exits_nonzero(tmp_path, monkeypatch):
    m = _load()
    monkeypatch.setattr(sys, "argv", ["check_alloy_syntax.py", str(tmp_path / "nope.yaml")])
    assert m.main() == 1
