"""校验镜像发布顺序，防止回归为 push-before-scan。"""

import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
WORKFLOW = ROOT / ".github/workflows/ci.yml"
OPENCLAW_WORKFLOW = ROOT / ".github/workflows/build-hci-openclaw.yml"
TRIVY_ACTION_SHA = "2736533278103862a861f4a35ebac3e97854d956"


def _block(text: str, start_marker: str, end_marker: str) -> str:
    start = text.index(start_marker)
    end = text.index(end_marker, start)
    return text[start:end]


def test_business_images_scan_before_push() -> None:
    """业务矩阵必须先扫描本地镜像，再推送正式 GHCR 标签。"""
    text = WORKFLOW.read_text(encoding="utf-8")
    block = _block(text, "  build-and-push:\n", "  # hci-sim 暂未迁入")
    build = block.index("- name: 构建待扫描镜像")
    scan = block.index("- name: Trivy 镜像安全扫描", build)
    push = block.index("- name: 推送已扫描镜像", scan)
    assert build < scan < push
    assert "          load: true\n          push: false" in block
    assert "image-ref: local/${{ matrix.service }}:" in block
    assert "if: always() && steps.build.outcome == 'success'" in block


def test_db_migrate_scans_before_push() -> None:
    """数据库迁移镜像也不能绕过同一安全顺序。"""
    text = WORKFLOW.read_text(encoding="utf-8")
    block = _block(text, "  build-db-migrate:\n", "  auto-deploy-non-prod:\n")
    build = block.index("- name: 构建待扫描 db-migrate 镜像")
    scan = block.index("- name: Trivy 镜像安全扫描", build)
    push = block.index("- name: 推送已扫描 db-migrate 镜像", scan)
    assert build < scan < push
    assert "vuln-type: os" in block
    assert "Trivy db-migrate Atlas/library 扫描（信息性）" in block
    assert "if: always() && steps.build.outcome == 'success'" in block


def test_openclaw_scans_before_push() -> None:
    """独立手动发布工作流也必须满足相同的发布顺序。"""
    text = OPENCLAW_WORKFLOW.read_text(encoding="utf-8")
    build = text.index("- name: 构建待扫描镜像")
    scan = text.index("- name: Trivy 镜像安全扫描", build)
    push = text.index("- name: 推送已扫描镜像", scan)
    assert build < scan < push
    assert "image-ref: local/hci-openclaw:" in text
    assert "if: always() && steps.build.outcome == 'success'" in text


def test_trivy_actions_use_complete_immutable_sha() -> None:
    """被条件跳过的扫描路径也必须使用可解析的完整 action SHA。"""
    workflow_text = "\n".join(path.read_text(encoding="utf-8") for path in (WORKFLOW, OPENCLAW_WORKFLOW))
    refs = re.findall(r"aquasecurity/trivy-action@([0-9a-f]+)", workflow_text)
    assert len(refs) == 4
    assert set(refs) == {TRIVY_ACTION_SHA}
    assert all(len(ref) == 40 for ref in refs)
