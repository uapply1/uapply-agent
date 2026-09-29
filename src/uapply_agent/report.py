"""End-of-run report: the dashboard's Submit step, for the chat and the client folder.

Files are saved into a visible "uApply output" folder (dashboard links don't expire; presigned
download links would within the hour). Acting on the case — resolving AI Check items, starting the
online portal — stays in the dashboard, reached through step deep links.
"""
from __future__ import annotations

import logging
import re
import zipfile
from pathlib import Path

from .api import ApiError, UApplyApi
from .cases import analysis_outcome
from .config import Settings
from .folder import OUTPUT_DIR, WorkingFolder
from .util import write_text_atomic

logger = logging.getLogger(__name__)



def status_label(r: dict) -> str:
    """As the dashboard's case list (use-survey-status-handler.ts)."""
    auto, ana = r.get("automation_status"), r.get("analyzing_status")
    if auto == "none":
        return "Not started"
    if auto == "generating":
        return "Generating archives"
    if auto == "archive":
        return "Archived"
    if ana == "none":
        return "Processed"
    if ana in ("processing", "analyzing"):
        return "Analyzing"
    if auto in ("started", "filling"):
        return "Filling forms"
    if auto == "completed":
        return "Completed"
    if ana in ("completed", "failed"):
        return "Analyzed"
    return "Draft"


def links(settings: Settings, survey_id: str) -> dict:
    base = f"{settings.dashboard_url}/rcic/survey/{survey_id}"
    return {"case": base, "ai_check": f"{base}?step=ai_check", "submit": f"{base}?step=submit",
            "online_portal": f"{base}?step=submit&portal=1"}


def _safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", name).strip() or "case"


def download_package(api: UApplyApi, folder: WorkingFolder, r: dict) -> dict:
    """Final package zip into "uApply output/", with the filled forms unpacked beside it."""
    out = folder.root / OUTPUT_DIR
    zpath = api.download_submit_package(r["survey_id"], out / f"{_safe(r['name'])}_final_package.zip")
    forms_dir = out / "Forms"
    forms = []
    with zipfile.ZipFile(zpath) as z:
        for info in z.infolist():
            parts = Path(info.filename).parts
            if info.is_dir() or len(parts) < 3 or parts[:2] != ("To Submit", "Forms") or ".." in parts:
                continue
            dest = forms_dir / Path(*parts[2:])
            dest.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(dest, "wb") as f:
                f.write(src.read())
            forms.append(str(dest.relative_to(folder.root)))
    return {"package": str(zpath.relative_to(folder.root)), "forms": forms}


def render(r: dict, lk: dict, files: dict) -> str:
    L = [f"# uApply report — {r['name']}", "",
         f"{r.get('application_type') or 'Case'} · **{status_label(r)}**", ""]
    analysis = r.get("analysis") or {}
    if analysis.get("failed"):
        L += [f"**Analysis incomplete:** {len(analysis['failed'])} of {analysis.get('sections')} sections failed, so "
              "their fields are empty and the forms are filled without them. Run the analysis again "
              "(`/uapply:run`) before submitting.", ""]
        L += [f"- {f['section']}: {f['error']}" for f in analysis["failed"][:10]] + [""]
    ai = r.get("ai_check") or {}
    if ai.get("conflict") or ai.get("doubtful"):
        L += [f"**Review before submitting:** AI Check has {ai.get('conflict', 0)} conflict(s) and "
              f"{ai.get('doubtful', 0)} doubtful field(s) ({ai.get('missing', 0)} missing). "
              f"[Open AI Check]({lk['ai_check']})", ""]
        worst = sorted((ai.get("pages") or {}).items(), key=lambda kv: (-kv[1]["conflict"], -kv[1]["doubtful"]))[:5]
        if worst:
            L += ["Most to review: " + " · ".join(f"{p} ({c['conflict']} conflict, {c['doubtful']} doubtful)"
                                               for p, c in worst), ""]

    docs = r.get("documents") or {}
    total = sum(docs.values())
    L += ["## Documents", f"{docs.get('completed', 0)} of {total} processed"
          + (f", {docs.get('failed', 0)} failed" if docs.get("failed") else "")]
    for f in r.get("failures") or []:
        L.append(f"- failed: {f['file_name']} — {(f.get('error') or '')[:160]}")
    L.append("")

    if r.get("imm_pdfs"):
        L += ["## IMM forms", "| Form | Applicant | Status | Filled |", "|---|---|---|---|"]
        for p in r["imm_pdfs"]:
            filled = f"{p['success_rate']:.0f}%" if p["status"] == "success" else (p.get("error") or "")[:80]
            L.append(f"| {p['name']} | {p.get('applicant') or ''} | {p['status']} | {filled} |")
        L.append("")
    sheets = [e for e in r.get("extra_files") or [] if e["category"] == "additional_sheet"]
    archives = r.get("archives") or []
    compressed = sum(1 for a in archives if a.get("compressing") == "completed")
    L += ["## Package", f"- Archives: {len(archives)} ({compressed} compressed)",
          f"- Additional sheets: {len(sheets)}",
          f"- Final package files: {len(r.get('submit_files') or [])}"]
    if files.get("package"):
        L.append(f"- Saved: `{files['package']}`" + (f", forms in `{OUTPUT_DIR}/Forms/`" if files.get("forms") else ""))
    L += ["", "## Next", f"- Review and submit: [Submit step]({lk['submit']})"]
    portal = r.get("online_portal") or {}
    if portal.get("ready"):
        L.append(f"- Online portal: [Start online portal]({lk['online_portal']}) — opens the dashboard, one click "
                 "sends the form data to the uApply Chrome extension and opens the IRCC sign-in")
    elif portal.get("eligible"):
        L.append("- Online portal: available once auto-fill has produced the online form data")
    return "\n".join(L) + "\n"


def build(api: UApplyApi, folder: WorkingFolder, settings: Settings, download: bool = True) -> dict:
    r = api.report(folder.survey_id)
    r["analysis"] = analysis_outcome(api.survey(folder.survey_id))
    lk = links(settings, folder.survey_id)
    files = {}
    if download:
        try:
            files = download_package(api, folder, r)
        except (ApiError, OSError, zipfile.BadZipFile) as e:
            logger.warning("final package download failed: %s", e)
            files = {"error": f"could not download the final package: {e}"[:300]}
    md = render(r, lk, files)
    out = folder.root / OUTPUT_DIR
    out.mkdir(parents=True, exist_ok=True)
    write_text_atomic(out / "report.md", md)
    ai = r.get("ai_check") or {}
    return {"report_markdown": md, "report_path": f"{OUTPUT_DIR}/report.md", "links": lk, "files": files,
            "status": status_label(r), "ai_check": {k: ai.get(k) for k in ("conflict", "doubtful", "missing")},
            "analysis": r["analysis"],
            "online_portal": r.get("online_portal")}
