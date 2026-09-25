"""The only place tasks are executed: pull → resolve inputs → spawn runtime → validate → submit."""
from __future__ import annotations

import json
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .api import ApiError, UApplyApi
from .folder import WorkingFolder
from .local_ops import to_image_for_model
from .runners import PlanLimited, Runner, RunnerError, get_runner

logger = logging.getLogger(__name__)


@dataclass
class RunStats:
    accepted: int = 0
    rejected: int = 0
    released: int = 0
    failed: int = 0
    plan_limited: bool = False
    remaining: int = 0
    failures: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def _schema_check(schema: dict, output: dict) -> Optional[str]:
    """Cheap local check so an obviously wrong answer is retried before a round-trip."""
    if not isinstance(output, dict):
        return "output is not an object"
    for key in schema.get("required", []):
        if key not in output:
            return f"missing required field {key!r}"
    return None


class Executor:
    def __init__(self, api: UApplyApi, folder: WorkingFolder, runner: Optional[Runner] = None,
                 runtime: str = "auto", model: str = "", session_id: Optional[str] = None):
        self.api = api
        self.folder = folder
        self.runner = runner or get_runner(runtime, model)
        self.session_id = session_id or f"{self.runner.name}-{uuid.uuid4().hex[:8]}"

    # ---- inputs ----

    def _materialize_inputs(self, task: dict) -> list[Path]:
        """Download each input document into the cache and return image paths for the model."""
        cache = self.folder.cache / task["id"]
        cache.mkdir(parents=True, exist_ok=True)
        images = []
        for inp in task["payload"].get("inputs", []):
            name = inp.get("file_name") or f"{inp['document_id']}.bin"
            dest = cache / Path(name).name
            if not dest.exists():
                url = self.api.document_download_url(inp["document_id"])
                self.api.download_to(url, dest)
            images.append(to_image_for_model(dest, cache))
        return images

    # ---- one task ----

    def run_one(self, task: dict, stats: RunStats) -> None:
        payload = task["payload"]
        schema = payload["output_schema"]
        try:
            images = self._materialize_inputs(task)
            cwd = self.folder.cache / task["id"]
            user_prompt = payload.get("user_prompt", "")
            for t in payload.get("text_inputs", []):
                user_prompt += f"\n\n--- document {t.get('document_id')} ---\n{t.get('text', '')}"
            note = ""
            for attempt in range(2):
                rr = self.runner.run(system_prompt=payload["system_prompt"], user_prompt=user_prompt + note,
                                     schema=schema, images=images, cwd=cwd)
                local_err = _schema_check(schema, rr.output)
                if local_err:
                    note = f"\n\nYour previous answer was invalid ({local_err}). Return JSON matching the schema exactly."
                    continue
                out = self.api.submit_result(task["id"], rr.output, rr.model, self.runner.name, rr.usage, self.session_id)
                if out.get("accepted"):
                    stats.accepted += 1
                    logger.info(f"task {task['id']} ({task['kind']}) accepted: {json.dumps(rr.output)[:120]}")
                    return
                rej = out.get("rejection") or {}
                stats.rejected += 1
                if out.get("task_status") in ("failed", "cancelled", "expired"):
                    stats.failures.append({"task_id": task["id"], "kind": task["kind"], "reason": rej.get("message", out.get("task_status"))})
                    return
                note = f"\n\nYour previous answer was rejected: {rej.get('code')}: {rej.get('message')}. Fix it and return JSON only."
            # Two local attempts spent; leave the task queued for the next run.
            self.api.release_task(task["id"], "executor: could not produce a valid result in this run")
            stats.released += 1
        except PlanLimited as e:
            stats.plan_limited = True
            self._release_quietly(task, f"plan limit: {e}")
            stats.released += 1
            raise
        except (RunnerError, ApiError, OSError) as e:
            logger.error(f"task {task['id']} failed: {e}")
            stats.failed += 1
            stats.failures.append({"task_id": task["id"], "kind": task["kind"], "reason": str(e)[:300]})
            self._release_quietly(task, str(e))

    def _release_quietly(self, task: dict, reason: str) -> None:
        try:
            self.api.release_task(task["id"], reason)
        except Exception as e:
            logger.warning(f"release {task['id']} failed: {e}")

    # ---- the loop ----

    def run(self, max_tasks: Optional[int] = None, workers: int = 2, kinds: Optional[list] = None,
            batch: int = 5) -> RunStats:
        stats = RunStats()
        survey_ids = self.folder.family_survey_ids
        if not survey_ids:
            raise RunnerError("folder has no case; run `uapply-agent init --survey <id>` first")
        done = 0
        workers = max(1, min(int(workers or 1), 4))
        while max_tasks is None or done < max_tasks:
            n = batch if max_tasks is None else min(batch, max_tasks - done)
            tasks = self.api.pull_tasks(survey_ids, n, self.session_id, self.runner.name, kinds=kinds)
            if not tasks:
                break
            try:
                if workers == 1 or len(tasks) == 1:
                    for t in tasks:
                        self.run_one(t, stats)
                else:
                    with ThreadPoolExecutor(max_workers=workers) as pool:
                        list(pool.map(lambda t: self.run_one(t, stats), tasks))
            except PlanLimited:
                break
            done += len(tasks)
        try:
            st = self.api.task_stats(survey_ids[0])
            stats.remaining = int(st.get("queued", 0)) + int(st.get("leased", 0))
        except Exception:
            pass
        return stats
