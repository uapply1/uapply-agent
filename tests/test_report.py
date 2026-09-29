"""End-of-run report: rendering, dashboard links, final package download."""
import io
import zipfile

import pytest

from uapply_agent.config import Settings
from uapply_agent.folder import WorkingFolder
from uapply_agent import report


REPORT = {
    "survey_id": "s-1", "name": "Li, Tianyi", "application_type": "Study Permit", "program": "tra",
    "analyzing_status": "completed", "automation_status": "completed",
    "documents": {"completed": 9, "failed": 1, "uploaded": 0},
    "failures": [{"document_id": "d", "file_name": "passport & sp.pdf", "error": "No sections were extracted"}],
    "ai_check": {"available": True, "conflict": 2, "doubtful": 5, "missing": 3,
                 "pages": {"5709": {"conflict": 2, "doubtful": 1, "missing": 0}}},
    "imm_pdfs": [{"name": "Imm5709e", "applicant": "Li, Tianyi", "status": "success", "success_rate": 91.6, "error": ""},
                 {"name": "Imm5645e", "applicant": "Li, Tianyi", "status": "failed", "success_rate": 0, "error": "Acrobat crashed"}],
    "extra_files": [], "archives": [{"compressing": "completed"}, {"compressing": "none"}],
    "submit_files": [{"type": "forms", "file_name": "a.pdf"}],
    "online_portal": {"eligible": False, "ready": False},
}


class FakeApi:
    def __init__(self, r=REPORT):
        self.r = r

    def report(self, sid):
        return self.r

    def download_submit_package(self, sid, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("To Submit/Forms/IMM5709_李天毅.pdf", b"%PDF")
            z.writestr("To Submit/Doc/passport.pdf", b"%PDF")
            z.writestr("To Submit/Forms/../../evil.pdf", b"x")
        dest.write_bytes(buf.getvalue())
        return dest


@pytest.fixture
def folder(tmp_path):
    f = WorkingFolder(tmp_path / "client")
    f.root.mkdir()
    f.init_case("s-1", "https://api.uapply.io")
    return f


def settings(backend="https://api.uapply.io"):
    s = Settings()
    s.backend_url = backend
    return s


def test_links_point_at_the_dashboard_steps():
    lk = report.links(settings(), "s-1")
    assert lk["submit"] == "https://app.uapply.io/rcic/survey/s-1?step=submit"
    assert lk["online_portal"].endswith("?step=submit&portal=1")
    assert report.links(settings("http://localhost:8000"), "s-1")["case"].startswith("http://localhost:8080/")


def test_report_leads_with_ai_check_and_lists_forms(folder):
    out = report.build(FakeApi(), folder, settings())
    md = out["report_markdown"]
    assert md.index("Review before submitting") < md.index("## Documents")
    assert "2 conflict(s) and 5 doubtful" in md and "?step=ai_check" in md
    assert "| Imm5709e | Li, Tianyi | success | 92% |" in md and "Acrobat crashed" in md
    assert "passport & sp.pdf — No sections were extracted" in md
    assert "Start online portal" not in md                       # not a PR case
    assert (folder.root / "uApply output" / "report.md").read_text(encoding="utf-8") == md


def test_final_package_is_saved_with_forms_unpacked(folder):
    out = report.build(FakeApi(), folder, settings())
    assert out["files"]["package"] == "uApply output/Li, Tianyi_final_package.zip"
    assert (folder.root / "uApply output" / "Forms" / "IMM5709_李天毅.pdf").exists()
    assert not (folder.root / "evil.pdf").exists() and not (folder.root.parent / "evil.pdf").exists()


def test_online_portal_link_only_when_ready(folder):
    r = {**REPORT, "online_portal": {"eligible": True, "ready": True}}
    md = report.build(FakeApi(r), folder, settings(), download=False)["report_markdown"]
    assert "[Start online portal](https://app.uapply.io/rcic/survey/s-1?step=submit&portal=1)" in md
    r = {**REPORT, "online_portal": {"eligible": True, "ready": False}}
    md = report.build(FakeApi(r), folder, settings(), download=False)["report_markdown"]
    assert "available once auto-fill" in md


def test_status_label_matches_the_dashboard():
    assert report.status_label({"automation_status": "none"}) == "Not started"
    assert report.status_label({"automation_status": "filling", "analyzing_status": "completed"}) == "Filling forms"
    assert report.status_label({"automation_status": "completed", "analyzing_status": "completed"}) == "Completed"
