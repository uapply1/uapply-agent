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

    def survey(self, survey_id):
        return {"id": survey_id, "name": "Zhang Wei", "dependents": [],
                "document_types": [{"id": "dt-pass", "name": "Passport & Visa & Permit", "file_name": "current_prev_passports",
                                    "category": "identity", "requirement": "required", "can_process": True}]}

    def survey_document_types(self, survey_id):
        return self.survey(survey_id)["document_types"]

    def set_llm_mode(self, survey_id, mode):
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
