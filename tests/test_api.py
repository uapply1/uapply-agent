import httpx

from uapply_agent.api import NO_AGENT_API_HINT, ApiError, UApplyApi
from uapply_agent.config import Settings


def client(handler):
    api = UApplyApi(Settings(backend_url="https://api.test"), token="t")
    api._client = httpx.Client(base_url="https://api.test", transport=httpx.MockTransport(handler))
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
