"""MCP tool logic against a fake API: upload matching, type listing, start_processing scoping."""
from pathlib import Path

import pytest

from uapply_agent import mcp_server as m
from uapply_agent.folder import WorkingFolder


class FakeApi:
    logged_in = True

    def __init__(self):
        self.uploads = []
        self.started = []
        self.docs = []
        self.agent_api = True
        self.modes = []

    def agent_api_available(self):
        return self.agent_api

    def survey(self, survey_id):
        return {"id": survey_id, "name": "Zhang Wei", "dependents": [], "documents": self.docs,
                "analyzing_status": "none",
                "document_types": [{"id": "dt-pass", "name": "Passport & Visa & Permit", "file_name": "current_prev_passports",
                                    "category": "identity", "requirement": "required", "can_process": True}]}

    def survey_document_types(self, survey_id):
        return self.survey(survey_id)["document_types"]

    def agent_survey_type(self):
        return {"id": "dt-agent", "name": "Agent Survey", "file_name": "agent_survey"}

    def set_llm_mode(self, survey_id, mode):
        self.modes.append(mode)
        return {"llm_mode": mode}

    def bulk_upload(self, survey_id, category, document_type_id, files, archive_name=""):
        assert len(files) == 1
        name = Path(files[0]).name
        doc_id = f"doc-{len(self.uploads) + 1}"
        self.uploads.append((category, document_type_id, name))
        self.archives = getattr(self, "archives", []) + [archive_name]
        # The server converts HEIC to PNG and renames accordingly.
        server_name = name[:-5] + ".png" if name.lower().endswith(".heic") else name
        return [{"id": doc_id, "file_name": server_name}]

    def documents(self, survey_id):
        return self.docs

    def start_document_processing(self, survey_id, document_id):
        self.started.append(document_id)
        return {"message": "ok"}


@pytest.fixture
def bound(tmp_path, monkeypatch):
    root = tmp_path / "client"
    root.mkdir()
    (root / "passport.pdf").write_bytes(b"%PDF-1.4 a")
    (root / "spouse").mkdir()
    (root / "spouse" / "passport.pdf").write_bytes(b"%PDF-1.4 b")
    api = FakeApi()
    monkeypatch.setattr(m, "_folder", WorkingFolder(root))
    monkeypatch.setattr(m, "_api", api)
    m.init_case("s-1")
    return api


def test_duplicate_basenames_map_to_their_own_document_ids(bound):
    out = m.sync_documents(document_type_id="dt-pass")
    assert out["ok"] and out["uploaded"] == 2 and out["failed"] == []
    ids = {d["path"]: d["document_id"] for d in out["documents"]}
    assert ids["passport.pdf"] != ids["spouse/passport.pdf"]
    # category came from the survey's type, not from the caller
    assert all(u[0] == "identity" for u in bound.uploads)
    # second run uploads nothing
    again = m.sync_documents(document_type_id="dt-pass")
    assert again["uploaded"] == 0 and sorted(again["skipped"]) == ["passport.pdf", "spouse/passport.pdf"]


def test_heic_is_converted_locally_and_recorded(bound, monkeypatch, tmp_path):
    root = m._folder.root
    (root / "IMG_1.HEIC").write_bytes(b"heic")
    def fake_convert(src, cache):
        cache.mkdir(parents=True, exist_ok=True)
        out = cache / "IMG_1.jpg"
        out.write_bytes(b"jpg")
        return out

    monkeypatch.setattr(m, "heic_to_jpeg", fake_convert)
    out = m.sync_documents(document_type_id="dt-pass", paths=["IMG_1.HEIC"])
    assert out["uploaded"] == 1 and out["failed"] == []
    assert bound.uploads[-1][2] == "IMG_1.jpg"
    entry = next(v for v in m._folder.manifest["files"].values() if v["path"] == "IMG_1.HEIC")
    assert entry["document_id"] == out["documents"][0]["document_id"]
    assert entry["uploaded_path"].endswith("IMG_1.jpg")


def test_bad_category_is_refused(bound):
    out = m.sync_documents(document_type_id="dt-pass", document_category="passports")
    assert not out["ok"] and out["error"]["code"] == "BAD_CATEGORY"


def test_list_document_types_uses_survey_types(bound):
    out = m.list_document_types()
    assert out["document_types"][0]["category"] == "identity"
    assert m.list_document_types(query="nothing")["document_types"] == []


def test_start_processing_skips_page_documents(bound):
    bound.docs = [
        {"id": "parent", "status": "failed", "old_doc_id": None},
        {"id": "page-1", "status": "failed", "old_doc_id": "parent"},
        {"id": "done", "status": "completed", "old_doc_id": None},
    ]
    out = m.start_processing()
    assert [s["document_id"] for s in out["started"]] == ["parent"]
    assert bound.started == ["parent"]


def test_set_folder_switches_case(bound, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    out = m.set_folder(str(other))
    assert out["ok"] and out["case"] is None
    assert not m.set_folder(str(tmp_path / "missing"))["ok"]


def test_upload_goes_to_the_types_default_archive(bound):
    m.sync_documents("dt-pass", paths=["passport.pdf"])
    assert bound.archives == ["Passport & Visa & Permit"]


def test_archive_name_override(bound):
    m.sync_documents("dt-pass", paths=["passport.pdf"], archive_name="Previous passports")
    assert bound.archives == ["Previous passports"]


def test_old_backend_binds_in_server_mode_and_gates_agent_tools(bound):
    bound.agent_api = False
    out = m.init_case("s-1")
    assert out["ok"] and out["case"]["llm_mode"] == "server" and "server mode" in out["warning"]
    assert bound.modes == ["local_agent"]  # only the first bind (agent api on) called set_llm_mode
    for tool in (m.run_tasks, m.wait_for_stage, m.task_stats):
        r = tool()
        assert not r["ok"] and r["error"]["code"] == "AGENT_API_UNAVAILABLE"
    assert m.set_llm_mode("local_agent")["error"]["code"] == "AGENT_API_UNAVAILABLE"


def test_case_status_falls_back_to_survey_documents(bound, monkeypatch):
    bound.agent_api = False
    monkeypatch.setattr(bound, "survey", lambda sid: {"id": sid, "name": "Zhang Wei", "dependents": [], "document_types": [],
                                                      "documents": [{"id": "a", "status": "completed", "old_doc_id": None},
                                                                    {"id": "a1", "status": "completed", "old_doc_id": "a"},
                                                                    {"id": "b", "status": "failed", "old_doc_id": None}]})
    out = m.case_status()
    assert out["ok"] and out["agent_api"] is False and out["documents"] == {"completed": 1, "failed": 1}


def test_whoami_reports_agent_api_and_runtime_paths(bound, monkeypatch):
    monkeypatch.setattr(m, "detect_runtimes", lambda: [{"name": "claude-code", "path": "/x/claude"}])
    monkeypatch.setattr(m, "_claude_login_state", lambda path: False)
    out = m.whoami()
    assert out["agent_api"] is True and out["runtimes"] == [{"name": "claude-code", "path": "/x/claude", "logged_in": False}]
    assert "claude auth login" in out["hint"]
    monkeypatch.setattr(m, "detect_runtimes", lambda: [])
    assert "installer" in m.whoami()["hint"]


def test_preview_document_renders_pdf_pages_locally(bound, tmp_path):
    import pymupdf
    root = m._folder.root
    doc = pymupdf.open()
    for i in range(4):
        page = doc.new_page(); page.insert_text((72, 72), f"page {i + 1}")
    doc.save(root / "scan.pdf"); doc.close()
    out = m.preview_document("scan.pdf")
    assert out["ok"] and out["page_count"] == 4 and len(out["images"]) == 1 and out["images"][0].endswith("_p001.png")
    assert Path(out["images"][0]).exists() and ".uapply/cache/preview" in out["images"][0].replace("\\", "/")
    out = m.preview_document("scan.pdf", pages="2-9")
    assert out["pages"] == "2-4" and len(out["images"]) == 3
    (root / "photo.jpg").write_bytes(b"\xff\xd8\xff")
    assert m.preview_document("photo.jpg")["images"] == [str(root / "photo.jpg")]
    assert m.preview_document("missing.pdf")["error"]["code"] == "NO_FILE"


def test_no_case_hint_offers_create_or_bind(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "_folder", WorkingFolder(tmp_path))
    r = m.list_documents()
    assert r["error"]["code"] == "NO_CASE" and "create_case" in r["error"]["hint"] and "init_case" in r["error"]["hint"]
    from uapply_agent import playbook
    run = playbook.prompt("run")
    assert "Create a new case" in run and "survey id" in run and "question tool" in run
    rules = playbook.instructions()
    assert "AskUserQuestion" in rules and "Never end your turn to ask a question" in rules
    assert '"Create case"' in rules  # the charge is confirmed by the RCIC picking this option


def test_generic_agent_survey_type_listed_once_for_imm_forms(bound):
    rows = m.list_document_types()["document_types"]
    generic = [r for r in rows if r.get("generic")]
    assert len(generic) == 1 and generic[0]["id"] == "dt-agent" and "IMM forms only" in generic[0]["use_for"]


def test_imm_form_uploads_under_agent_survey_folder(bound):
    m.sync_documents("dt-agent", paths=["passport.pdf"])
    assert bound.uploads[-1][:2] == ("other", "dt-agent") and bound.archives == ["Agent Survey"]


def test_unknown_type_is_refused_without_uploading(bound):
    r = m.sync_documents("dt-nope", paths=["passport.pdf"])
    assert r["error"]["code"] == "UNKNOWN_TYPE" and bound.uploads == []


def test_whoami_flags_an_installed_runtime_that_does_not_start(bound, monkeypatch):
    monkeypatch.setattr(m, "detect_runtimes", lambda: [{"name": "claude-code", "path": "C:/nodejs/claude.CMD",
                                                        "error": "not compatible with the version of Windows"}])
    out = m.whoami()
    assert "does not start" in out["hint"] and "logged_in" not in out["runtimes"][0]


def test_file_already_on_the_case_is_recorded_not_reuploaded(bound):
    """The upload succeeded but the local record was lost: match by type, name and size."""
    size = (m._folder.root / "passport.pdf").stat().st_size
    bound.docs = [{"id": "srv-1", "file_name": "passport.pdf", "document_type_id": "dt-pass", "size": size, "old_doc_id": None},
                  {"id": "srv-2", "file_name": "passport.pdf", "document_type_id": "dt-other", "size": size, "old_doc_id": None}]
    out = m.sync_documents("dt-pass", paths=["passport.pdf"])
    assert out["already_on_server"] == [{"path": "passport.pdf", "document_id": "srv-1"}] and out["uploaded"] == 0
    assert bound.uploads == []
    assert m.sync_documents("dt-pass", paths=["passport.pdf"])["skipped"] == ["passport.pdf"]


def test_same_name_different_size_is_uploaded(bound):
    bound.docs = [{"id": "srv-1", "file_name": "passport.pdf", "document_type_id": "dt-pass", "size": 999999, "old_doc_id": None}]
    out = m.sync_documents("dt-pass", paths=["passport.pdf"])
    assert out["uploaded"] == 1 and out["already_on_server"] == []


def test_run_tasks_reports_per_document_progress(bound, monkeypatch):
    class FakeExecutor:
        def __init__(self, *a, **kw):
            self.runner = type("R", (), {"name": "claude-code"})()

        def run(self, **kw):
            assert 20 <= kw["budget_s"] <= 300
            from uapply_agent.executor import RunStats
            return RunStats(accepted=2, remaining=3)
    monkeypatch.setattr(m, "Executor", FakeExecutor)
    bound.docs = [
        {"id": "a", "file_name": "passport & sp.pdf", "status": "analyzing", "old_doc_id": None},
        {"id": "a1", "file_name": "passport & sp_p1.png", "status": "completed", "old_doc_id": "a"},
        {"id": "b", "file_name": "birth certificate.pdf", "status": "completed", "old_doc_id": None},
        {"id": "c", "file_name": "photo.jpg", "status": "failed", "error": "bad image", "old_doc_id": None},
    ]
    out = m.run_tasks()
    p = out["progress"]
    assert out["accepted"] == 2 and out["remaining"] == 3
    assert p["documents_total"] == 3 and p["documents_finished"] == 2       # pages are not counted
    assert p["in_progress"] == ["passport & sp.pdf"] and p["failed"][0]["file_name"] == "photo.jpg"


def test_start_analysis_waits_for_processing_then_starts(bound):
    bound.docs = [{"id": "a", "file_name": "passport.pdf", "status": "analyzing", "old_doc_id": None}]
    started = []
    bound.start_analysis = lambda sid: started.append(sid) or {"message": "started"}
    r = m.start_analysis()
    assert r["error"]["code"] == "PROCESSING_NOT_DONE" and started == []
    bound.docs[0]["status"] = "completed"
    r = m.start_analysis()
    assert r["ok"] and started == ["s-1"] and r["progress"]["analysis"] == "none"



def test_photos_never_block_analysis_or_progress(bound, monkeypatch):
    """A Digital Photo is stored, never processed: it stays 'uploaded' and must not hold analysis back."""
    types = [{"id": "dt-pass", "name": "Passport", "file_name": "current_prev_passports", "category": "identity",
              "can_process": True},
             {"id": "dt-photo", "name": "Digital Photo", "file_name": "digital_photo", "category": "identity",
              "can_process": False}]
    orig = bound.survey
    monkeypatch.setattr(bound, "survey", lambda sid: {**orig(sid), "document_types": types})
    bound.docs = [{"id": "p", "file_name": "passport.pdf", "status": "completed", "old_doc_id": None,
                   "document_type_id": "dt-pass"},
                  {"id": "ph", "file_name": "photo.jpg", "status": "uploaded", "old_doc_id": None,
                   "document_type_id": "dt-photo"}]
    started = []
    bound.start_analysis = lambda sid: started.append(sid) or {"message": "ok"}
    r = m.start_analysis()
    assert r["ok"] and started == ["s-1"]
    p = r["progress"]
    assert p["documents_total"] == 1 and p["documents_finished"] == 1 and p["not_processed"] == ["photo.jpg"]
    out = m.start_processing()
    assert out["started"] == []            # the photo is not sent for processing
