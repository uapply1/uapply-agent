"""Chat intake: AnyChat wrapper against a fake CLI, PDF rendering, local intake, tools."""
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from uapply_agent.chat.anychat import AnyChatSource
from uapply_agent.chat.base import ChatError, Transcript
from uapply_agent.chat.render import transcript_to_pdf
from uapply_agent.chat.store import ChatStore, redact
from uapply_agent.folder import WorkingFolder
from uapply_agent.local_ops import pdf_pages_text
from uapply_agent.runners.base import Runner, RunResult

TRANSCRIPT = """# 张伟 (friend)

2026-03-02 10:15 张伟: 老师您好，我想申请加拿大的学签，明年九月入学多伦多大学
2026-03-02 10:16 Jacky: 好的，请把护照发我
2026-03-02 10:20 张伟: 我叫张伟 Zhang Wei，1998年5月4日出生，中国籍
2026-03-03 09:00 张伟: 我太太李娜也想一起去，还有一个三岁的孩子
"""

FAKE_CLI = r'''#!/usr/bin/env python3
import json, sys, os
args = sys.argv[1:]
mode = os.environ.get("FAKE_ANYCHAT_MODE", "ok")
if args[:1] == ["whoami"]:
    if mode == "not_logged_in":
        sys.stderr.write("E_NOT_LOGGED_IN: please login\n"); sys.exit(2)
    print(json.dumps({"email": "rcic@example.com"})); sys.exit(0)
if args[:1] == ["resolve"]:
    print(json.dumps({"candidates": [{"display_name": "张伟", "wxid": "wxid_abc123", "type": "friend"},
                                     {"display_name": "张伟 (同事)", "wxid": "wxid_def456"}]})); sys.exit(0)
if args[:1] == ["query"]:
    out = args[args.index("-o") + 1]
    open(out, "w", encoding="utf-8").write(os.environ["FAKE_ANYCHAT_TRANSCRIPT"])
    print(json.dumps({"ok": True, "args": args})); sys.exit(0)
sys.stderr.write("unknown command\n"); sys.exit(1)
'''


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    exe = tmp_path / "anychat"
    exe.write_text(FAKE_CLI)
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("ANYCHAT_BIN", str(exe))
    monkeypatch.setenv("FAKE_ANYCHAT_TRANSCRIPT", TRANSCRIPT)
    monkeypatch.setenv("FAKE_ANYCHAT_MODE", "ok")
    return exe


def test_not_installed_and_unsupported_platform(tmp_path, monkeypatch):
    monkeypatch.delenv("ANYCHAT_BIN", raising=False)
    monkeypatch.setattr("uapply_agent.chat.anychat.shutil.which", lambda name: None)
    monkeypatch.setattr("uapply_agent.chat.anychat.Path.home", lambda: tmp_path)
    a = AnyChatSource().available()
    assert not a.ok
    assert a.state in ("not_installed", "unsupported_platform")
    if sys.platform.startswith("linux"):
        assert a.state == "unsupported_platform"
    with pytest.raises(ChatError) as ei:
        AnyChatSource().resolve("x")
    assert ei.value.code == "not_installed"


def test_available_and_not_logged_in(fake_cli, monkeypatch):
    assert AnyChatSource().available().ok
    monkeypatch.setenv("FAKE_ANYCHAT_MODE", "not_logged_in")
    a = AnyChatSource().available()
    assert not a.ok and a.state == "not_logged_in"


def test_resolve_hides_raw_ids(fake_cli):
    cands = AnyChatSource().resolve("张伟")
    assert [c.display_name for c in cands] == ["张伟", "张伟 (同事)"]
    assert cands[0].raw_id == "wxid_abc123"
    assert "wxid" not in json.dumps([c.public() for c in cands])


def test_fetch_writes_transcript_with_expected_command(fake_cli, tmp_path):
    t = AnyChatSource().fetch("张伟", 30, tmp_path / "chat")
    assert t.path.exists() and t.path.name.startswith("anychat_张伟_")
    assert t.message_count == 4 and t.chars == len(TRANSCRIPT)
    assert t.source == "anychat" and t.contact == "张伟"


def test_render_round_trip_through_pdfplumber(tmp_path):
    md = tmp_path / "t.md"
    md.write_text(TRANSCRIPT, encoding="utf-8")
    pdf = transcript_to_pdf(md, tmp_path / "t.pdf", "Chat history: 张伟", ["Source: anychat (self-reported)", "Period: 2026-03-01 to 2026-03-31"])
    pages = pdf_pages_text(pdf)
    assert pages is not None and len(pages) == 1
    text = pages[0]
    assert "Chat history" in text and "self-reported" in text
    assert "Zhang Wei" in text and "多伦多大学" in text


def test_render_paginates_long_transcripts(tmp_path):
    md = tmp_path / "long.md"
    md.write_text("\n".join(f"2026-01-01 00:{i:02d} 张伟: 消息 {i} " + "x" * 120 for i in range(300)), encoding="utf-8")
    pdf = transcript_to_pdf(md, tmp_path / "long.pdf", "Chat", [])
    pages = pdf_pages_text(pdf)
    assert pages is not None and len(pages) > 3
    assert "消息 299" in pages[-1]


def test_redact():
    assert redact("from wxid_abc_12 in 12345678@chatroom") == "from [id] in [id]"


class IntakeRunner(Runner):
    name = "fake"
    binary = "fake"

    def __init__(self):
        super().__init__()
        self.calls = []

    def run(self, *, system_prompt, user_prompt, schema, images, cwd, timeout_s=300, text_files=()):
        self.calls.append({"system_prompt": system_prompt, "user_prompt": user_prompt, "text_files": list(text_files)})
        return RunResult(output={
            "applicant": {"family_name": "Zhang", "given_name": "Wei", "native_name": "张伟", "birthdate": "1998-05-04", "citizenship": "China"},
            "family": [{"name": "李娜", "relationship": "spouse"}, {"name": "child", "relationship": "child", "note": "age 3"}],
            "application": {"program": "tra", "visa_type": "study_visa", "visa_location": "outside_canada",
                            "suggested_application_type_id": "at-study", "confidence": 0.9, "rationale": "asked for a study permit"},
            "key_facts": ["University of Toronto, Sept next year"], "open_questions": ["passport number"],
        }, model="fake", usage={"input_tokens": 5000, "output_tokens": 300})


def test_intake_passes_transcript_as_file_and_validates_catalog(tmp_path):
    from uapply_agent.chat.intake import run_intake
    md = tmp_path / "t.md"
    md.write_text(TRANSCRIPT, encoding="utf-8")
    t = Transcript("anychat", "张伟", "2026-01-01", "2026-03-31", md)
    runner = IntakeRunner()
    hints, usage = run_intake(runner, t, [{"id": "at-study", "name": "Study Permit", "program": "tra", "visa_type": "study_visa", "visa_location": "outside_canada"}], tmp_path / "cwd")
    call = runner.calls[0]
    assert call["text_files"] == [md] and md.name in call["user_prompt"]
    assert "id=at-study" in call["user_prompt"] and "immigration intake" in call["system_prompt"]
    assert hints.applicant.native_name == "张伟" and hints.application.suggested_application_type_id == "at-study"
    assert usage["input_tokens"] == 5000
    # an id outside the catalog is dropped
    hints2, _ = run_intake(runner, t, [{"id": "other"}], tmp_path / "cwd")
    assert hints2.application.suggested_application_type_id is None


def test_intake_caps_long_transcript(tmp_path):
    from uapply_agent.chat.intake import cap_transcript
    md = tmp_path / "t.md"
    md.write_text("\n".join(f"line {i}" for i in range(5000)), encoding="utf-8")
    capped = cap_transcript(md, 2000, tmp_path / "cwd")
    text = capped.read_text()
    assert capped != md and "line 4999" in text and "line 1\n" not in text and text.startswith("(earlier messages omitted")


# ---------- MCP tools ----------

class FakeApi:
    logged_in = True

    def __init__(self):
        self.created, self.uploads = [], []

    def application_types(self):
        return [{"id": "at-study", "code": "sp", "name": "Study Permit", "program": "tra", "visa_type": "study_visa", "visa_location": "outside_canada"}]

    def agent_survey_type(self):
        return {"id": "dt-agent-survey", "name": "Agent Survey", "file_name": "agent_survey"}

    def create_survey(self, name, application_type_id, team_id=None, llm_mode="local_agent", imm_pdf_types=None):
        self.created.append((name, application_type_id, team_id))
        return {"id": "s-new", "name": name}

    def teams(self):
        return []

    def survey(self, survey_id):
        return {"id": survey_id, "name": "Zhang Wei", "dependents": []}

    def set_llm_mode(self, survey_id, mode):
        return {"llm_mode": mode}

    def agent_api_available(self):
        return True

    def bulk_upload(self, survey_id, category, type_id, files, archive_name=""):
        self.uploads.append((survey_id, category, type_id, Path(files[0]).name))
        self.archives = getattr(self, "archives", []) + [archive_name]
        return [{"id": f"doc-{len(self.uploads)}", "file_name": Path(files[0]).name}]


@pytest.fixture
def tools(tmp_path, monkeypatch, fake_cli):
    from uapply_agent import mcp_server as m
    root = tmp_path / "client"
    root.mkdir()
    api = FakeApi()
    monkeypatch.setattr(m, "_folder", WorkingFolder(root))
    monkeypatch.setattr(m, "_api", api)
    monkeypatch.setattr(m, "get_runner", lambda runtime, model: IntakeRunner())
    m._settings.chat_source = "anychat"
    m._settings.anychat_bin = str(fake_cli)
    return m, api


def test_chat_sources_and_find(tools):
    m, _ = tools
    assert m.chat_sources()["sources"][0]["ok"]
    out = m.chat_find_contact("张伟")
    assert [c["display_name"] for c in out["candidates"]] == ["张伟", "张伟 (同事)"]


def test_chat_fetch_without_case_queues_then_create_case_uploads(tools):
    m, api = tools
    out = m.chat_fetch("张伟", days=30)
    assert out["ok"] and out["upload"] == "queued"
    assert out["intake"]["application"]["suggested_application_type_id"] == "at-study"
    assert "wxid" not in json.dumps(out, ensure_ascii=False)
    assert ChatStore(m._folder).pending_uploads()

    refused = m.create_case("Zhang Wei", "at-study", confirmation="yes")
    assert not refused["ok"] and refused["error"]["code"] == "CONFIRMATION_REQUIRED" and api.created == []

    ok = m.create_case("Zhang Wei", "at-study", confirmation="create case")
    assert ok["ok"] and ok["survey_id"] == "s-new" and api.created == [("Zhang Wei", "at-study", None)]
    assert m._folder.survey_id == "s-new"
    assert ok["chat_uploads"][0]["document_id"] == "doc-1"
    assert api.archives == ["Agent Survey"]  # the type's default archive, not a new unnamed one
    assert api.uploads[0][1:3] == ("other", "dt-agent-survey") and api.uploads[0][3].endswith(".pdf")
    assert ChatStore(m._folder).pending_uploads() == []
    # the PDF is in the manifest under .uapply/chat
    assert any(v["path"].startswith(".uapply/chat/") for v in m._folder.manifest["files"].values())
    # a second create_case is refused because the folder is bound
    assert m.create_case("X", "at-study", confirmation="确认创建")["error"]["code"] == "CASE_EXISTS"


def test_chat_fetch_with_case_uploads_immediately(tools):
    m, api = tools
    m.init_case("s-1")
    out = m.chat_fetch("张伟")
    assert out["upload"]["document_id"] == "doc-1"
    assert api.uploads[0][0] == "s-1"


def test_chat_fetch_reports_intake_failure_but_keeps_transcript(tools, monkeypatch):
    m, api = tools
    from uapply_agent.runners.base import RunnerError
    monkeypatch.setattr(m, "get_runner", lambda runtime, model: (_ for _ in ()).throw(RunnerError("no runtime")))
    out = m.chat_fetch("张伟")
    assert out["ok"] and out["intake"] is None and "no runtime" in out["intake_error"]
    assert Path(out["transcript"]["path"]).exists()


def test_upload_is_deduplicated_on_transcript_content(tools):
    m, api = tools
    m.init_case("s-1")
    first = m.chat_fetch("张伟")
    second = m.chat_upload(path=first["transcript"]["path"].replace(str(m._folder.root) + "/", ""))
    assert second["ok"] and second["already_filed"] and second["document_id"] == first["upload"]["document_id"]
    assert len(api.uploads) == 1
    # same contact fetched again on the same day → same content → not filed twice
    again = m.chat_fetch("张伟")
    assert again["upload"]["already_filed"] and len(api.uploads) == 1


def test_chat_upload_can_be_disabled(tools):
    m, api = tools
    m.init_case("s-1")
    m._settings.chat_upload = False
    try:
        out = m.chat_fetch("张伟")
    finally:
        m._settings.chat_upload = True
    assert out["upload"] == "disabled" and api.uploads == [] and out["intake"] is not None


def test_create_case_attaches_default_forms_and_single_team(tools, monkeypatch):
    m, api = tools
    api.application_types = lambda: [{"id": "at-study", "name": "Study Permit", "default_imm_pdf_types": ["imm-1294", "imm-5645"]}]
    api.teams = lambda: [{"id": "team-1", "name": "Maple Immigration"}]
    captured = {}

    def create_survey(name, application_type_id, team_id=None, llm_mode="local_agent", imm_pdf_types=None):
        captured.update(team_id=team_id, imm_pdf_types=imm_pdf_types)
        return {"id": "s-new", "name": name}

    api.create_survey = create_survey
    out = m.create_case("Zhang Wei", "at-study", confirmation="create case")
    assert out["ok"] and out["team_id"] == "team-1"
    assert captured == {"team_id": "team-1", "imm_pdf_types": ["imm-1294", "imm-5645"]}
    assert m.create_case("X", "nope", confirmation="create case")["error"]["code"] == "CASE_EXISTS"


def test_create_case_rejects_unknown_type(tools):
    m, api = tools
    assert m.create_case("X", "nope", confirmation="create case")["error"]["code"] == "BAD_APPLICATION_TYPE"


def test_intake_prompt_attributes_speakers(tmp_path):
    from uapply_agent.chat.intake import run_intake
    md = tmp_path / "t.md"
    md.write_text(TRANSCRIPT, encoding="utf-8")
    runner = IntakeRunner()
    run_intake(runner, Transcript("anychat", "张伟", "2026-01-01", "2026-03-31", md), [], tmp_path / "cwd")
    assert "messages from '张伟' are the client's own statements" in runner.calls[0]["user_prompt"]


def test_schema_refs_are_inlined_for_the_runtime():
    from uapply_agent.chat.intake import IntakeHints
    from uapply_agent.runners.base import inline_schema_refs
    flat = inline_schema_refs(IntakeHints.model_json_schema())
    assert "$defs" not in flat and "$ref" not in json.dumps(flat)
    assert flat["properties"]["applicant"]["properties"]["native_name"]["anyOf"][0]["type"] == "string"
    assert flat["properties"]["family"]["items"]["properties"]["relationship"]["type"] == "string"


def test_codex_runner_sends_prompt_on_stdin(monkeypatch, tmp_path):
    import subprocess
    from uapply_agent.runners.codex import CodexRunner
    seen = {}

    def fake_exec(self, cmd, cwd, timeout_s, stdin=None):
        seen["cmd"], seen["stdin"] = cmd, stdin
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout='{"file_types": ["Visa"]}', stderr="")

    monkeypatch.setattr(CodexRunner, "_exec", fake_exec)
    big = tmp_path / "big.md"
    big.write_text("x" * 100_000)
    rr = CodexRunner().run(system_prompt="S", user_prompt="U", schema={"type": "object"}, images=[], cwd=tmp_path, text_files=[big])
    assert rr.output == {"file_types": ["Visa"]}
    assert seen["cmd"][-1] == "-" and "x" * 100_000 in seen["stdin"] and "## Instructions" in seen["stdin"]
    assert all(len(a) < 1000 for a in seen["cmd"])


def test_create_case_without_agent_api_still_binds_the_folder(tools, monkeypatch):
    """The case is created (and billed) first; binding must not fail afterwards on the agent API."""
    m, api = tools
    monkeypatch.setattr(api, "agent_api_available", lambda: False)
    monkeypatch.setattr(api, "set_llm_mode", lambda *a: (_ for _ in ()).throw(AssertionError("agent API called")))
    out = m.create_case("Zhang Wei", "at-study", confirmation="create case")
    assert out["ok"] and out["case"]["llm_mode"] == "server" and "server mode" in out["warning"]
    assert m._folder.survey_id == "s-new"
