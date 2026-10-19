"""identify_files: a helper looks at the folder's pages; the conversation gets text only."""
from pathlib import Path

from test_extract_content import make_pdf
from uapply_agent import identify
from uapply_agent.config import Settings
from uapply_agent.runners.base import Runner, RunResult


class Api:
    def survey_document_types(self, survey_id):
        return [{"id": "t-pass", "name": "Passport", "file_name": "passport", "description": "Passport bio page"}]

    def agent_survey_type(self):
        return {"id": "t-survey", "name": "Agent Survey"}


def server(use_server, folder, task_runner="session"):
    from uapply_agent import mcp_server as m
    use_server(folder, Api(), Settings(task_runner=task_runner))
    return m


def answer(paths, type_id="t-pass"):
    return {"files": [{"path": p, "document_type_id": type_id, "document_type": "Passport", "holder": "LI, TIANYI",
                       "key_facts": "EJ3504068, expires 2034-05-01", "confidence": "high"} for p in paths],
            "applicant": {"family_name": "LI", "given_names": "TIANYI"}, "notes": ""}


def folder_files(folder):
    make_pdf(folder.root / "passport & sp.pdf", 3, text=False)
    (folder.root / "photo.jpg").write_bytes(b"\xff\xd8 fake")
    (folder.root / "notes.zip").write_bytes(b"PK")


def test_session_mode_writes_one_brief_and_returns_only_a_prompt(use_server, case_folder):
    folder_files(case_folder)
    m = server(use_server, case_folder)
    out = m.identify_files()
    assert out["ok"] and out["files"] == 2 and out["prompt"].startswith("Task brief: ")
    assert out["skipped"] == []                                           # the scan ignores notes.zip
    explicit = m.identify_files(["notes.zip", "missing.pdf"])
    assert explicit["files"] == 0 and [s["reason"] for s in explicit["skipped"]] == [
        "cannot read .zip files", "not a file in the working folder"]
    out = m.identify_files()
    assert not any(k in out for k in ("images", "identified"))          # nothing to Read here
    brief = Path(out["prompt"].removeprefix("Task brief: "))
    text = brief.read_text(encoding="utf-8")
    assert "1. `passport & sp.pdf` → `f01_p1.png`, `f01_p2.png` (pages 1-2 of 3)" in text
    assert "2. `photo.jpg` → `f02.jpg`" in text
    assert "`submit_identification`" in text and '"t-pass"' in text and '"generic": true' in text
    assert {p.name for p in brief.parent.iterdir()} >= {"f01_p1.png", "f01_p2.png", "f02.jpg"}


def test_submission_is_checked_then_read_back(use_server, case_folder):
    folder_files(case_folder)
    m = server(use_server, case_folder)
    assert m.identified_files()["error"]["code"] == "NOT_IDENTIFIED"
    m.identify_files()
    missing = m.submit_identification(answer(["photo.jpg"]))
    assert missing["accepted"] is False and "passport & sp.pdf" in missing["feedback"]
    wrong_type = m.submit_identification(answer(["photo.jpg", "passport & sp.pdf"], type_id="t-nope"))
    assert wrong_type["accepted"] is False and "not in the list" in wrong_type["feedback"]
    assert m.submit_identification(answer(["photo.jpg", "passport & sp.pdf"]))["accepted"] is True
    got = m.identified_files()["identified"]
    assert got["files"][1]["key_facts"].startswith("EJ3504068")


def test_without_a_case_types_are_left_null(use_server, tmp_path):
    from uapply_agent.folder import WorkingFolder
    folder = WorkingFolder(tmp_path / "unbound")
    folder.root.mkdir()
    (folder.root / "photo.jpg").write_bytes(b"\xff\xd8 fake")
    m = server(use_server, folder)
    text = Path(m.identify_files()["prompt"].removeprefix("Task brief: ")).read_text(encoding="utf-8")
    assert "No case is bound yet: set every document_type_id to null." in text


def test_cli_mode_runs_the_runtime_with_the_page_images(use_server, case_folder, monkeypatch):
    folder_files(case_folder)
    seen = {}

    class FakeRunner(Runner):
        name = "codex"

        def run(self, *, system_prompt, user_prompt, schema, images, cwd, timeout_s=300, text_files=()):
            seen["images"] = [p.name for p in images]
            return RunResult(output=answer(["photo.jpg", "passport & sp.pdf"]))
    m = server(use_server, case_folder, task_runner="cli")
    monkeypatch.setattr(m, "get_runner", lambda *a, **k: FakeRunner())
    out = m.identify_files()
    assert seen["images"] == ["f01_p1.png", "f01_p2.png", "f02.jpg"]
    assert out["identified"]["applicant"]["family_name"] == "LI"
    assert identify.load(case_folder)["files"][0]["path"] == "photo.jpg"


def test_a_new_identification_starts_from_an_empty_directory(case_folder):
    (case_folder.root / "a.jpg").write_bytes(b"\xff\xd8 a")
    (case_folder.root / "b.jpg").write_bytes(b"\xff\xd8 b")
    identify.prepare(case_folder, ["a.jpg", "b.jpg"], [])
    prep = identify.prepare(case_folder, ["b.jpg"], [])
    assert [p.name for p in (case_folder.cache / identify.TASK_ID).iterdir()] == ["f01.jpg"]
    assert prep["files"] == ["b.jpg"]


def test_task_runner_may_submit_identifications():
    from uapply_agent import playbook
    head = playbook.plugin_files("1.0.0")["agents/task-runner.md"].split("---")[1]
    assert "mcp__uapply__submit_identification" in head
    assert "Never Read a client's" in playbook.instructions()
