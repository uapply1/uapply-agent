"""Typed client for the uApply backend. Every call the agent makes goes through here."""
from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any, Iterable, Optional

import httpx

from .config import Credentials, Settings


class ApiError(RuntimeError):
    def __init__(self, status: int, body: Any, hint: str = ""):
        self.status, self.body, self.hint = status, body, hint
        msg = body.get("message") if isinstance(body, dict) else str(body)[:300]
        super().__init__(f"HTTP {status}: {msg}")


class UApplyApi:
    def __init__(self, settings: Optional[Settings] = None, token: Optional[str] = None, timeout: float = 90.0):
        self.settings = settings or Settings.load()
        self.token = token or Credentials.get_token()
        self._client = httpx.Client(base_url=self.settings.backend_url, timeout=timeout,
                                    headers=self._headers())

    def _headers(self) -> dict:
        h = {"Accept": "application/json", "User-Agent": "uapply-agent/0.1"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    @property
    def logged_in(self) -> bool:
        return bool(self.token)

    def _refresh_token(self) -> bool:
        """Exchange the stored refresh token once; False when there is nothing to refresh with."""
        s = self.settings
        try:
            import keyring
            refresh = keyring.get_password("uapply-agent", "refresh_token")
        except Exception:
            refresh = None
        if not (refresh and s.auth0_domain and s.auth0_client_id):
            return False
        try:
            r = httpx.post(f"https://{s.auth0_domain}/oauth/token", timeout=30, data={
                "grant_type": "refresh_token", "client_id": s.auth0_client_id, "refresh_token": refresh})
            if r.status_code != 200:
                return False
            body = r.json()
            Credentials.set_token(body["access_token"], body.get("refresh_token") or refresh)
            self.token = body["access_token"]
            self._client.headers.update(self._headers())
            return True
        except Exception:
            return False

    def _req(self, method: str, path: str, _retry: bool = True, **kw) -> Any:
        r = self._client.request(method, path, **kw)
        if r.status_code == 401 and _retry and self._refresh_token():
            return self._req(method, path, _retry=False, **kw)
        if r.status_code >= 400:
            try:
                body = r.json()
            except Exception:
                body = r.text
            raise ApiError(r.status_code, body, body.get("hint", "") if isinstance(body, dict) else "")
        if r.headers.get("content-type", "").startswith("application/json"):
            return r.json()
        return r.text

    # ---- surveys / documents (existing endpoints) ----

    def survey(self, survey_id: str) -> dict:
        return self._req("GET", f"/api/survey/surveys/{survey_id}/")

    def document_types(self) -> list:
        return self._req("GET", "/api/survey/document-types/")

    def documents(self, survey_id: str) -> list:
        data = self._req("GET", "/api/survey/documents/", params={"survey": survey_id})
        rows = data.get("results", data) if isinstance(data, dict) else data
        # The backend filters by ?survey=; keep the client-side guard so an older backend
        # can never leak another case's documents into the chat.
        return [d for d in rows if str(d.get("survey", survey_id)) == str(survey_id)]

    def application_types(self) -> list:
        data = self._req("GET", "/api/survey/application-types/")
        rows = data.get("results", data) if isinstance(data, dict) else data
        return [{"id": r.get("id"), "code": r.get("code"), "name": r.get("name"), "program": r.get("program"),
                 "visa_type": r.get("visa_type"), "visa_location": r.get("visa_location"),
                 "applicant_type": r.get("applicant_type")} for r in rows if r.get("is_active", True)]

    def agent_survey_type_id(self) -> str:
        for t in self.document_types():
            if t.get("file_name") == "agent_survey":
                return t["id"]
        raise ApiError(404, {"message": "agent_survey document type not found"})

    def create_survey(self, name: str, application_type_id: str, team_id: Optional[str] = None,
                      llm_mode: str = "local_agent") -> dict:
        """Creates the case and charges the RCIC's account — only after explicit RCIC confirmation."""
        body = {"name": name, "application_type_id": application_type_id, "llm_mode": llm_mode}
        if team_id:
            body["team_id"] = team_id
        return self._req("POST", "/api/survey/surveys/", json=body)

    def survey_document_types(self, survey_id: str) -> list:
        """The types attached to this case (the only ones bulk_upload accepts), with category."""
        return self.survey(survey_id).get("document_types") or []

    def bulk_upload(self, survey_id: str, document_category: str, document_type_id: str,
                    paths: Iterable[Path], archive_name: str = "") -> list:
        files = []
        handles = []
        try:
            for p in paths:
                f = open(p, "rb")
                handles.append(f)
                files.append(("files", (p.name, f, mimetypes.guess_type(p.name)[0] or "application/octet-stream")))
            return self._req("POST", "/api/survey/documents/bulk_upload/",
                             data={"survey_id": survey_id, "document_category": document_category,
                                   "document_type_id": document_type_id, "archive_name": archive_name},
                             files=files, timeout=600)
        finally:
            for f in handles:
                f.close()

    def document_download_url(self, document_id: str) -> str:
        return self._req("GET", f"/api/survey/documents/{document_id}/download/")["url"]

    def download_to(self, url: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with httpx.stream("GET", url, timeout=300, follow_redirects=True) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
        return dest

    def start_document_processing(self, survey_id: str, document_id: str) -> dict:
        return self._req("POST", f"/api/ai-parse/surveys/{survey_id}/start_document_processing/",
                         json={"document_id": document_id})

    # ---- agent endpoints ----

    def agent_status(self, survey_id: str) -> dict:
        return self._req("GET", f"/api/ai-parse/agent/surveys/{survey_id}/status/")

    def agent_wait(self, survey_id: str, stage: str, timeout_s: int) -> dict:
        return self._req("GET", f"/api/ai-parse/agent/surveys/{survey_id}/wait/",
                         params={"stage": stage, "timeout": timeout_s}, timeout=timeout_s + 30)

    def set_llm_mode(self, survey_id: str, mode: str) -> dict:
        return self._req("POST", f"/api/ai-parse/agent/surveys/{survey_id}/llm_mode/", json={"llm_mode": mode})

    def pull_tasks(self, survey_ids: list, n: int, session_id: str, runtime: str,
                   kinds: Optional[list] = None, lease_s: Optional[int] = None) -> list:
        body = {"survey_ids": survey_ids, "n": n, "session_id": session_id, "runtime": runtime}
        if kinds:
            body["kinds"] = kinds
        if lease_s:
            body["lease_s"] = lease_s
        return self._req("POST", "/api/ai-parse/agent/tasks/pull/", json=body)["tasks"]

    def submit_result(self, task_id: str, result: dict, model: str, runtime: str,
                      usage: Optional[dict], session_id: str) -> dict:
        return self._req("POST", f"/api/ai-parse/agent/tasks/{task_id}/result/",
                         json={"result": result, "model": model or "", "runtime": runtime,
                               "usage": usage or {}, "session_id": session_id})

    def release_task(self, task_id: str, reason: str) -> dict:
        return self._req("POST", f"/api/ai-parse/agent/tasks/{task_id}/release/", json={"reason": reason[:500]})

    def task_stats(self, survey_id: str) -> dict:
        return self._req("GET", "/api/ai-parse/agent/tasks/stats/", params={"survey": survey_id})["stats"]
