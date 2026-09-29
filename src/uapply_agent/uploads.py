"""Uploading folder files to the case, without duplicating documents already on the server."""
from __future__ import annotations

from pathlib import Path

from .api import ApiError, UApplyApi
from .constants import HEIC_EXTENSIONS
from .context import ToolError
from .folder import WorkingFolder
from .local_ops import heic_to_jpeg

AGENT_SURVEY_FILE_NAME = "agent_survey"
AGENT_SURVEY_USE = ("IMM forms only: filled intake, draft or previous IMM forms (e.g. IMM 5709, 5257, 5645, 5406), "
                    "including screenshots or scans. Not for other unmatched documents: ask the RCIC.")
DOCUMENT_CATEGORIES = ("identity", "school", "financial", "spouse", "parent", "child", "language",
                       "employment", "proof_of_capability", "immigration", "inviter", "education", "other")


def document_types(api: UApplyApi, survey_id: str, query: str = "") -> list[dict]:
    """The case's document types, plus the generic Agent Survey type for IMM forms."""
    rows = [{"id": t["id"], "name": t["name"], "file_name": t.get("file_name"), "category": t.get("category"),
             "requirement": t.get("requirement"), "can_process": t.get("can_process")}
            for t in api.survey_document_types(survey_id)]
    if not any(r["file_name"] == AGENT_SURVEY_FILE_NAME for r in rows):
        try:
            g = api.agent_survey_type()
            rows.append({"id": g["id"], "name": g.get("name"), "file_name": AGENT_SURVEY_FILE_NAME,
                         "category": "other", "requirement": None, "can_process": True, "generic": True,
                         "use_for": AGENT_SURVEY_USE})
        except ApiError:
            pass                        # the backend has no Agent Survey type; the case types still apply
    q = query.lower()
    return [r for r in rows if not q or q in f"{r['name']} {r['file_name']}".lower()]


def _resolve_type(api: UApplyApi, survey_id: str, document_type_id: str) -> dict:
    match = [t for t in api.survey_document_types(survey_id) if str(t.get("id")) == document_type_id]
    if match:
        return match[0]
    # Not on the case: the backend accepts only the generic Agent Survey type.
    generic = api.agent_survey_type()
    if str(generic.get("id")) != document_type_id:
        raise ToolError("UNKNOWN_TYPE", f"document type {document_type_id} is not on this case",
                        "pick an id from list_document_types")
    return {**generic, "category": "other"}


def already_on_server(local: Path, size: int, document_type_id: str, server_docs: list, claimed: set):
    """A case document of this type with this file's name (HEIC arrives as .png/.jpg) and size."""
    converted = local.suffix.lower() in HEIC_EXTENSIONS
    for d in server_docs:
        if d.get("id") in claimed or str(d.get("document_type_id")) != str(document_type_id):
            continue
        name = str(d.get("file_name") or "")
        same_name = name == local.name or (converted and Path(name).stem == local.stem)
        if same_name and (converted or not d.get("size") or int(d["size"]) == int(size)):
            return d
    return None


def sync(api: UApplyApi, folder: WorkingFolder, survey_id: str, document_type_id: str, document_category: str = "",
         paths: list[str] | None = None, applicant: str = "principal", archive_name: str = "") -> dict:
    """Upload the folder's unmanifested files (or `paths`) under one document type."""
    dtype = _resolve_type(api, survey_id, document_type_id)
    category = document_category or dtype.get("category") or "other"
    if category not in DOCUMENT_CATEGORIES:
        raise ToolError("BAD_CATEGORY", f"document_category must be one of {DOCUMENT_CATEGORIES}")
    archive = archive_name or dtype.get("name") or ""
    scanned = folder.scan(include_manifested=True)
    known = {f.path for f in scanned}
    unknown = [p for p in (paths or []) if p not in known]
    chosen = [f for f in scanned if not paths or f.path in paths]
    uploaded, failed, matched = [], [{"path": p, "reason": "not a supported file in the folder"} for p in unknown], []
    server_docs: list | None = None
    claimed = {e.get("document_id") for e in folder.manifest.get("files", {}).values()}
    # One request per file: the response maps to that file with no name matching, so HEIC conversions
    # and duplicate names in different subfolders cannot mix up document ids.
    for f in (f for f in chosen if not f.manifested):
        try:
            local = folder.resolve(f.path)
            if server_docs is None:
                server_docs = [d for d in api.documents(survey_id) if not d.get("old_doc_id")]
            existing = already_on_server(local, f.size, document_type_id, server_docs, claimed)
            if existing:
                # e.g. the upload went through but the local record was lost: record it, do not duplicate
                claimed.add(existing["id"])
                folder.record_upload(f.sha256, f.path, existing["id"], applicant)
                matched.append({"path": f.path, "document_id": existing["id"]})
                continue
            send = heic_to_jpeg(local, folder.cache) if local.suffix.lower() in HEIC_EXTENSIONS else local
            docs = api.bulk_upload(survey_id, category, document_type_id, [send], archive_name=archive)
            doc = next((d for d in docs if isinstance(d, dict) and d.get("id")), None)
            if not doc:
                failed.append({"path": f.path, "reason": "empty upload response"})
                continue
            folder.record_upload(f.sha256, f.path, doc["id"], applicant,
                                 uploaded_path=str(send.relative_to(folder.root)) if send != local else None)
            uploaded.append({"path": f.path, "document_id": doc["id"], "file_name": doc.get("file_name")})
        except (ApiError, OSError, ValueError) as e:
            failed.append({"path": f.path, "reason": str(e)[:300]})
    return {"uploaded": len(uploaded), "documents": uploaded, "skipped": [f.path for f in chosen if f.manifested],
            "failed": failed, "already_on_server": matched}
