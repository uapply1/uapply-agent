from pathlib import Path

import pytest

from uapply_agent.folder import WorkingFolder, sha256_of


def make_folder(tmp_path: Path) -> WorkingFolder:
    (tmp_path / "passport.pdf").write_bytes(b"%PDF-1.4 fake")
    (tmp_path / "spouse").mkdir()
    (tmp_path / "spouse" / "id.jpg").write_bytes(b"\xff\xd8 fake")
    (tmp_path / "~$lock.docx").write_bytes(b"x")
    (tmp_path / ".hidden.pdf").write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("ignored")
    return WorkingFolder(tmp_path)


def test_scan_lists_supported_files_with_hints(tmp_path):
    f = make_folder(tmp_path)
    files = f.scan()
    paths = {x.path: x for x in files}
    assert set(paths) == {"passport.pdf", "spouse/id.jpg"}
    assert paths["spouse/id.jpg"].applicant_hint == "spouse"
    assert paths["passport.pdf"].applicant_hint is None
    assert paths["passport.pdf"].kind == "pdf" and paths["spouse/id.jpg"].kind == "image"
    assert paths["passport.pdf"].sha256 == sha256_of(tmp_path / "passport.pdf")
    assert not any(x.manifested for x in files)


def test_manifest_round_trip_and_rename(tmp_path):
    f = make_folder(tmp_path)
    digest = sha256_of(tmp_path / "passport.pdf")
    f.record_upload(digest, "passport.pdf", "doc-1")
    assert f.manifest["files"][digest]["document_id"] == "doc-1"
    assert (f.state / ".gitignore").exists()
    # Renaming does not create a new upload; the hash is the identity.
    (tmp_path / "passport.pdf").rename(tmp_path / "zhang_wei_passport.pdf")
    scanned = {x.path: x for x in f.scan(include_manifested=True)}
    assert scanned["zhang_wei_passport.pdf"].manifested
    assert scanned["zhang_wei_passport.pdf"].document_id == "doc-1"
    assert [x.path for x in f.scan(include_manifested=False)] == ["spouse/id.jpg"]


def test_case_json_and_family_ids(tmp_path):
    f = make_folder(tmp_path)
    assert f.family_survey_ids == []
    f.init_case("s-1", "https://api.example", dependents=[{"survey_id": "s-2", "name": "Li Na", "relationship": "spouse"}])
    assert f.survey_id == "s-1"
    assert f.family_survey_ids == ["s-1", "s-2"]
    assert f.case["stage_history"][0]["stage"] == "init"


def test_resolve_refuses_paths_outside_folder(tmp_path):
    f = make_folder(tmp_path)
    assert f.resolve("passport.pdf") == (tmp_path / "passport.pdf").resolve()
    with pytest.raises(ValueError):
        f.resolve("../etc/passwd")


def test_clean_removes_cache_only(tmp_path):
    f = make_folder(tmp_path)
    f.cache.mkdir(parents=True)
    (f.cache / "x.png").write_bytes(b"x")
    assert f.clean() == 1
    assert (tmp_path / "passport.pdf").exists()
