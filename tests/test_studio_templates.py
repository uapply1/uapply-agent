"""Content Studio templates: zipping a package folder, the Community client and the MCP tools."""
import io
import json
import zipfile

import httpx
import pytest

from uapply_agent import mcp_server as m
from uapply_agent import playbook
from uapply_agent.api import MY_TEMPLATES, CommunityApi
from uapply_agent.config import Settings
from uapply_agent.folder import WorkingFolder
from uapply_agent.studio_templates import PackageError, zip_package


def _package(root):
    pkg = root / "studio-templates" / "tips_card"
    (pkg / "assets").mkdir(parents=True)
    (pkg / "template.json").write_text(json.dumps({"id": "tips_card"}))
    (pkg / "portrait.svg").write_text("<svg/>")
    (pkg / "assets" / "art.png").write_bytes(b"png")
    (pkg / ".DS_Store").write_bytes(b"x")
    (pkg / ".hidden").mkdir()
    (pkg / ".hidden" / "x.svg").write_text("<svg/>")
    return pkg


def test_zip_package_keeps_relative_paths_and_skips_hidden_files(tmp_path):
    names = zipfile.ZipFile(io.BytesIO(zip_package(_package(tmp_path)))).namelist()
    assert sorted(names) == ["assets/art.png", "portrait.svg", "template.json"]


def test_zip_package_skips_links_out_of_the_folder(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("token")
    pkg = _package(tmp_path / "work")
    (pkg / "assets" / "leak.png").symlink_to(secret)
    names = zipfile.ZipFile(io.BytesIO(zip_package(pkg))).namelist()
    assert "assets/leak.png" not in names and "assets/art.png" in names


def test_zip_package_needs_a_manifest(tmp_path):
    with pytest.raises(PackageError, match="template.json"):
        zip_package(tmp_path)


class FakeCommunity:
    def __init__(self, check=None, upload=None):
        self.sent = []
        self._check = check or {"ok": True, "errors": [], "warnings": ["fixed text"]}
        self._upload = upload or {"ok": True, "slug": "tips_card--u7", "name": "Tips", "uploaded_revision": 2,
                                  "status": "draft", "preview_url": "https://c.test/console/studio/templates/tips_card--u7?revision=2",
                                  "errors": [], "warnings": []}

    def template_guide(self):
        return {"markdown": "# Guide", "slots": {"headline": {"kind": "text"}}, "icons": ["star"], "content_types": ["news"],
                "example": {"template.json": "{}"}, "limit": 20}

    def template_check(self, package):
        self.sent.append(("check", zipfile.ZipFile(io.BytesIO(package)).namelist()))
        return self._check

    def template_upload(self, package):
        self.sent.append(("upload", zipfile.ZipFile(io.BytesIO(package)).namelist()))
        return self._upload

    def template_publish(self, slug, revision=None):
        self.sent.append(("publish", slug, revision))
        return {"slug": slug, "name": "Tips", "status": "published", "published_revision": revision or 2,
                "content_types": ["news"], "preview_url": "u"}

    def my_templates(self):
        return {"results": [{"slug": "tips_card--u7", "id": "tips_card", "name": "Tips", "status": "draft", "extra": 1}], "limit": 20}


@pytest.fixture
def studio(tmp_path, use_server):
    root = tmp_path / "work"
    root.mkdir()
    _package(root)
    ctx = use_server(WorkingFolder(root), api=None)
    ctx._community = FakeCommunity()
    return ctx


def test_tools_need_no_case_and_send_the_zipped_folder(studio):
    guide = m.studio_template_guide()
    assert guide["ok"] and guide["guide"] == "# Guide" and guide["slots"]["headline"]["kind"] == "text"
    out = m.studio_template_check(folder="studio-templates/tips_card")
    assert out == {"ok": True, "passed": True, "errors": [], "warnings": ["fixed text"]}
    up = m.studio_template_upload(folder="studio-templates/tips_card")
    assert up["uploaded"] and up["slug"] == "tips_card--u7" and up["revision"] == 2 and up["preview_url"].endswith("revision=2")
    assert [s[0] for s in studio._community.sent] == ["check", "upload"]
    assert "template.json" in studio._community.sent[0][1]
    pub = m.studio_template_publish(slug="tips_card--u7")
    assert pub["status"] == "published" and studio._community.sent[-1] == ("publish", "tips_card--u7", None)
    assert m.my_studio_templates()["templates"][0] == {"slug": "tips_card--u7", "id": "tips_card", "name": "Tips", "status": "draft",
                                                       "content_types": None, "published_revision": None,
                                                       "latest_revision": None, "preview_url": None}


def test_failed_checks_and_bad_folders(studio):
    studio._community = FakeCommunity(upload={"ok": False, "errors": ["no portrait.svg"], "warnings": []})
    up = m.studio_template_upload(folder="studio-templates/tips_card")
    assert up["ok"] and not up["uploaded"] and up["errors"] == ["no portrait.svg"]
    for folder in ("studio-templates", "../elsewhere"):
        out = m.studio_template_check(folder=folder)
        assert not out["ok"] and out["error"]["code"] == "BAD_PACKAGE"


def _client(handler, **settings):
    api = CommunityApi(Settings(community_url="https://community.test", **settings), token="tok")
    api._client = httpx.Client(base_url=api._client.base_url, headers=api._client.headers, transport=httpx.MockTransport(handler))
    return api


def test_community_client_url_firm_header_and_check_errors():
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.params.get("check") == "1":
            return httpx.Response(400, json={"error": {"code": "invalid_package", "message": "The package has problems to fix.",
                                                       "fields": {"package": ["bad"]}}, "checks": {"errors": ["bad"], "warnings": []}})
        return httpx.Response(201, json={"slug": "a--u1", "checks": {"errors": [], "warnings": ["w"]}})

    api = _client(handler, community_firm="maple")
    assert api.template_check(b"zip") == {"ok": False, "errors": ["bad"], "warnings": []}
    out = api.template_upload(b"zip")
    assert out["ok"] and out["slug"] == "a--u1" and out["warnings"] == ["w"]
    assert str(seen[0].url).startswith(f"https://community.test{MY_TEMPLATES}")
    assert seen[0].headers["X-Firm"] == "maple" and seen[0].headers["Authorization"] == "Bearer tok"


def test_community_errors_carry_the_server_message():
    from uapply_agent.api import ApiError

    api = _client(lambda r: httpx.Response(403, json={"error": {"code": "permission_denied", "message": "Only the RCIC can add your own templates."}}))
    with pytest.raises(ApiError, match="Only the RCIC"):
        api.my_templates()


def test_studio_template_command_is_generated():
    files = playbook.plugin_files("1.0.0")
    assert "studio_template_guide" in files["commands/studio-template.md"]
    assert "uapply-studio-template/SKILL.md" in playbook.codex_skill_files()


@pytest.mark.parametrize("body, message", [
    ({"error": "Survey not found"}, "Survey not found"),
    ({"error": {"message": "Only the RCIC can add your own templates."}}, "Only the RCIC"),
    ({"message": "Not ready", "error": "x"}, "Not ready"),
    ({"detail": "Authentication credentials were not provided."}, "Authentication"),
    ("<html>", "<html>"),
])
def test_api_error_messages(body, message):
    from uapply_agent.api import ApiError

    assert message in str(ApiError(404, body))

