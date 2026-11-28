"""5888 日志服务器单元测试夹具（完全隔离临时目录，不污染生产/系统目录）。"""

from __future__ import annotations

import importlib
import logging
import os
import sys
from pathlib import Path

import pytest

LOG_SERVER_DIR = Path(__file__).resolve().parents[2]


@pytest.fixture()
def log_server_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """在隔离目录中加载 app 与 process_upload，返回模块与测试客户端。"""
    upload_dir = tmp_path / "upload"
    temp_dir = tmp_path / "chunks"
    session_dir = tmp_path / "sessions"
    log_dir = tmp_path / "logs"
    for folder in (upload_dir, temp_dir, session_dir, log_dir):
        folder.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("ACLI_UPLOAD_FOLDER", str(upload_dir))
    monkeypatch.setenv("ACLI_TEMP_FOLDER", str(temp_dir))
    monkeypatch.setenv("ACLI_SESSION_STORE", str(session_dir))
    monkeypatch.setenv("ACLI_LOG_DIR", str(log_dir))
    monkeypatch.setenv("ACLI_RATE_STATE_FILE", str(tmp_path / "rate_state.json"))
    monkeypatch.setenv("ACLI_UPLOAD_TOKEN", "")
    monkeypatch.setenv("ACLI_P1_TICKET_SECRET", "")
    monkeypatch.setenv("ACLI_P1_REQUIRE_AUTH", "0")
    monkeypatch.setenv("ACLI_UPLOAD_STALL_SECONDS", "300")
    monkeypatch.setenv("ACLI_PROCESSING_STALL_SECONDS", "1800")

    dir_str = str(LOG_SERVER_DIR)
    if dir_str not in sys.path:
        sys.path.insert(0, dir_str)

    # 清除同名缓存模块，确保每次用例重新按隔离环境变量初始化
    for mod_name in ("app", "process_upload", "archive_index"):
        sys.modules.pop(mod_name, None)

    archive_index_mod = importlib.import_module("archive_index")
    process_upload_mod = importlib.import_module("process_upload")
    app_mod = importlib.import_module("app")

    app_mod.app.config["TESTING"] = True
    app_mod.app.logger.handlers[:] = [logging.NullHandler()]
    process_upload_mod.log.handlers[:] = [logging.NullHandler()]
    process_upload_mod.fail_log.handlers[:] = [logging.NullHandler()]

    yield {
        "app_mod": app_mod,
        "process_mod": process_upload_mod,
        "archive_index": archive_index_mod,
        "client": app_mod.app.test_client(),
        "upload_dir": upload_dir,
        "temp_dir": temp_dir,
        "session_dir": session_dir,
        "log_dir": log_dir,
    }

    for mod_name in ("app", "process_upload", "archive_index"):
        sys.modules.pop(mod_name, None)
    if dir_str in sys.path:
        sys.path.remove(dir_str)
    os.environ.pop("ACLI_UPLOAD_TOKEN", None)
    os.environ.pop("ACLI_P1_TICKET_SECRET", None)
