"""After analysis: fill the case's IMM PDFs with the RCIC's Acrobat Pro, or hand them to uApply's
platform filler when Acrobat Pro isn't available. Time-boxed and resumable across tool calls
(state in .uapply/autofill.json), like run_tasks."""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path

from . import acrobat
from .api import UApplyApi
from .folder import WorkingFolder

logger = logging.getLogger(__name__)

STATE = "autofill.json"
MAX_REFILLS = 1          # re-claims after survey values changed mid-fill


class AutoFiller:
    def __init__(self, api: UApplyApi, folder: WorkingFolder, detect: Callable = acrobat.detect,
                 fill: Callable = acrobat.fill):
        self.api, self.folder, self.detect, self.fill = api, folder, detect, fill

    def _state(self) -> dict:
        s = self.folder.read_state(STATE, {})
        return s if s.get("survey_id") == self.folder.survey_id else {}

    def _save(self, s: dict) -> None:
        self.folder.write_state(STATE, s)

    def _clear(self) -> None:
        self.folder.remove_state(STATE)

    def _to_platform(self, reason: str) -> dict:
        self.api.start_auto_filling(self.folder.survey_id)
        self._save({"survey_id": self.folder.survey_id, "mode": "platform", "reason": reason})
        return {"mode": "platform", "reason": reason, "remaining": 0,
                "next": "uApply's platform fills the forms: loop wait_for_stage('filling') until done"}

    def _claim(self, restarts: int = 0) -> dict:
        claim = self.api.autofill_claim(self.folder.survey_id)
        s = {"survey_id": self.folder.survey_id, "mode": "local", "forms": claim.get("forms", []),
             "skipped": claim.get("skipped", []), "done": {}, "restarts": restarts}
        self._save(s)
        return s

    def run(self, budget_s: int = 240) -> dict:
        s = self._state()
        if s.get("mode") == "platform":
            status = self.api.agent_status(self.folder.survey_id).get("automation_status")
            if status in ("completed", "failed"):
                self._clear()                   # done on uApply; a later run starts afresh
                return {"mode": "platform", "remaining": 0, "automation_status": status}
            return {"mode": "platform", "reason": s.get("reason"), "remaining": 0,
                    "next": "loop wait_for_stage('filling') until done"}
        if not s:
            found = self.detect()
            if not found["available"]:
                return self._to_platform(found["reason"])
            s = self._claim()

        deadline = time.monotonic() + budget_s
        out_dir = self.folder.output / "imm_pdfs"
        out_dir.mkdir(parents=True, exist_ok=True)
        for form in s["forms"]:
            if form["imm_pdf_id"] in s["done"]:
                continue
            if time.monotonic() > deadline:
                break
            s["done"][form["imm_pdf_id"]] = self._fill_one(form, out_dir)
            self._save(s)
            if not s["done"][form["imm_pdf_id"]]["ok"] and s["done"][form["imm_pdf_id"]].get("acrobat_failed") \
                    and not any(r["ok"] for r in s["done"].values()):
                # Acrobat can't automate at all on this PC: the platform fills every form instead.
                return self._to_platform(s["done"][form["imm_pdf_id"]]["error"])

        remaining = sum(1 for f in s["forms"] if f["imm_pdf_id"] not in s["done"])
        result = {"mode": "local", "filled": [r for r in s["done"].values() if r["ok"]],
                  "failed": [r for r in s["done"].values() if not r["ok"]], "skipped": s["skipped"],
                  "remaining": remaining}
        if remaining:
            return {**result, "next": "call autofill_forms again"}
        fin = self.api.autofill_finish(self.folder.survey_id)
        if fin.get("need_restart") and s.get("restarts", 0) < MAX_REFILLS:
            s = self._claim(restarts=s.get("restarts", 0) + 1)
            return {**result, "remaining": len(s["forms"]),
                    "next": "survey values changed during the fill: call autofill_forms again to refill"}
        if result["failed"]:
            names = ", ".join(r["name"] for r in result["failed"])
            return {**result, **self._to_platform(f"{len(result['failed'])} form(s) failed locally ({names})")}
        self._clear()
        return {**result, "automation_status": fin.get("automation_status"), "imm_pdfs": fin.get("imm_pdfs", [])}

    def _fill_one(self, form: dict, out_dir: Path) -> dict:
        name, sid, pid = form["name"], self.folder.survey_id, form["imm_pdf_id"]
        work = self.folder.cache / "autofill" / pid
        template = self.api.download_to(form["template_url"], work / f"{name}_blank.pdf")
        out = out_dir / f"{name}.pdf"
        try:
            r = self.fill(template, form.get("ops", []), out)
        except Exception as e:  # noqa: BLE001  (any Acrobat failure hands the form to the platform)
            logger.warning("%s: local fill failed: %s", name, e)
            self.api.autofill_result(sid, pid, error=f"local Acrobat fill failed: {e}"[:2000])
            return {"name": name, "ok": False, "error": str(e)[:300], "acrobat_failed": True}
        ops = max(1, len(form.get("ops", [])))
        rate = float(form.get("success_rate") or 0.0) * r["applied"] / ops
        self.api.autofill_result(sid, pid, pdf=out, success_rate=rate,
                                 errors=list(form.get("errors", [])) + r["errors"],
                                 survey_values_updated_at=form.get("survey_values_updated_at"))
        return {"name": name, "ok": True, "applied": r["applied"], "failed_fields": r["failed"],
                "path": str(out.relative_to(self.folder.root))}
