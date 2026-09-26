"""The only place tasks are executed: pull → resolve inputs → spawn runtime → validate → submit."""
from __future__ import annotations

import json
import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .api import ApiError, UApplyApi
from .folder import WorkingFolder
from .local_ops import pdf_page_count, pdf_pages_text, render_pdf_pages, to_image_for_model
from .runners import PlanLimited, Runner, RunnerError, get_runner

logger = logging.getLogger(__name__)

IMAGE_EXT = (".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp")


@dataclass
class RunStats:
    accepted: int = 0
    rejected: int = 0
    released: int = 0
    failed: int = 0
    plan_limited: bool = False
    remaining: int = 0
    model_calls: int = 0
    text_layer_docs: int = 0      # documents extracted with pdfplumber, no model look
    failures: list = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def bump(self, name: str, n: int = 1) -> None:
        with self._lock:
            setattr(self, name, getattr(self, name) + n)

    def note_failure(self, item: dict) -> None:
        with self._lock:
            self.failures.append(item)

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}


def _schema_check(schema: dict, output: dict) -> Optional[str]:
    """Cheap local check so an obviously wrong answer is retried before a round-trip."""
    if not isinstance(output, dict):
        return "output is not an object"
    for key in schema.get("required", []):
        if key not in output:
            return f"missing required field {key!r}"
    return None


def _merge_usage(total: dict, part: dict) -> dict:
    for k in ("input_tokens", "output_tokens", "duration_s", "cost_usd"):
        if part.get(k) is not None:
            total[k] = (total.get(k) or 0) + part[k]
    return total


class Executor:
    def __init__(self, api: UApplyApi, folder: WorkingFolder, runner: Optional[Runner] = None,
                 runtime: str = "auto", model: str = "", session_id: Optional[str] = None,
                 force_ocr: bool = False):
        self.api = api
        self.folder = folder
        self.runner = runner or get_runner(runtime, model)
        self.session_id = session_id or f"{self.runner.name}-{uuid.uuid4().hex[:8]}"
        self.force_ocr = force_ocr

    # ---- inputs ----

    def _download_inputs(self, task: dict) -> list[Path]:
        """Download each input document into the task's cache dir; return the raw files."""
        cache = self.folder.cache / task["id"]
        cache.mkdir(parents=True, exist_ok=True)
        files = []
        for inp in task["payload"].get("inputs", []):
            name = inp.get("file_name") or f"{inp['document_id']}.bin"
            dest = cache / Path(name).name
            if not dest.exists():
                url = self.api.document_download_url(inp["document_id"])
                self.api.download_to(url, dest)
            files.append(dest)
        return files

    def _for_model(self, path: Path, cache: Path) -> Path:
        """A PDF goes to the runtime as-is when it can read PDFs; otherwise page 1 as an image."""
        if path.suffix.lower() == ".pdf" and self.runner.supports_pdf:
            return path
        return to_image_for_model(path, cache)

    # ---- generic kinds (classification, sections, ...) ----

    def _run_generic(self, task: dict, stats: RunStats) -> None:
        payload = task["payload"]
        schema = payload["output_schema"]
        cwd = self.folder.cache / task["id"]
        files = self._download_inputs(task)
        images = [self._for_model(p, cwd) for p in files]
        user_prompt = payload.get("user_prompt", "")
        if any(p.suffix.lower() == ".pdf" for p in images):
            user_prompt += ("\n\nFor a PDF, read only its first pages: call Read with pages=\"1-3\" "
                            "(never without a page range) and classify from those.")
        for t in payload.get("text_inputs", []):
            user_prompt += f"\n\n--- document {t.get('document_id')} ---\n{t.get('text', '')}"
        note = ""
        for attempt in range(2):
            rr = self.runner.run(system_prompt=payload["system_prompt"], user_prompt=user_prompt + note,
                                 schema=schema, images=images, cwd=cwd)
            stats.bump('model_calls')
            local_err = _schema_check(schema, rr.output)
            if local_err:
                note = f"\n\nYour previous answer was invalid ({local_err}). Return JSON matching the schema exactly."
                continue
            if self._submit(task, rr.output, rr.model, rr.usage, stats, note_out := []):
                return
            note = note_out[0]
        self.api.release_task(task["id"], "executor: could not produce a valid result in this run")
        stats.bump('released')

    # ---- extract_content: OCR the whole document once ----

    def _run_extract_content(self, task: dict, stats: RunStats) -> None:
        payload = task["payload"]
        schema = payload["output_schema"]
        cwd = self.folder.cache / task["id"]
        src = self._download_inputs(task)[0]
        ext = src.suffix.lower()

        if ext == ".pdf":
            text_pages = pdf_pages_text(src, force_ocr=self.force_ocr)
            if text_pages is not None:
                # Text layer present and sane: no model call at all.
                pages = [{"n": i, "text": t} for i, t in enumerate(text_pages, start=1)]
                logger.info(f"task {task['id']}: {len(pages)} pages from text layer, no model call")
                stats.bump('text_layer_docs')
                self._submit(task, {"pages": pages}, "pdfplumber", {}, stats, [], runtime="local")
                return
            pages, usage, model = self._ocr_pdf(src, payload, schema, cwd, stats)
        elif ext in IMAGE_EXT:
            img = to_image_for_model(src, cwd)
            rr = self.runner.run(system_prompt=payload["system_prompt"],
                                 user_prompt=payload["user_prompt"] + "\n\nThis is a single-page document: return pages=[{n: 1, text}].",
                                 schema=schema, images=[img], cwd=cwd)
            stats.bump('model_calls')
            pages = [{"n": 1, "text": (rr.output.get("pages") or [{}])[0].get("text", "")}]
            usage, model = rr.usage, rr.model
        else:
            raise RunnerError(f"extract_content: unsupported file type {ext}")

        if not pages:
            raise RunnerError("extract_content: the runtime returned no pages")
        if not self._submit(task, {"pages": pages}, model, usage, stats, []):
            self.api.release_task(task["id"], "executor: OCR result rejected")
            stats.bump('released')

    def _ocr_pdf(self, src: Path, payload: dict, schema: dict, cwd: Path, stats: RunStats):
        """Chunk a scanned PDF by the runtime's page budget; one headless call per chunk."""
        n = pdf_page_count(src)
        per = max(1, self.runner.pages_per_call)
        pages, usage, model = [], {}, ""
        for first in range(1, n + 1, per):
            last = min(first + per - 1, n)
            got, u, m = self._ocr_range(src, payload, schema, cwd, stats, first, last)
            pages.extend(got)
            usage = _merge_usage(usage, u)
            model = m or model
        seen = {}
        for p in pages:
            if 1 <= p["n"] <= n:
                seen.setdefault(p["n"], p)
        missing = [k for k in range(1, n + 1) if k not in seen]
        if missing:
            # One retry, page by page, for whatever the chunked pass skipped.
            logger.warning(f"OCR missed pages {missing[:10]}; retrying them individually")
            for k in missing:
                got, u, m = self._ocr_range(src, payload, schema, cwd, stats, k, k)
                usage = _merge_usage(usage, u)
                model = m or model
                for p in got:
                    if p["n"] == k:
                        seen[k] = p
        still = [k for k in range(1, n + 1) if k not in seen]
        if still:
            raise RunnerError(f"OCR could not read pages {still[:10]} of {src.name}")
        return [seen[k] for k in sorted(seen)], usage, model

    def _ocr_range(self, src, payload, schema, cwd, stats, first, last):
        if self.runner.supports_pdf:
            images = [src]
            prompt = (f"{payload['user_prompt']}\n\nTranscribe pages {first} to {last} of {src.name} "
                      f"(read it with pages=\"{first}-{last}\"). Return exactly {last - first + 1} entries, "
                      f"numbered n={first} to n={last}.")
        else:
            images = render_pdf_pages(src, cwd, first, last)
            prompt = (f"{payload['user_prompt']}\n\nThe images are pages {first} to {last} of the document, in order. "
                      f"Return exactly {last - first + 1} entries, numbered n={first} to n={last}.")
        rr = self.runner.run(system_prompt=payload["system_prompt"], user_prompt=prompt,
                             schema=schema, images=images, cwd=cwd)
        stats.bump('model_calls')
        got = [p for p in (rr.output.get("pages") or []) if isinstance(p, dict)]
        if got and all(1 <= int(p.get("n", 0)) <= (last - first + 1) for p in got) and first != 1:
            for p in got:
                p["n"] = int(p["n"]) + first - 1
        return [{"n": int(p.get("n", 0)), "text": str(p.get("text", ""))} for p in got], rr.usage or {}, rr.model

    # ---- submit ----

    def _submit(self, task: dict, output: dict, model: str, usage: dict, stats: RunStats,
                note_out: list, runtime: Optional[str] = None) -> bool:
        out = self.api.submit_result(task["id"], output, model, runtime or self.runner.name, usage, self.session_id)
        if out.get("accepted"):
            stats.bump('accepted')
            logger.info(f"task {task['id']} ({task['kind']}) accepted: {json.dumps(output)[:120]}")
            return True
        rej = out.get("rejection") or {}
        stats.bump('rejected')
        if out.get("task_status") in ("failed", "cancelled", "expired"):
            stats.note_failure({"task_id": task["id"], "kind": task["kind"], "reason": rej.get("message", out.get("task_status"))})
            note_out.append("")
            return True  # nothing more to do for this task
        note_out.append(f"\n\nYour previous answer was rejected: {rej.get('code')}: {rej.get('message')}. Fix it and return JSON only.")
        return False

    # ---- one task ----

    def run_one(self, task: dict, stats: RunStats) -> None:
        try:
            if task["kind"] == "extract_content":
                self._run_extract_content(task, stats)
            else:
                self._run_generic(task, stats)
        except PlanLimited as e:
            stats.plan_limited = True
            self._release_quietly(task, f"plan limit: {e}")
            stats.bump('released')
            raise
        except (RunnerError, ApiError, OSError, ValueError) as e:
            logger.error(f"task {task['id']} failed: {e}")
            stats.bump('failed')
            stats.note_failure({"task_id": task["id"], "kind": task["kind"], "reason": str(e)[:300]})
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
