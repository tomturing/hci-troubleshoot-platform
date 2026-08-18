"""校验镜像发布流水线的安全顺序，防止回归为 push-before-scan。"""

from pathlib import Path


WORKFLOW = Path(__file__).parents[2] / ".github" / "workflows" / "ci.yml"
OPENCLAW_WORKFLOW = (
    Path(__file__).parents[2] / ".github" / "workflows" / "build-hci-openclaw.yml"
)


def _job_block(text: str, marker: str, next_marker: str) -> str:
    start = text.index(marker)
    end = text.index(next_marker, start)
    return text[start:end]


def test_business_image_is_scanned_before_push() -> None:
    """业务矩阵必须先扫描本地镜像，再推送正式 GHCR 标签。"""
    text = WORKFLOW.read_text(encoding="utf-8")
    block = _job_block(
        text,
        "  build-and-push:\n",
        "  # hci-sim 不属于 hci-platform-env",
    )

    build = block.index("- name: 构建待扫描镜像")
    scan = block.index("- name: Trivy 镜像安全扫描", build)
    push = block.index("- name: 推送已扫描镜像", scan)
    assert build < scan < push
    assert "          load: true\n          push: false" in block
    assert "local/${{ matrix.service }}:${{ needs.prepare.outputs.image_tag }}" in block
    assert "          image-ref: local/${{ matrix.service }}:" in block
    assert "          ignore-unfixed: true" in block
    assert "          scanners: vuln" in block
    assert 'if: always() && steps.build.outcome == \'success\'' in block
    assert 'exit-code: "0"' in block
    assert "docker push \"${target_image}:${{ needs.prepare.outputs.image_tag }}\"" in block


def test_db_migrate_is_scanned_before_push() -> None:
    """数据库迁移镜像也不能绕过同一安全顺序。"""
    text = WORKFLOW.read_text(encoding="utf-8")
    block = _job_block(text, "  build-db-migrate:\n", "  auto-deploy-non-prod:\n")

    build = block.index("- name: 构建待扫描 db-migrate 镜像")
    scan = block.index("- name: Trivy 镜像安全扫描", build)
    push = block.index("- name: 推送已扫描 db-migrate 镜像", scan)
    assert build < scan < push
    assert "          load: true\n          push: false" in block
    assert "local/db-migrate:${{ needs.prepare.outputs.image_tag }}" in block
    assert "          image-ref: local/db-migrate:" in block
    assert "          vuln-type: os" in block
    assert "          ignore-unfixed: true" in block
    assert "          scanners: vuln" in block
    assert "Trivy db-migrate Atlas/library 扫描（信息性）" in block
    assert 'if: always() && steps.build.outcome == \'success\'' in block


def test_hci_sim_digest_drift_creates_reviewable_pr() -> None:
    """hci-sim digest 漂移必须自动创建可追踪 PR，且不绕过人工审查。"""
    text = WORKFLOW.read_text(encoding="utf-8")
    block = _job_block(
        text,
        "  pin-hci-sim-gitops:\n",
        "  # ─────────────────────────────────────────────────────────────────────────\n",
    )
    assert "contents: write" in block
    assert "pull-requests: write" in block
    assert "ci/pin-hci-sim-digest-${HCI_SIM_DIGEST#sha256:}" in block
    assert "gh pr create" in block
    assert "[skip docs]" not in block
    assert '"agent:codex"' in block
    assert "hci-sim digest 自动钉入结果" in block


def test_docs_gate_only_exempts_digest_anchor_changes() -> None:
    """自动修复 PR 必须依赖可验证的最小 diff，不能自声明跳过文档门禁。"""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "deploy/gitops/argo-apps/*" in text
    assert 'grep -vE \'^[-+]?digest: "sha256:\'' in text
    assert "已文档化的 digest 锚点钉入" in text


def test_openclaw_is_scanned_before_manual_push() -> None:
    """独立手动发布工作流也必须满足相同的发布顺序。"""
    text = OPENCLAW_WORKFLOW.read_text(encoding="utf-8")
    build = text.index("- name: 构建待扫描镜像")
    scan = text.index("- name: Trivy 镜像安全扫描", build)
    push = text.index("- name: 推送已扫描镜像", scan)
    assert build < scan < push
    assert "                  load: true\n                  push: false" in text
    assert "image-ref: local/hci-openclaw:" in text
    assert 'if: always() && steps.build.outcome == \'success\'' in text
