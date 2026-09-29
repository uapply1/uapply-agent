"""Filing a fetched transcript on the case: a text PDF uploaded as an Agent Survey document."""
from __future__ import annotations

import logging
from pathlib import Path

from ..api import UApplyApi
from ..folder import WorkingFolder
from ..util import sha256_of
from .base import ChatError
from .render import transcript_to_pdf
from .store import ChatStore

logger = logging.getLogger(__name__)


def upload_transcript(api: UApplyApi, folder: WorkingFolder, md_path: Path) -> dict:
    """Render the transcript to PDF and upload it; a transcript already filed with the same content
    is not uploaded again."""
    store = ChatStore(folder)
    row = store.row(md_path)
    md_sha = sha256_of(md_path)
    if existing := store.filed(md_path, md_sha):
        store.clear_pending(md_path)
        return {"document_id": existing, "pdf": row.get("pdf"), "already_filed": True}
    pdf = md_path.with_suffix(".pdf")
    transcript_to_pdf(md_path, pdf, f"Chat history: {row.get('contact', md_path.stem)}", [
        f"Source: {row.get('source', 'chat')} (self-reported, unverified)",
        f"Period: {row.get('from', '?')} to {row.get('to', '?')}",
        f"Fetched: {row.get('fetched_at', '')}",
        f"Case: {folder.case.get('name', '')} ({folder.survey_id})",
    ])
    dtype = api.agent_survey_type()
    # Filed in the type's default archive, next to the RCIC's own uploads of that type.
    docs = api.bulk_upload(folder.survey_id, "other", dtype["id"], [pdf], archive_name=dtype.get("name") or "")
    doc = next((d for d in docs if isinstance(d, dict) and d.get("id")), None)
    if not doc:
        raise ChatError("UPLOAD_FAILED", "empty upload response for the transcript PDF")
    rel_pdf = str(pdf.relative_to(folder.root))
    folder.record_upload(sha256_of(pdf), rel_pdf, doc["id"], "principal", uploaded_path=rel_pdf)
    store.mark_uploaded(md_path, doc["id"], md_sha, rel_pdf)
    store.clear_pending(md_path)
    return {"document_id": doc["id"], "pdf": rel_pdf}


def upload_pending(api: UApplyApi, folder: WorkingFolder) -> list[dict]:
    """Upload every transcript fetched before the folder had a case; failures are reported per file
    and stay queued for `chat_upload`."""
    out = []
    for path in ChatStore(folder).pending_uploads():
        try:
            out.append({"path": str(path), **upload_transcript(api, folder, path)})
        except Exception as e:  # one bad transcript must not block the others
            logger.warning("filing %s failed", path, exc_info=True)
            out.append({"path": str(path), "error": str(e)[:200]})
    return out
