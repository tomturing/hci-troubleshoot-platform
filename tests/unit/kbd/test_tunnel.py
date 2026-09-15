"""
tests/unit/kbd/test_tunnel.py — kbd/tunnel.py 端口转发与依赖探测单元测试
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

_scripts_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "scripts"))
if _scripts_root not in sys.path:
    sys.path.insert(0, _scripts_root)


class TestPostgresTunnel:
    """测试 PostgreSQL 端口转发与连通性守护。"""

    def test_postgres_already_reachable_does_not_start_tunnel(self):
        from kbd import tunnel

        with (
            patch.object(tunnel, "_check_postgres_reachable", return_value=True),
            patch.object(tunnel, "_start_tunnel") as mock_start,
        ):
            assert tunnel.ensure_postgres_reachable() is True
            mock_start.assert_not_called()

    def test_postgres_remote_host_skips_tunnel(self):
        from kbd import tunnel

        with patch.object(tunnel.settings, "DATABASE_URL", "postgresql://user:pass@10.43.0.10:5432/db"):
            assert tunnel._local_postgres_port() is None
            assert tunnel.ensure_postgres_reachable() is True

    def test_postgres_detects_postgres_external_service_first(self):
        from kbd import tunnel

        def fake_has_service(namespace: str, service_name: str) -> bool:
            return service_name == "postgres-external"

        with patch.object(tunnel, "_namespace_has_service", side_effect=fake_has_service):
            target = tunnel._resolve_postgres_service_target("hci-staging")
            assert target == "svc/postgres-external"

    def test_postgres_falls_back_to_clusterip_service(self):
        from kbd import tunnel

        def fake_has_service(namespace: str, service_name: str) -> bool:
            return service_name == "postgres"

        with patch.object(tunnel, "_namespace_has_service", side_effect=fake_has_service):
            target = tunnel._resolve_postgres_service_target("hci-staging")
            assert target == "svc/postgres"

    def test_postgres_raises_when_no_service_found(self):
        from kbd import tunnel

        with patch.object(tunnel, "_namespace_has_service", return_value=False):
            with pytest.raises(tunnel.PortForwardError, match="未找到 svc/postgres-external 或 svc/postgres"):
                tunnel._resolve_postgres_service_target("hci-staging")

    def test_postgres_starts_tunnel_when_unreachable(self, tmp_path):
        from kbd import tunnel

        fake_proc = MagicMock()
        fake_proc.pid = 1234
        fake_proc.poll.return_value = None

        pid_file = tmp_path / ".test-pg.pid"
        lock_file = tmp_path / ".test-pg.lock"
        log_file = tmp_path / ".test-pg.log"

        reachable_states = [False, True]

        def fake_check(*args, **kwargs):
            return reachable_states.pop(0) if reachable_states else True

        with (
            patch.object(tunnel, "_resolve_k8s_namespace", return_value="hci-staging"),
            patch.object(tunnel, "_resolve_postgres_service_target", return_value="svc/postgres-external"),
            patch.object(tunnel, "_start_tunnel", return_value=fake_proc),
            patch.object(tunnel, "_PG_PID_FILE", pid_file),
            patch.object(tunnel, "_PG_LOCK_FILE", lock_file),
            patch.object(tunnel, "_PG_LOG_FILE", log_file),
            patch.object(tunnel, "_check_postgres_reachable", side_effect=fake_check),
        ):
            ok = tunnel.ensure_postgres_reachable()
            assert ok is True

    def test_ensure_pipeline_dependencies_raises_when_postgres_unreachable(self):
        from kbd import tunnel

        with patch.object(tunnel, "ensure_postgres_reachable", return_value=False):
            with pytest.raises(tunnel.PortForwardError, match="PostgreSQL 数据库不可达"):
                tunnel.ensure_pipeline_dependencies()


class TestTunnelSafety:
    """测试进程身份校验与元数据管理。"""

    def test_reused_pid_is_not_treated_as_owned(self):
        from kbd import tunnel

        cmd = ["kubectl", "port-forward", "svc/postgres-external", "-n", "hci-staging", "5432:5432"]
        metadata = {
            "pid": 999,
            "namespace": "hci-staging",
            "local_port": 5432,
            "command": cmd,
            "process_identity": {"start_ticks": "200", "executable": "/usr/bin/kubectl"},
        }

        with patch.object(
            tunnel,
            "_process_identity",
            return_value={"start_ticks": "201", "executable": "/usr/bin/python"},
        ):
            assert tunnel._process_matches_metadata(metadata, cmd) is False

    def test_matching_pid_and_identity_is_treated_as_owned(self):
        from kbd import tunnel

        cmd = ["kubectl", "port-forward", "svc/postgres-external", "-n", "hci-staging", "5432:5432"]
        identity = {"start_ticks": "200", "executable": "/usr/bin/kubectl"}
        metadata = {
            "pid": 999,
            "namespace": "hci-staging",
            "local_port": 5432,
            "command": cmd,
            "process_identity": identity,
        }

        with patch.object(tunnel, "_process_identity", return_value=identity):
            assert tunnel._process_matches_metadata(metadata, cmd) is True
