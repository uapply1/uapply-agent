"""Tasks as subagents of the RCIC's own Claude Code session.

The server never spawns a model process here. One round: pull tasks, finish locally what needs no
model (PDF text layers), write a brief per remaining task, and hand the briefs back to the
conversation, which spawns one `uapply:task-runner` subagent per brief. The subagent reads the
brief and calls `submit_task`; the RCIC's session login and plan cover every model call. Light
tasks (tiers.py) share one subagent, up to LIGHT_PER_AGENT of them, on a smaller `model`.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

from . import briefs, tiers
from .api import ApiError, UApplyApi
from .folder import WorkingFolder

logger = logging.getLogger(__name__)

RUNTIME = "claude-code-session"
LEASE_S = 1800               # a subagent may take a while on a long scan; the lease must outlast it
MAX_TASKS_PER_ROUND = 8
LIGHT_PER_AGENT = 8          # light briefs one subagent reads and submits in parallel
OCR_PAGE_NOTE = ("The images are the document's pages in order (page 1 first). Return one entry per page, "
                 "numbered n=1 upwards, with an empty string for a blank page.")


@dataclass
class Round:
    tasks: list[dict] = field(default_factory=list)      # one entry per subagent the conversation spawns
    submitted: int = 0                                   # finished here without a model
    failed: list[dict] = field(default_factory=list)
    session_id: str = field(default_factory=lambda: f"session-{uuid.uuid4().hex[:8]}")

    def as_dict(self) -> dict:
        return {"mode": "session", "tasks": self.tasks, "submitted": self.submitted, "failed": self.failed}


class SessionRunner:
    def __init__(self, api: UApplyApi, folder: WorkingFolder, force_ocr: bool = False, light_model: str = ""):
        self.api, self.folder, self.force_ocr, self.light_model = api, folder, force_ocr, light_model
        self.session_id = f"session-{uuid.uuid4().hex[:8]}"

    def prepare_round(self, n: int, kinds: list[str] | None = None) -> Round:
        survey_ids = self.folder.family_survey_ids
        rnd = Round(session_id=self.session_id)
        n = max(1, min(int(n or 1), MAX_TASKS_PER_ROUND))
        light = []
        for task in self.api.pull_tasks(survey_ids, n, self.session_id, RUNTIME, kinds=kinds, lease_s=LEASE_S):
            try:
                brief = self._prepare(task, rnd)
            except (ApiError, OSError, ValueError) as e:
                logger.warning("task %s could not be prepared: %s", task["id"], e)
                rnd.failed.append({"task_id": task["id"], "reason": str(e)[:300]})
                self._release(task["id"], f"could not prepare the task: {e}")
                continue
            if not brief:
                continue
            if tiers.tier(task) == tiers.LIGHT:
                light.append(brief)
            else:
                rnd.tasks.append({**brief, "prompt": f"Task brief: {brief['brief']}"})
        for i in range(0, len(light), LIGHT_PER_AGENT):
            rnd.tasks.append(self._light_entry(light[i:i + LIGHT_PER_AGENT]))
        return rnd

    def _light_entry(self, group: list[dict]) -> dict:
        """One subagent for a group of light briefs; a group of one is an ordinary entry."""
        model = {"model": self.light_model} if self.light_model else {}
        if len(group) == 1:
            return {**group[0], **model, "prompt": f"Task brief: {group[0]['brief']}"}
        paths = [b["brief"] for b in group]
        return {"task_ids": [b["task_id"] for b in group], "kind": "llm_call", "briefs": paths, **model,
                "prompt": "Task briefs:\n" + "\n".join(f"- {p}" for p in paths)}

    def _prepare(self, task: dict, rnd: Round) -> dict | None:
        cwd = briefs.task_dir(self.folder, task["id"])
        files = briefs.download_inputs(self.api, self.folder, task)
        if task["kind"] == "extract_content":
            src = files[0]
            local = briefs.local_pages(src, self.force_ocr)
            if local is not None:
                pages, reader = local
                logger.info("task %s: %d pages read by %s, no model call", task["id"], len(pages), reader)
                verdict = briefs.submit(self.api, task["id"], task["kind"], {"pages": pages}, reader, "local",
                                        {}, self.session_id)
                if verdict.accepted:
                    rnd.submitted += 1
                else:
                    rnd.failed.append({"task_id": task["id"], "reason": verdict.feedback})
                    self._release(task["id"], "text-layer result rejected", count_attempt=False)
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

    def _release(self, task_id: str, reason: str, count_attempt: bool = True) -> None:
        try:
            self.api.release_task(task_id, reason, count_attempt=count_attempt)
        except ApiError as e:   # the lease expires on its own; the server re-queues the task
            logger.warning("releasing task %s failed: %s", task_id, e)
