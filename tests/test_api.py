import httpx

from uapply_agent.api import NO_AGENT_API_HINT, ApiError, UApplyApi
from uapply_agent.config import Settings


def client(handler):
    api = UApplyApi(Settings(backend_url="https://api.test"), token="t")
    api._client = httpx.Client(base_url="https://api.test", transport=httpx.MockTransport(handler),
                               headers=api._headers())
    return api


def test_agent_404_from_old_backend_is_explained_and_cached():
    calls = []

    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(404, text="<html>Not Found</html>")
    api = client(handler)
    try:
        api.agent_status("s-1")
        assert False, "expected ApiError"
    except ApiError as e:
        assert e.status == 404 and e.hint == NO_AGENT_API_HINT
    assert api.agent_api_available() is False
    assert calls == ["/api/ai-parse/agent/surveys/s-1/status/"]  # cached: no second probe


def test_documents_come_from_the_survey_detail():
    def handler(req):
        assert req.url.path == "/api/survey/surveys/s-1/"
        return httpx.Response(200, json={"id": "s-1", "documents": [{"id": "d1"}]}, headers={"content-type": "application/json"})
    assert client(handler).documents("s-1") == [{"id": "d1"}]


def test_agent_api_available_probe_on_branch_backend():
    def handler(req):
        assert req.url.path == "/api/ai-parse/agent/tasks/stats/"
        return httpx.Response(200, json={}, headers={"content-type": "application/json"})
    assert client(handler).agent_api_available() is True


def test_network_errors_become_api_errors():
    def handler(req):
        raise httpx.ConnectError("connection refused")
    try:
        client(handler).survey("s-1")
        assert False, "expected ApiError"
    except ApiError as e:
        assert e.status == 0 and "ConnectError" in str(e)


def test_concurrent_401s_refresh_the_token_once(monkeypatch):
    import threading

    from uapply_agent import api as api_mod
    posts = []

    def fake_post(url, timeout, data):
        posts.append(data["refresh_token"])
        return httpx.Response(200, json={"access_token": "new", "refresh_token": "r2"})
    monkeypatch.setattr(api_mod.httpx, "post", fake_post)
    monkeypatch.setattr(api_mod.Credentials, "get_refresh_token", classmethod(lambda cls: "r1"))
    monkeypatch.setattr(api_mod.Credentials, "set_token", classmethod(lambda cls, t, r=None: "keyring"))

    def handler(req):
        ok = req.headers["authorization"] == "Bearer new"
        return httpx.Response(200 if ok else 401, json={}, headers={"content-type": "application/json"})
    api = client(handler)
    api.settings.auth0_domain, api.settings.auth0_client_id = "auth.test", "cid"
    threads = [threading.Thread(target=api.survey, args=("s-1",)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert posts == ["r1"] and api.token == "new"


def test_interrupted_download_leaves_no_file(tmp_path, monkeypatch):
    from uapply_agent import api as api_mod

    class Broken:
        def __enter__(self):
            raise httpx.ReadTimeout("slow")

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(api_mod.httpx, "stream", lambda *a, **k: Broken())
    dest = tmp_path / "scan.pdf"
    try:
        client(lambda r: httpx.Response(200)).download_to("https://s3.test/x", dest)
        assert False, "expected ApiError"
    except ApiError:
        pass
    assert not dest.exists() and not (tmp_path / "scan.pdf.part").exists()


def test_a_pasted_token_drops_the_previous_refresh_token(tmp_path, monkeypatch):
    from uapply_agent.config import Credentials
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("UAPPLY_TOKEN", raising=False)
    import keyring
    monkeypatch.setattr(keyring, "set_password", lambda *a: (_ for _ in ()).throw(RuntimeError("no backend")))
    monkeypatch.setattr(keyring, "get_password", lambda *a: None)
    Credentials.set_token("a1", "r1")
    assert Credentials.get_refresh_token() == "r1"
    Credentials.set_token("pasted")
    assert Credentials.get_token() == "pasted" and Credentials.get_refresh_token() is None
    Credentials.clear()
    assert Credentials.get_token() is None
