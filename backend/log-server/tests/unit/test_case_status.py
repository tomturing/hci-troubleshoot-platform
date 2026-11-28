"""5888 日志服务器完整工单状态查询（/case_status、/cases/status/batch）与安全边界单元测试。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path


def _sign_p1_ticket(secret: str, case_id: str, exp: int | None = None) -> str:
    """按 5888 与平台一致的 p1.v1 协议签发测试票据。"""
    payload = {
        "v": 1,
        "cid": case_id,
        "fn": f"{case_id}.zip",
        "exp": exp if exp is not None else int(time.time()) + 600,
        "jti": "test-jti-001",
    }
    encoded = (
        base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        .decode("ascii")
        .rstrip("=")
    )
    signing_input = f"p1.v1.{encoded}"
    sig = hmac.new(secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{signing_input}.{sig}"


def _upload_and_archive(
    env: dict,
    case_id: str,
    payload: bytes,
    *,
    headers: dict[str, str] | None = None,
    extra_form: dict[str, str] | None = None,
) -> tuple[str, dict]:
    """执行一次完整的 init -> single chunk -> 同步 process_upload.process 归档流程。"""
    client = env["client"]
    app_mod = env["app_mod"]
    process_mod = env["process_mod"]

    dispatched: list[str] = []
    original_popen = app_mod.subprocess.Popen

    def fake_popen(cmd, **_kwargs):
        dispatched.append(cmd[-1])

    app_mod.subprocess.Popen = fake_popen
    try:
        form = {"filename": f"{case_id}.zip", "file_size": str(len(payload))}
        if extra_form:
            form.update(extra_form)
        init_resp = client.post("/init_upload", data=form, headers=headers or {})
        assert init_resp.status_code == 200, init_resp.get_data(as_text=True)
        upload_id = init_resp.get_json()["upload_id"]

        chunk_resp = client.post(
            "/upload_chunk",
            data={
                "upload_id": upload_id,
                "chunk_index": "0",
                "chunk_total": "1",
                "chunk": (io.BytesIO(payload), "chunk0.bin"),
            },
            headers=headers or {},
        )
        assert chunk_resp.status_code == 200
        assert chunk_resp.get_json()["status"] == "complete"
        assert dispatched == [upload_id]
    finally:
        app_mod.subprocess.Popen = original_popen

    assert process_mod.process(upload_id) is True
    return upload_id, init_resp.get_json()


def test_empty_case_status_and_trace_propagation(log_server_env: dict) -> None:
    """全新空白工单应返回空归档、空会话、全 0 汇总，并透传唯一调用链。"""
    client = log_server_env["client"]
    resp = client.get(
        "/case_status?case_id=Q2026112800001",
        headers={"X-Request-ID": "trace-empty-001"},
    )
    assert resp.status_code == 200
    assert resp.headers["X-Request-ID"] == "trace-empty-001"
    assert resp.headers["X-Trace-ID"] == "trace-empty-001"

    data = resp.get_json()
    assert data["case_id"] == "Q2026112800001"
    assert data["trace_id"] == "trace-empty-001"
    assert data["checked_at"].endswith("+00:00")
    assert data["archives"] == []
    assert data["sessions"] == []
    assert data["summary"] == {
        "has_archive": False,
        "archive_count": 0,
        "active_upload_count": 0,
        "processing_count": 0,
        "stalled_count": 0,
        "failed_session_count": 0,
    }


def test_multi_archive_metadata_preserved_across_rename(log_server_env: dict) -> None:
    """同一工单多任务归档：旧包改名加时间戳后，sha256/upload_source/task_id/archive_bind_key 均严格保持不变。"""
    case_id = "Q2026112800002"
    first_bytes = b"first-task-log-archive-payload"
    second_bytes = b"second-task-log-archive-payload-different"
    first_sha = hashlib.sha256(first_bytes).hexdigest()
    second_sha = hashlib.sha256(second_bytes).hexdigest()

    # 第一次：手工经 5888 Web 上传
    uid1, _ = _upload_and_archive(
        log_server_env,
        case_id,
        first_bytes,
        headers={"X-Request-ID": "trace-manual-1"},
        extra_form={"upload_source": "web_manual"},
    )
    # 稍微前调 mtime 确保排序确定
    canonical_file = log_server_env["upload_dir"] / case_id / f"{case_id}.zip"
    older_ts = time.time() - 120
    os.utime(canonical_file, (older_ts, older_ts))
    # 同步刷新 .digests.json 中第一份的 mtime_ns 以模拟真实前序落盘
    cache = log_server_env["archive_index"].load_cache(str(canonical_file.parent))
    cache[f"{case_id}.zip"]["mtime_ns"] = canonical_file.stat().st_mtime_ns
    log_server_env["archive_index"].save_cache(str(canonical_file.parent), cache)

    before_resp = log_server_env["client"].get(f"/case_status?case_id={case_id}")
    assert before_resp.status_code == 200
    first_bind_key = before_resp.get_json()["archives"][0]["archive_bind_key"]
    assert first_bind_key == f"sha256:{first_sha}"

    # 第二次：平台 P2 中转上传不同内容
    uid2, _ = _upload_and_archive(
        log_server_env,
        case_id,
        second_bytes,
        headers={"X-Request-ID": "trace-p2-2"},
        extra_form={"upload_source": "p2_relay", "task_id": "task-uuid-002", "force": "1"},
    )

    resp = log_server_env["client"].get(f"/case_status?case_id={case_id}")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["summary"]["has_archive"] is True
    assert body["summary"]["archive_count"] == 2
    assert len(body["archives"]) == 2

    newest, older = body["archives"][0], body["archives"][1]
    assert newest["stored_name"] == f"{case_id}.zip"
    assert newest["sha256"] == second_sha
    assert newest["upload_source"] == "p2_relay"
    assert newest["upload_id"] == uid2
    assert newest["task_id"] == "task-uuid-002"
    assert newest["archive_bind_key"] == f"sha256:{second_sha}"

    # 旧包改名后 metadata 与 archive_bind_key 均保持不变
    assert older["stored_name"].startswith(f"{case_id}-") and older["stored_name"].endswith(".zip")
    assert older["sha256"] == first_sha
    assert older["upload_source"] == "web_manual"
    assert older["upload_id"] == uid1
    assert older["archive_bind_key"] == first_bind_key

    # /archive_status 应 O(1) 返回最新归档的 sha256 与 upload_source
    arch_resp = log_server_env["client"].get(f"/archive_status?filename={case_id}.zip")
    assert arch_resp.status_code == 200
    arch_data = arch_resp.get_json()
    assert arch_data["sha256"] == second_sha
    assert arch_data["upload_source"] == "p2_relay"
    assert arch_data["task_id"] == "task-uuid-002"
    assert arch_data["archive_bind_key"] == f"sha256:{second_sha}"


def test_upload_progress_archive_fallback_returns_cached_sha256(log_server_env: dict) -> None:
    """会话被清理后，/upload_progress 兜底归档应 O(1) 带出 digest.sha256。"""
    case_id = "Q2026112800003"
    payload = b"archive-fallback-digest-test"
    expected_sha = hashlib.sha256(payload).hexdigest()
    uid, _ = _upload_and_archive(log_server_env, case_id, payload)

    # 模拟 24h 后 session_reaper 删掉会话 JSON
    (log_server_env["session_dir"] / f"{uid}.json").unlink()

    resp = log_server_env["client"].get(f"/upload_progress?filename={case_id}.zip")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["found"] is True
    assert data["status"] == "done"
    assert data["progress"] == 100
    assert data["digest"] == {"sha256": expected_sha, "size": len(payload)}


def test_stalled_processing_failed_sessions_and_readonly_invariance(log_server_env: dict) -> None:
    """覆盖 uploading 停滞（P1 静默丢包）、pending 合片中、failed 失败以及严格只读不删过期会话。"""
    case_id = "Q2026112800004"
    filename = f"{case_id}.zip"
    session_dir: Path = log_server_env["session_dir"]
    now = datetime.now()

    def write_session(uid: str, doc: dict) -> None:
        (session_dir / f"{uid}.json").write_text(json.dumps(doc), encoding="utf-8")

    # 1. 活跃且正常上传中（idle 10s < 300s）
    write_session(
        "sess-active",
        {
            "upload_id": "sess-active",
            "filename": filename,
            "file_size": 1000,
            "uploaded_size": 400,
            "chunks": {"0": "/tmp/c0"},
            "chunk_total": 2,
            "status": "uploading",
            "upload_source": "acli_direct",
            "start_time": time.time() - 60,
            "last_updated": (now - timedelta(seconds=10)).isoformat(),
        },
    )
    # 2. P1 静默丢包停滞会话（idle 600s >= 300s）
    write_session(
        "sess-stalled",
        {
            "upload_id": "sess-stalled",
            "filename": filename,
            "file_size": 1000,
            "uploaded_size": 200,
            "chunks": {"0": "/tmp/c0"},
            "chunk_total": 2,
            "status": "uploading",
            "upload_source": "p1_ticket",
            "start_time": time.time() - 700,
            "last_updated": (now - timedelta(seconds=600)).isoformat(),
        },
    )
    # 3. 合片处理中（pending，idle 30s < 1800s）
    write_session(
        "sess-pending",
        {
            "upload_id": "sess-pending",
            "filename": filename,
            "file_size": 1000,
            "uploaded_size": 1000,
            "chunks": {"0": "/tmp/c0", "1": "/tmp/c1"},
            "chunk_total": 2,
            "status": "pending",
            "upload_source": "p2_relay",
            "start_time": time.time() - 120,
            "last_updated": (now - timedelta(seconds=30)).isoformat(),
        },
    )
    # 4. 失败会话
    write_session(
        "sess-failed",
        {
            "upload_id": "sess-failed",
            "filename": filename,
            "file_size": 1000,
            "uploaded_size": 1000,
            "chunks": {"0": "/tmp/c0"},
            "chunk_total": 1,
            "status": "failed",
            "error": "合片大小不符",
            "upload_source": "p2_relay",
            "start_time": time.time() - 300,
            "last_updated": (now - timedelta(seconds=250)).isoformat(),
        },
    )
    # 5. 已超过 24h 的过期会话：只读接口必须跳过它但绝不能删除文件
    write_session(
        "sess-expired",
        {
            "upload_id": "sess-expired",
            "filename": filename,
            "file_size": 1000,
            "uploaded_size": 100,
            "chunks": {"0": "/tmp/c0"},
            "chunk_total": 2,
            "status": "uploading",
            "start_time": time.time() - 90000,
            "last_updated": (now - timedelta(hours=30)).isoformat(),
        },
    )

    before_files = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(session_dir.iterdir())
    }

    resp = log_server_env["client"].get(f"/case_status?case_id={case_id}")
    assert resp.status_code == 200
    data = resp.get_json()

    assert data["summary"] == {
        "has_archive": False,
        "archive_count": 0,
        "active_upload_count": 2,
        "processing_count": 1,
        "stalled_count": 1,
        "failed_session_count": 1,
    }
    by_id = {s["upload_id"]: s for s in data["sessions"]}
    assert "sess-expired" not in by_id
    assert by_id["sess-active"]["stalled"] is False
    assert by_id["sess-active"]["progress"] == 40
    assert by_id["sess-stalled"]["stalled"] is True
    assert by_id["sess-stalled"]["idle_seconds"] >= 595
    assert by_id["sess-pending"]["status"] == "pending"
    assert by_id["sess-pending"]["stalled"] is False
    assert by_id["sess-failed"]["error"] == "合片大小不符"

    # 严格只读验证：目录内容与每个文件字节完全未变
    after_files = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(session_dir.iterdir())
    }
    assert after_files == before_files


def test_batch_case_status_and_limits(log_server_env: dict) -> None:
    """批量查询去重保序、返回各自状态，并拒绝超限与非法工单号。"""
    client = log_server_env["client"]
    c1, c2 = "Q2026112800011", "Q2026112800012"
    _upload_and_archive(log_server_env, c1, b"batch-case-1-data", extra_form={"upload_source": "p2_relay"})

    resp = client.post("/cases/status/batch", json={"case_ids": [c1, c2, c1]})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["count"] == 2
    assert set(body["items"].keys()) == {c1, c2}
    assert body["items"][c1]["summary"]["has_archive"] is True
    assert body["items"][c2]["summary"]["has_archive"] is False

    # 超出 50 个工单返回 400
    too_many = [f"Q20261128{i:05d}" for i in range(51)]
    over_resp = client.post("/cases/status/batch", json={"case_ids": too_many})
    assert over_resp.status_code == 400

    # 空列表或非法 JSON 返回 400
    assert client.post("/cases/status/batch", json={"case_ids": []}).status_code == 400
    assert client.post("/cases/status/batch", data="not-json").status_code == 400
    assert client.post("/cases/status/batch", json={"case_ids": [c1, "../etc"]}).status_code == 400


def test_security_auth_and_path_traversal_guards(log_server_env: dict) -> None:
    """安全验证：鉴权 fail-closed、P1 票据防跨工单越权、禁止票据调批量接口、阻断目录穿越。"""
    app_mod = log_server_env["app_mod"]
    client = log_server_env["client"]

    app_mod.AUTH_TOKEN = "global-secret-token"
    app_mod.P1_TICKET_SECRET = "p1-hmac-secret-key"
    app_mod.P1_REQUIRE_AUTH = False  # 即使处于匿名直传放宽期，查询端点也必须强制鉴权

    c1, c2 = "Q2026112800021", "Q2026112800022"
    p1_ticket_c1 = _sign_p1_ticket(app_mod.P1_TICKET_SECRET, c1)

    # 1. 无凭据访问 /case_status 与 /cases/status/batch 一律 401
    assert client.get(f"/case_status?case_id={c1}").status_code == 401
    assert client.post("/cases/status/batch", json={"case_ids": [c1]}).status_code == 401

    # 2. P1 票据可查自身工单 /case_status
    ok_resp = client.get(
        f"/case_status?case_id={c1}",
        headers={"Authorization": f"Bearer {p1_ticket_c1}"},
    )
    assert ok_resp.status_code == 200

    # 3. P1 票据跨工单查询被拒绝（401 filename_mismatch）
    cross_resp = client.get(
        f"/case_status?case_id={c2}",
        headers={"Authorization": f"Bearer {p1_ticket_c1}"},
    )
    assert cross_resp.status_code == 401
    assert cross_resp.get_json()["reason"] == "filename_mismatch"

    # 4. P1 票据访问批量接口被拒绝（403）
    batch_p1_resp = client.post(
        "/cases/status/batch",
        json={"case_ids": [c1]},
        headers={"Authorization": f"Bearer {p1_ticket_c1}"},
    )
    assert batch_p1_resp.status_code == 403
    assert batch_p1_resp.get_json()["reason"] == "batch_requires_global_token"

    # 5. 全局 Bearer 访问 /case_status 与 /cases/status/batch 正常放行
    global_headers = {"Authorization": "Bearer global-secret-token"}
    assert client.get(f"/case_status?case_id={c2}", headers=global_headers).status_code == 200
    assert (
        client.post("/cases/status/batch", json={"case_ids": [c1, c2]}, headers=global_headers).status_code
        == 200
    )

    # 6. 路径穿越与非法工单号全部 400（含 URL 编码换行注入 %0A）
    for bad in ("../../etc", "Q1899010100001", "Q2026112800021\n", "Q2026112800021/x", "bad"):
        bad_resp = client.get("/case_status", query_string={"case_id": bad}, headers=global_headers)
        assert bad_resp.status_code == 400, f"应拒绝非法 case_id={bad!r}"

    # 7. P1 票据初始化上传时强制标注 upload_source=p1_ticket（不可被表单伪造为 web_manual）
    init_resp = client.post(
        "/init_upload",
        data={"filename": f"{c1}.zip", "file_size": "16", "upload_source": "web_manual"},
        headers={"Authorization": f"Bearer {p1_ticket_c1}"},
    )
    assert init_resp.status_code == 200
    uid = init_resp.get_json()["upload_id"]
    sess_doc = json.loads((log_server_env["session_dir"] / f"{uid}.json").read_text(encoding="utf-8"))
    assert sess_doc["upload_source"] == "p1_ticket"
    assert sess_doc["status"] == "uploading"
