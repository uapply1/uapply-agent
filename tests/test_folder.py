from pathlib import Path

import pytest

from uapply_agent.folder import WorkingFolder
from uapply_agent.util import sha256_of


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


def test_state_files_are_utf8_whatever_the_system_code_page(tmp_path):
    """English Windows defaults to cp1252: Chinese names must still round-trip (UTF-8 on disk)."""
    import os
    import subprocess
    import sys
    name, shot = "李天毅".encode("unicode_escape").decode(), "屏幕截图 1.png".encode("unicode_escape").decode()
    script = tmp_path / "run.py"   # ASCII-only source: an ASCII locale cannot even decode a Chinese argv
    script.write_text(
        "from pathlib import Path; from uapply_agent.folder import WorkingFolder\n"
        f"f = WorkingFolder(Path({str(tmp_path)!r}))\n"
        f"f.init_case('s-1', 'https://api', 'local_agent', name='{name}')\n"
        f"f.record_upload('abc', '{shot}', 'doc-1')\n"
        "g = WorkingFolder(Path(f.root))\n"
        f"print(g.case['name'] == '{name}', g.manifest['files']['abc']['path'] == '{shot}')\n", encoding="ascii")
    env = {**os.environ, "LC_ALL": "C", "PYTHONCOERCECLOCALE": "0", "PYTHONUTF8": "0"}
    out = subprocess.run([sys.executable, str(script)], env=env, capture_output=True, text=True, encoding="utf-8")
    assert out.stdout.strip() == "True True", out.stderr[-500:]
    assert "李天毅".encode() in (tmp_path / ".uapply" / "case.json").read_bytes()


def test_truncated_state_file_is_set_aside_not_fatal(tmp_path):
    from uapply_agent.folder import WorkingFolder
    f = WorkingFolder(tmp_path)
    f.state.mkdir()
    (f.state / "manifest.json").write_bytes(b"")          # what the old build left after a failed write
    assert f.manifest == {"schema": 1, "files": {}}
    assert any(p.name.startswith("manifest.json.corrupt-") for p in f.state.iterdir())
    f.record_upload("abc", "a.pdf", "doc-1")
    assert WorkingFolder(tmp_path).manifest["files"]["abc"]["document_id"] == "doc-1"


def test_state_file_in_a_legacy_code_page_is_still_read(tmp_path, monkeypatch):
    import locale

    from uapply_agent.folder import WorkingFolder
    monkeypatch.setattr(locale, "getpreferredencoding", lambda do_setlocale=True: "gbk")
    f = WorkingFolder(tmp_path)
    f.state.mkdir()
    (f.state / "case.json").write_bytes('{"name": "李天毅"}'.encode("gbk"))   # older build on Chinese Windows
    assert f.case["name"] == "李天毅"
