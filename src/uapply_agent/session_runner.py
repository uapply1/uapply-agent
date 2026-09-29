"""Tasks as subagents of the RCIC's own Claude Code session.

The server never spawns a model process here. One round: pull tasks, finish locally what needs no
model (PDF text layers), write a brief per remaining task, and hand the briefs back to the
conversation, which spawns one `uapply:task-runner` subagent per brief. The subagent reads the
brief and calls `submit_task`; the RCIC's session login and plan cover every model call.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

from . import briefs
from .api import ApiError, UApplyApi
from .folder import WorkingFolder

logger = logging.getLogger(__name__)

RUNTIME = "claude-code-session"
LEASE_S = 1800               # a subagent may take a while on a long scan; the lease must outlast it
MAX_TASKS_PER_ROUND = 8
OCR_PAGE_NOTE = ("The images are the document's pages in order (page 1 first). Return one entry per page, "
                 "numbered n=1 upwards, with an empty string for a blank page.")


@dataclass
class Round:
    tasks: list[dict] = field(default_factory=list)      # briefs for the conversation to hand out
    submitted: int = 0                                   # finished here without a model
    failed: list[dict] = field(default_factory=list)
    session_id: str = field(default_factory=lambda: f"session-{uuid.uuid4().hex[:8]}")

    def as_dict(self) -> dict:
        return {"mode": "session", "tasks": self.tasks, "submitted": self.submitted, "failed": self.failed}


class SessionRunner:
    def __init__(self, api: UApplyApi, folder: WorkingFolder, force_ocr: bool = False):
        self.api, self.folder, self.force_ocr = api, folder, force_ocr
        self.session_id = f"session-{uuid.uuid4().hex[:8]}"

    def prepare_round(self, n: int, kinds: list[str] | None = None) -> Round:
        survey_ids = self.folder.family_survey_ids
        rnd = Round(session_id=self.session_id)
        n = max(1, min(int(n or 1), MAX_TASKS_PER_ROUND))
        for task in self.api.pull_tasks(survey_ids, n, self.session_id, RUNTIME, kinds=kinds, lease_s=LEASE_S):
            try:
                brief = self._prepare(task, rnd)
            except (ApiError, OSError, ValueError) as e:
                logger.warning("task %s could not be prepared: %s", task["id"], e)
                rnd.failed.append({"task_id": task["id"], "reason": str(e)[:300]})
                self._release(task["id"], f"could not prepare the task: {e}")
                continue
            if brief:
                rnd.tasks.append(brief)
        return rnd

    def _prepare(self, task: dict, rnd: Round) -> dict | None:
        cwd = briefs.task_dir(self.folder, task["id"])
        files = briefs.download_inputs(self.api, self.folder, task)
        if task["kind"] == "extract_content":
            src = files[0]
            pages = briefs.text_layer_pages(src, self.force_ocr)
            if pages is not None:
                logger.info("task %s: %d pages from the text layer, no model call", task["id"], len(pages))
                verdict = briefs.submit(self.api, task["id"], task["kind"], {"pages": pages}, "pdfplumber", "local",
                                        {}, self.session_id)
                if verdict.accepted:
                    rnd.submitted += 1
                else:
                    rnd.failed.append({"task_id": task["id"], "reason": verdict.feedback})
                    self._release(task["id"], "text-layer result rejected")
                return None
            images = briefs.images_for_ocr(src, cwd)
            note = OCR_PAGE_NOTE
        else:
            images = briefs.images_for_classification(files, cwd)
            note = ""
        text_files = briefs.write_text_files(task, cwd)
        brief = briefs.write_brief(self.folder, task, images, text_files, extra_instruction=note)
        return {"task_id": task["id"], "kind": task["kind"], "document": briefs._document_name(task),
                "brief": str(brief), "pages": len(images)}

    def _release(self, task_id: str, reason: str) -> None:
        try:
            self.api.release_task(task_id, reason)
        except ApiError as e:   # the lease expires on its own; the server re-queues the task
            logger.warning("releasing task %s failed: %s", task_id, e)
