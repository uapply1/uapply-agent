"""Typed client for the uApply backend. Every call the agent makes goes through here."""
from __future__ import annotations

import logging
import mimetypes
import threading
from pathlib import Path
from typing import Any, Iterable, Optional

import httpx

from . import __version__
from .config import Credentials, Settings


logger = logging.getLogger(__name__)

USER_AGENT = f"uapply-agent/{__version__}"
AGENT_PREFIX = "/api/ai-parse/agent/"
NO_AGENT_API_HINT = ("the backend has no local-agent API (branch not deployed); the case runs in server mode "
                     "and its documents are processed by uApply's server models")


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
        self._agent_api: Optional[bool] = None
        self._refresh_lock = threading.Lock()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "UApplyApi":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _headers(self) -> dict:
        h = {"Accept": "application/json", "User-Agent": USER_AGENT}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    @property
    def logged_in(self) -> bool:
        return bool(self.token)

    def _refresh_token(self, rejected: Optional[str]) -> bool:
        """Exchange the stored refresh token for a new access token. Serialised: executor workers
        hit 401 together, and Auth0 revokes the whole token family when a rotated token is reused."""
        with self._refresh_lock:
            if self.token and self.token != rejected:
                return True                     # another thread already refreshed
            s = self.settings
            refresh = Credentials.get_refresh_token()
            if not (refresh and s.auth0_domain and s.auth0_client_id):
                return False
            try:
                r = httpx.post(f"https://{s.auth0_domain}/oauth/token", timeout=30, data={
                    "grant_type": "refresh_token", "client_id": s.auth0_client_id, "refresh_token": refresh})
            except httpx.HTTPError as e:
                logger.warning("token refresh failed: %s", e)
                return False
            if r.status_code != 200:
                logger.warning("token refresh rejected (HTTP %s)", r.status_code)
                return False
            body = r.json()
            Credentials.set_token(body["access_token"], body.get("refresh_token") or refresh)
            self.token = body["access_token"]
            self._client.headers.update(self._headers())
            return True

    def _req(self, method: str, path: str, _retry: bool = True, **kw) -> Any:
        sent_token = self.token
        try:
            r = self._client.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise ApiError(0, f"{type(e).__name__}: {e}", "check the network connection to the uApply backend") from e
        if r.status_code == 401 and _retry and self._refresh_token(sent_token):
            return self._req(method, path, _retry=False, **kw)
        if r.status_code >= 400:
            try:
                body = r.json()
            except ValueError:
                body = r.text
            hint = body.get("hint", "") if isinstance(body, dict) else ""
            if r.status_code == 404 and path.startswith(AGENT_PREFIX) and not isinstance(body, dict):
                self._agent_api = False
                body, hint = {"message": "this backend has no local-agent API"}, NO_AGENT_API_HINT
            raise ApiError(r.status_code, body, hint)
        if r.headers.get("content-type", "").startswith("application/json"):
            return r.json()
        return r.text

    def agent_api_available(self) -> bool:
        """Does this backend serve /api/ai-parse/agent/? Production without the branch answers an HTML 404."""
        if self._agent_api is None:
            try:
                self._req("GET", f"{AGENT_PREFIX}tasks/stats/")
                self._agent_api = True
            except ApiError as e:
                self._agent_api = e.status != 404
        return self._agent_api

    # ---- surveys / documents (existing endpoints) ----

    def survey(self, survey_id: str) -> dict:
        return self._req("GET", f"/api/survey/surveys/{survey_id}/")

    def document_types(self) -> list:
        return self._req("GET", "/api/survey/document-types/")

    def documents(self, survey_id: str) -> list:
        """The case's documents from the survey detail: scoped by construction on every backend.
        (The flat /documents/ list only honours ?survey= on branch backends and rows carry no survey id.)"""
        return self.survey(survey_id).get("documents") or []

    def application_types(self) -> list:
        data = self._req("GET", "/api/survey/application-types/")
        rows = data.get("results", data) if isinstance(data, dict) else data
        out = []
        for r in rows:
            if not r.get("is_active", True):
                continue
            pdfs = r.get("imm_pdf_types") or []
            default_forms = [p["id"] for p in pdfs if isinstance(p, dict) and p.get("id")
                             and p.get("confirmed", True) and p.get("role") in (None, "main", "required")]
            out.append({"id": r.get("id"), "code": r.get("code"), "name": r.get("name"), "program": r.get("program"),
                        "visa_type": r.get("visa_type"), "visa_location": r.get("visa_location"),
                        "applicant_type": r.get("applicant_type"), "default_imm_pdf_types": default_forms})
        return out

    def teams(self) -> list:
        data = self._req("GET", "/api/teams/")
        rows = data.get("results", data) if isinstance(data, dict) else data
        return [{"id": t.get("id"), "name": t.get("name")} for t in rows if isinstance(t, dict) and t.get("id")]

    def agent_survey_type(self) -> dict:
        for t in self.document_types():
            if t.get("file_name") == "agent_survey":
                return t
        raise ApiError(404, {"message": "agent_survey document type not found"})

    def agent_survey_type_id(self) -> str:
        return self.agent_survey_type()["id"]

    def create_survey(self, name: str, application_type_id: str, team_id: Optional[str] = None,
                      llm_mode: str = "local_agent", imm_pdf_types: Optional[list] = None) -> dict:
        """Creates the case and charges the RCIC's account — only after explicit RCIC confirmation."""
        body = {"name": name, "application_type_id": application_type_id, "llm_mode": llm_mode,
                "imm_pdf_types": imm_pdf_types or []}
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
        """Fetch a presigned storage URL. Not through `self._client`: the bearer token must not be
        sent to the storage host. Written to `<dest>.part` first, so an interrupted download is
        never mistaken for the file."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        try:
            with httpx.stream("GET", url, timeout=300, follow_redirects=True) as r:
                r.raise_for_status()
                with open(part, "wb") as f:
                    for chunk in r.iter_bytes():
                        f.write(chunk)
        except httpx.HTTPError as e:
            part.unlink(missing_ok=True)
            raise ApiError(getattr(getattr(e, "response", None), "status_code", 0), f"download failed: {e}") from e
        part.replace(dest)
        return dest

    def start_analysis(self, survey_id: str) -> dict:
        return self._req("POST", f"/api/ai-parse/surveys/{survey_id}/start_analysis/", json={})

    # ---- after analysis: archives + compression, then IMM PDF auto-fill ----

    def generate_archive_files(self, survey_id: str) -> dict:
        """The dashboard's Confirm: merge each archive into a PDF and queue compression (no AI)."""
        return self._req("POST", f"/api/survey/surveys/{survey_id}/generate_archive_files/", json={}, timeout=600)

    def start_auto_filling(self, survey_id: str) -> dict:
        """Platform auto-fill (uApply's Windows filler, no AI)."""
        return self._req("POST", f"/api/survey/surveys/{survey_id}/start_auto_filling/", json={})

    def autofill_claim(self, survey_id: str) -> dict:
        return self._req("POST", f"{AGENT_PREFIX}surveys/{survey_id}/autofill/claim/", json={}, timeout=300)

    def autofill_result(self, survey_id: str, imm_pdf_id: str, pdf: Optional[Path] = None, error: str = "",
                        success_rate: Optional[float] = None, errors: Optional[list] = None,
                        survey_values_updated_at: Optional[str] = None) -> dict:
        import json
        data = {"error": error, "errors": json.dumps(errors or []),
                "survey_values_updated_at": survey_values_updated_at or ""}
        if success_rate is not None:
            data["success_rate"] = str(success_rate)
        path = f"{AGENT_PREFIX}surveys/{survey_id}/autofill/{imm_pdf_id}/result/"
        if pdf is None:
            return self._req("POST", path, data=data, timeout=120)
        with open(pdf, "rb") as f:
            return self._req("POST", path, data=data, files={"file": (pdf.name, f, "application/pdf")}, timeout=300)

    def report(self, survey_id: str) -> dict:
        return self._req("GET", f"{AGENT_PREFIX}surveys/{survey_id}/report/", timeout=180)

    def download_submit_package(self, survey_id: str, dest: Path) -> Path:
        """The dashboard's "Build Final Package" zip, streamed to `dest`."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        path = f"/api/survey/surveys/{survey_id}/download_zip_submit_files/"
        for attempt in range(2):
            with self._client.stream("POST", path, json={}, timeout=httpx.Timeout(60, read=900)) as r:
                if r.status_code == 401 and attempt == 0 and self._refresh_token():
                    continue
                if r.status_code >= 400:
                    r.read()
                    raise ApiError(r.status_code, r.text[:300])
                with open(tmp, "wb") as f:
                    for chunk in r.iter_bytes():
                        f.write(chunk)
            tmp.replace(dest)
            return dest
        raise ApiError(401, "not logged in", "log in with `uapply-agent login`")

    def autofill_finish(self, survey_id: str) -> dict:
        return self._req("POST", f"{AGENT_PREFIX}surveys/{survey_id}/autofill/finish/", json={})

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
