"""The agent's log files, the bundle the RCIC can send, and send_logs."""
import base64
import io
import logging
import os
import time
import zipfile

import pytest

from uapply_agent import diagnostics, logs
from uapply_agent import mcp_server as m
from uapply_agent.api import ApiError
from uapply_agent.config import Settings


@pytest.fixture
def log_file(monkeypatch):
    """setup_logging into the test's config dir; handlers removed afterwards."""
    root = logging.getLogger()
    before = list(root.handlers)
    monkeypatch.setattr(logs, "_configured", [])
    path = logs.setup_logging("test", logging.CRITICAL)
    yield path
    for h in root.handlers[len(before):]:
        h.close()
        root.removeHandler(h)


def flush():
    for h in logging.getLogger().handlers:
        h.flush()


def test_secrets_and_long_messages_never_reach_the_file(log_file):
    log = logging.getLogger("uapply_agent.test")
    log.warning("GET /x with Authorization: Bearer abc.def-123 failed")
    log.warning("refresh_token=r3fr3sh&x=1, {'access_token': 'tok'}")
    log.warning("downloaded https://bucket.s3.amazonaws.com/a/b.pdf?X-Amz-Signature=deadbeef&X-Amz-Date=1")
    log.warning("token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.sig")
    log.info("%s", "x" * 5000)
    flush()
    text = log_file.read_text(encoding="utf-8")
    for secret in ("abc.def-123", "r3fr3sh", "'tok'", "deadbeef", "eyJhbGciOiJIUzI1NiJ9"):
        assert secret not in text
    assert "Bearer <redacted>" in text and "b.pdf?<redacted>" in text
    assert "chars cut]" in text and "x" * 2001 not in text


def test_library_info_is_left_out_but_agent_info_and_tracebacks_kept(log_file):
    logging.getLogger("httpx").info("HTTP Request: GET /noise")
    logging.getLogger("httpx").warning("HTTP warning kept")
    try:
        raise ValueError("boom with Bearer leaked.token")
    except ValueError:
        logging.getLogger("uapply_agent.x").exception("tool failed")
    flush()
    text = log_file.read_text(encoding="utf-8")
    assert "noise" not in text and "HTTP warning kept" in text
    assert "Traceback" in text and "ValueError: boom" in text and "leaked.token" not in text


def test_old_and_excess_files_are_pruned(tmp_path):
    d = tmp_path / "logs"
    d.mkdir()
    old = d / "old.log"
    old.write_text("x")
    os.utime(old, (time.time() - 20 * 86400,) * 2)
    big = [d / f"{i}.log" for i in range(3)]
    for i, p in enumerate(big):
        p.write_bytes(b"x" * 1000)
        os.utime(p, (time.time() - i,) * 2)
    logs.prune(d, keep_days=14, keep_bytes=2500)
    assert sorted(p.name for p in d.glob("*.log")) == ["0.log", "1.log"]


def test_bundle_holds_logs_versions_case_and_note(log_file):
    logging.getLogger("uapply_agent.executor").error("task t1 failed on codex: timed out")
    flush()
    data = diagnostics.build_bundle(Settings(), 24, "analysis failed", case={"survey_id": "s-1"},
                                    status={"stages": {"analysis": "failed"}})
    zf = zipfile.ZipFile(io.BytesIO(data))
    names = zf.namelist()
    assert {"about.json", "note.txt", "case.json", "status.json", f"logs/{log_file.name}"} <= set(names)
    assert "task t1 failed" in zf.read(f"logs/{log_file.name}").decode()
    assert zf.read("note.txt").decode() == "analysis failed"
    assert '"agent_version"' in zf.read("about.json").decode()


def test_bundle_drops_the_oldest_logs_to_fit(tmp_path, monkeypatch):
    files = []
    for i in range(3):
        p = tmp_path / f"{i}.log"
        p.write_text(base64.b64encode(os.urandom(4000)).decode())   # barely compressible
        files.append(p)
    monkeypatch.setattr(diagnostics, "recent_files", lambda hours: files)
    data = diagnostics.build_bundle(Settings(), 24, "", max_bytes=12000)
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert "logs/0.log" in names and "logs/2.log" not in names


class LogsApi:
    def __init__(self, fail=None):
        self.sent, self.fail = [], fail

    def agent_status(self, survey_id):
        return {"stages": {"processing": "done"}}

    def send_diagnostics(self, bundle, survey_id, note):
        if self.fail:
            raise self.fail
        self.sent.append((bundle, survey_id, note))
        return {"success": True, "id": "diag-1"}


def test_send_logs_uploads_the_bundle_for_the_case(use_server, case_folder):
    api = LogsApi()
    use_server(case_folder, api)
    out = m.send_logs(note="run_tasks failed twice")
    assert out == {"ok": True, "sent": True, "reference": "diag-1", "size": len(api.sent[0][0])}
    bundle, survey_id, note = api.sent[0]
    assert (survey_id, note) == ("s-1", "run_tasks failed twice")
    assert "status.json" in zipfile.ZipFile(io.BytesIO(bundle)).namelist()


def test_send_logs_works_without_a_case(use_server, tmp_path):
    from uapply_agent.folder import WorkingFolder
    (tmp_path / "loose").mkdir()
    api = LogsApi()
    use_server(WorkingFolder(tmp_path / "loose"), api)
    assert m.send_logs(note="login fails")["sent"]
    assert api.sent[0][1] is None


def test_an_old_backend_points_at_the_logs_folder(use_server, case_folder):
    use_server(case_folder, LogsApi(fail=ApiError(404, "<html>")))
    out = m.send_logs(note="x")
    assert out["ok"] is False and out["error"]["code"] == "LOGS_NOT_ACCEPTED"
    assert diagnostics.where() in out["error"]["hint"]


def test_a_failing_tool_is_logged_with_its_reason(log_file, use_server, case_folder):
    class Broken(LogsApi):
        def agent_api_available(self):
            return True

        def task_stats(self, survey_id):
            raise RuntimeError("database is locked")
    use_server(case_folder, Broken())
    out = m.task_stats()
    assert out["ok"] is False
    flush()
    text = log_file.read_text(encoding="utf-8")
    assert "[case=s-1]" in text
    assert "tool task_stats(-) failed" in text and "RUNTIMEERROR database is locked" in text
    assert "Traceback" in text
