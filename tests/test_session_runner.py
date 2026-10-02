"""Tasks as subagents of the RCIC's session: briefs, local finishes, submit/release tools."""
from pathlib import Path

from test_extract_content import make_pdf
from uapply_agent import briefs, playbook
from uapply_agent.config import Settings
from uapply_agent.context import runner_mode
from uapply_agent.session_runner import SessionRunner

SCHEMA = {"type": "object", "properties": {"file_types": {"type": "array"}}, "required": ["file_types"]}
PAGES_SCHEMA = {"type": "object", "properties": {"pages": {"type": "array"}}, "required": ["pages"]}


def task(tid="t1", kind="classify_document", file_name="page.jpg", schema=SCHEMA, **payload):
    return {"id": tid, "kind": kind, "payload": {
        "system_prompt": "classify carefully", "user_prompt": "对上传的图像进行分类", "output_schema": schema,
        "inputs": [{"document_id": "d1", "file_name": file_name, "mime": "application/octet-stream"}], **payload}}


class FakeApi:
    def __init__(self, src: Path, tasks, verdict=None):
        self.src, self.queue, self.verdict = src, list(tasks), verdict or {"accepted": True, "task_status": "accepted"}
        self.pulled, self.submitted, self.released = [], [], []

    def pull_tasks(self, survey_ids, n, session_id, runtime, kinds=None, lease_s=None):
        self.pulled.append({"n": n, "runtime": runtime, "lease_s": lease_s})
        out, self.queue = self.queue[:n], self.queue[n:]
        return out

    def document_download_url(self, document_id):
        return "https://s3/x"

    def download_to(self, url, dest: Path):
        dest.write_bytes(self.src.read_bytes())
        return dest

    def submit_result(self, task_id, result, model, runtime, usage, session_id):
        self.submitted.append({"task_id": task_id, "result": result, "model": model, "runtime": runtime})
        return self.verdict

    def release_task(self, task_id, reason):
        self.released.append((task_id, reason))

    def task_stats(self, survey_id):
        return {"queued": len(self.queue), "leased": 0}


def test_mode_follows_the_setting_then_the_client():
    assert runner_mode("auto", "claude-code") == "session"   # what Claude Code actually sends
    assert runner_mode("auto", "Claude Code") == "session"
    assert runner_mode("auto", "Claude Code Desktop") == "session"
    assert runner_mode("auto", "codex") == "cli"
    assert runner_mode("auto", None) == "cli"
    assert runner_mode("cli", "Claude Code") == "session"    # Claude Code never runs a headless CLI
    assert runner_mode("cli", "codex") == "cli"
    assert runner_mode("session", None) == "session"


def test_classification_task_becomes_a_brief_with_the_first_pages(case_folder, tmp_path):
    src = make_pdf(tmp_path / "passport.pdf", pages=5, text=False)
    api = FakeApi(src, [task(file_name="passport.pdf")])
    rnd = SessionRunner(api, case_folder).prepare_round(4)
    assert api.pulled == [{"n": 4, "runtime": "claude-code-session", "lease_s": 1800}]
    assert rnd.submitted == 0 and len(rnd.tasks) == 1
    t = rnd.tasks[0]
    assert t["task_id"] == "t1" and t["pages"] == 3 and t["document"] == "passport.pdf"
    brief = Path(t["brief"]).read_text(encoding="utf-8")
    assert "## Instructions\n\nclassify carefully" in brief and "对上传的图像进行分类" in brief
    assert '"file_types"' in brief and "`submit_task`" in brief and "`release_task`" in brief
    listed = [line for line in brief.splitlines() if line.startswith("- `")]
    assert [x.split("`")[1] for x in listed] == ["passport_p001.png", "passport_p002.png", "passport_p003.png"]
    info = briefs.read_task_file(case_folder, "t1")
    assert info["kind"] == "classify_document" and info["schema"] == SCHEMA


def test_text_layer_pdf_is_finished_without_a_subagent(case_folder, tmp_path):
    src = make_pdf(tmp_path / "statement.pdf", pages=2, text=True)
    api = FakeApi(src, [task(kind="extract_content", file_name="statement.pdf", schema=PAGES_SCHEMA)])
    rnd = SessionRunner(api, case_folder).prepare_round(2)
    assert rnd.tasks == [] and rnd.submitted == 1
    sub = api.submitted[0]
    assert sub["model"] == "pdfplumber" and sub["runtime"] == "local" and len(sub["result"]["pages"]) == 2


def test_scanned_pdf_brief_lists_every_page_in_order(case_folder, tmp_path):
    src = make_pdf(tmp_path / "scan.pdf", pages=4, text=False)
    api = FakeApi(src, [task(kind="extract_content", file_name="scan.pdf", schema=PAGES_SCHEMA)])
    rnd = SessionRunner(api, case_folder).prepare_round(1)
    t = rnd.tasks[0]
    brief = Path(t["brief"]).read_text(encoding="utf-8")
    assert t["pages"] == 4 and "numbered n=1 upwards" in brief
    assert brief.index("scan_p001.png") < brief.index("scan_p004.png")


def test_a_task_that_cannot_be_prepared_is_released(case_folder, tmp_path):
    src = tmp_path / "doc.xyz"
    src.write_bytes(b"?")
    api = FakeApi(src, [task(kind="extract_content", file_name="doc.xyz", schema=PAGES_SCHEMA)])
    rnd = SessionRunner(api, case_folder).prepare_round(1)
    assert rnd.tasks == [] and rnd.failed[0]["task_id"] == "t1"
    assert api.released[0][0] == "t1" and "unsupported" in api.released[0][1]


def test_rejection_feedback_is_included_in_the_brief(case_folder, tmp_path):
    src = tmp_path / "page.jpg"
    src.write_bytes(b"\xff\xd8 fake")
    t = task()
    t["rejection"] = {"code": "SCHEMA_INVALID", "message": "file_types must be a list"}
    rnd = SessionRunner(FakeApi(src, [t]), case_folder).prepare_round(1)
    assert "A previous answer was rejected: file_types must be a list" in Path(rnd.tasks[0]["brief"]).read_text()


def _server(use_server, case_folder, api, task_runner="session"):
    from uapply_agent import mcp_server as m
    use_server(case_folder, api, Settings(task_runner=task_runner))
    return m


def test_run_tasks_returns_briefs_in_session_mode(use_server, case_folder, tmp_path):
    src = tmp_path / "page.jpg"
    src.write_bytes(b"\xff\xd8 fake")
    api = FakeApi(src, [task("t1"), task("t2")])
    api.agent_api_available = lambda: True
    api.survey = lambda sid: {"id": sid, "documents": [], "document_types": [], "analyzing_status": "none"}
    m = _server(use_server, case_folder, api)
    out = m.run_tasks(workers=2)
    assert out["ok"] and out["mode"] == "session" and [t["task_id"] for t in out["tasks"]] == ["t1", "t2"]
    assert out["remaining"] == 0 and "progress" in out


def test_submit_task_checks_the_schema_then_relays_the_verdict(use_server, case_folder, tmp_path):
    src = tmp_path / "page.jpg"
    src.write_bytes(b"\xff\xd8 fake")
    api = FakeApi(src, [task("t1")], verdict={"accepted": False, "task_status": "queued",
                                               "rejection": {"code": "TYPE_NOT_ALLOWED", "message": "not allowed"}})
    api.agent_api_available = lambda: True
    m = _server(use_server, case_folder, api)
    SessionRunner(api, case_folder).prepare_round(1)
    bad = m.submit_task("t1", {"nope": 1})
    assert bad["accepted"] is False and "missing required field" in bad["feedback"] and api.submitted == []
    rejected = m.submit_task("t1", {"file_types": ["Card"]})
    assert rejected["accepted"] is False and "TYPE_NOT_ALLOWED" in rejected["feedback"] and not rejected["final"]
    assert api.submitted[0]["runtime"] == "claude-code-session"
    api.verdict = {"accepted": True, "task_status": "accepted"}
    assert m.submit_task("t1", {"file_types": ["Passport"]})["accepted"] is True
    assert m.submit_task("zzz", {})["error"]["code"] == "UNKNOWN_TASK"
    assert m.release_task("t1", "unreadable")["released"] and api.released == [("t1", "unreadable")]


def test_plugin_ships_the_task_runner_agent_without_a_model():
    files = playbook.plugin_files("1.0.0")
    agent = files["agents/task-runner.md"]
    head = agent.split("---")[1]
    assert "name: task-runner" in head and "tools: Read, mcp__uapply__submit_task, mcp__uapply__release_task" in head
    assert "permissionMode: dontAsk" in head and "omitClaudeMd: true" in head and "background: true" in head
    assert "model:" not in head                  # the session's own model
    assert "uapply:task-runner" in playbook.instructions() and "Task brief:" in playbook.instructions()


def test_whoami_does_not_ask_for_the_cli_in_session_mode(use_server, case_folder, monkeypatch):
    class Api:
        logged_in = True

        def agent_api_available(self):
            return True
    m = _server(use_server, case_folder, Api())
    monkeypatch.setattr(m, "detect_runtimes", lambda settings=None: [])
    out = m.whoami()
    assert out["task_runner"] == "session" and "hint" not in out
    m = _server(use_server, case_folder, Api(), task_runner="cli")
    assert "hint" in m.whoami()


def test_intake_brief_and_hints_round_trip(use_server, case_folder, tmp_path):
    from uapply_agent.chat.base import Transcript
    from uapply_agent.chat.store import ChatStore
    md = case_folder.chat / "anychat_zw_2026-01-01_2026-03-31.md"
    md.parent.mkdir(parents=True)
    md.write_text("2026-03-02 张伟: 我想申请学签", encoding="utf-8")

    class Api:
        def application_types(self):
            return [{"id": "at-study", "name": "Study Permit"}]
    m = _server(use_server, case_folder, Api())
    t = Transcript("anychat", "张伟", "2026-01-01", "2026-03-31", md)
    ChatStore(case_folder).record(t)
    from uapply_agent.chat.intake import intake_prompts
    brief = m._intake_brief(t, intake_prompts)
    text = brief.read_text(encoding="utf-8")
    assert "`submit_intake`" in text and str(md) in text and "suggested_application_type_id" in text
    bad = m.submit_intake(str(md), {"application": {"confidence": 5}})
    assert bad["accepted"] is False
    good = m.submit_intake(str(md), {"applicant": {"family_name": "张"},
                                     "application": {"suggested_application_type_id": "nope", "confidence": 0.5}})
    assert good["accepted"] is True
    hints = m.intake_hints(str(md))["intake"]
    assert hints["applicant"]["family_name"] == "张" and hints["application"]["suggested_application_type_id"] is None
    assert m.intake_hints(str(tmp_path / "other.md"))["error"]["code"] == "NO_HINTS"
