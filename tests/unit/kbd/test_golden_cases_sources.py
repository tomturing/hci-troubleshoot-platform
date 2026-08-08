"""KBD Golden Corpus 的来源完整性与本地可用源数据回归。"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from kbd.converter import convert_kbd_structured

REPO_ROOT = Path(__file__).resolve().parents[3]
GOLDEN_ROOT = REPO_ROOT / "tests" / "golden" / "kbd_cases"
CACHE_ROOT = REPO_ROOT / "data-pipeline" / "kbd" / "cache"
MANIFEST = json.loads((GOLDEN_ROOT / "manifest.json").read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _image_set(case_dir: Path) -> list[dict[str, str]]:
    images = sorted(
        (path for path in case_dir.glob("img_*.*") if path.suffix != ".failed"),
        key=lambda path: (int(path.stem.split("_")[1]), path.name),
    )
    return [{"name": path.name, "sha256": _sha256(path)} for path in images]


def _image_set_sha256(images: list[dict[str, str]]) -> str:
    material = json.dumps(images, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(material.encode()).hexdigest()


def test_manifest_contains_exactly_126_unique_requested_cases():
    ids = [item["support_id"] for item in MANIFEST["cases"]]

    assert MANIFEST["required_case_count"] == 126
    assert sum(item["case_count"] for item in MANIFEST["cohorts"]) == 126
    assert len(ids) == len(set(ids)) == 126
    assert {"27123", "37150", "37180", "41818"}.issubset(ids)
    assert all(item.get("source_status") == "available" for item in MANIFEST["cases"])
    assert all(item.get("annotation_status") in {"pending_gold", "gold"} for item in MANIFEST["cases"])
    assert all(
        item.get("review_status") in {"pending_expert", "approved"}
        for item in MANIFEST["cases"]
        if item.get("annotation_status") == "gold"
    )


@pytest.mark.parametrize("case", MANIFEST["cases"], ids=lambda item: item["support_id"])
def test_available_source_integrity_and_conversion(case, monkeypatch):
    """所有纳入语料的源文件都必须匹配不可变指纹并能完整转换。"""
    support_id = case["support_id"]
    case_dir = CACHE_ROOT / support_id
    raw_path = case_dir / "raw.json"
    if not raw_path.exists():
        pytest.skip(f"本 checkout 未提供 {support_id} 原始缓存；严格门禁会拒绝")

    images = _image_set(case_dir)
    integrity_errors: list[str] = []
    if _sha256(raw_path) != case["raw_sha256"]:
        integrity_errors.append("raw_sha256 不匹配")
    if len(images) != case["image_count"]:
        integrity_errors.append("image_count 不匹配")
    if _image_set_sha256(images) != case["image_set_sha256"]:
        integrity_errors.append("image_set_sha256 不匹配")
    if (case_dir / "fetch.failed").exists():
        integrity_errors.append("存在 fetch.failed")
    if list(case_dir.glob("img_*.failed")):
        integrity_errors.append("存在图片下载失败标记")
    if integrity_errors:
        message = f"{support_id} 本地运行缓存不是 manifest 金标准：{'、'.join(integrity_errors)}"
        if os.environ.get("KBD_GOLDEN_STRICT") == "1":
            pytest.fail(message)
        pytest.skip(message)

    monkeypatch.setattr("kbd.converter.settings.KBD_CACHE_DIR", CACHE_ROOT)
    converted = convert_kbd_structured(support_id)
    assert converted is not None
    assert all(str(converted.get(field) or "").strip() for field in (
        "title",
        "problem_description",
        "steps_text",
        "root_cause",
    ))
    assert len(converted["images_json"]) == case["image_count"]
    assert all("context_before" in item and "context_after" in item for item in converted["images_json"])


@pytest.mark.parametrize(
    "case",
    [item for item in MANIFEST["cases"] if item.get("annotation_status") == "gold"],
    ids=lambda item: item["support_id"],
)
def test_available_source_matches_gold_observations(case, monkeypatch):
    support_id = case["support_id"]
    if not (CACHE_ROOT / support_id / "raw.json").exists():
        pytest.skip(f"本 checkout 未提供 {support_id} 原始缓存；严格门禁会拒绝")
    monkeypatch.setattr("kbd.converter.settings.KBD_CACHE_DIR", CACHE_ROOT)
    converted = convert_kbd_structured(support_id)
    gold = json.loads((GOLDEN_ROOT / case["gold"]).read_text(encoding="utf-8"))

    assert converted is not None
    assert gold["title_contains"] in converted["title"]
    source_text = "\n".join(
        str(converted.get(field) or "")
        for field in ("problem_description", "alert_info", "steps_text", "root_cause")
    )
    for token in gold["required_source_tokens"]:
        assert token in source_text
    assert len(converted["images_json"]) >= gold["minimum_images"]
    assert all("context_before" in item and "context_after" in item for item in converted["images_json"])


@pytest.mark.e2e
def test_strict_case_source_annotation_and_review_gate():
    if os.environ.get("KBD_GOLDEN_STRICT") != "1":
        pytest.skip("设置 KBD_GOLDEN_STRICT=1 才执行完整真实数据硬门禁")

    missing_sources = [
        item["support_id"]
        for item in MANIFEST["cases"]
        if not (CACHE_ROOT / item["support_id"] / "raw.json").exists()
    ]
    invalid_sources = []
    for item in MANIFEST["cases"]:
        case_dir = CACHE_ROOT / item["support_id"]
        raw_path = case_dir / "raw.json"
        if not raw_path.exists():
            continue
        images = _image_set(case_dir)
        if (
            _sha256(raw_path) != item["raw_sha256"]
            or len(images) != item["image_count"]
            or _image_set_sha256(images) != item["image_set_sha256"]
            or (case_dir / "fetch.failed").exists()
            or list(case_dir.glob("img_*.failed"))
        ):
            invalid_sources.append(item["support_id"])
    missing_gold = [
        item["support_id"]
        for item in MANIFEST["cases"]
        if item.get("annotation_status") != "gold"
        or not item.get("gold")
        or not (GOLDEN_ROOT / item["gold"]).exists()
    ]
    unapproved_gold = [
        item["support_id"]
        for item in MANIFEST["cases"]
        if item.get("annotation_status") == "gold"
        and item.get("review_status") != "approved"
    ]
    # 一次报告全部阻断，避免第一条失败掩盖其余验收条件。
    blockers = []
    if missing_sources:
        blockers.append(f"缺少真实 raw.json: {missing_sources}")
    if invalid_sources:
        blockers.append(f"本地缓存不匹配 manifest 金标准: {invalid_sources}")
    if missing_gold:
        blockers.append(f"缺少专家 Gold 标注: {missing_gold}")
    if unapproved_gold:
        blockers.append(f"Gold 尚未完成业务专家终审: {unapproved_gold}")
    assert not blockers, "\n".join(blockers)
