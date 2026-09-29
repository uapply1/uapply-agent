"""The only place tasks are executed: pull → resolve inputs → spawn runtime → validate → submit."""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .api import ApiError, UApplyApi
from .folder import WorkingFolder
from .constants import IMAGE_EXTENSIONS
from .local_ops import pdf_page_count, pdf_pages_text, render_pdf_pages, to_image_for_model
from .runners import PlanLimited, Runner, RunnerError, RuntimeUnavailable

logger = logging.getLogger(__name__)

PROMPT_INLINE_MAX = 6000   # characters; longer prompts are passed as files
CLASSIFY_PAGES = 3         # a PDF's first pages are enough to classify it
ATTEMPTS_PER_TASK = 2      # model answers per task and run, the second with the rejection as feedback
OCR_SECONDS_PER_PAGE = 30  # extra time budget per page for chunked OCR calls
MAX_WORKERS = 4
FINAL_TASK_STATUSES = ("failed", "cancelled", "expired")


@dataclass
class SubmitOutcome:
    done: bool                 # accepted, or the server closed the task: stop working on it
    retry_note: str = ""       # feedback for the next attempt when the server rejected the answer


@dataclass
class RunStats:
    accepted: int = 0
    rejected: int = 0
    released: int = 0
    failed: int = 0
    plan_limited: bool = False
    runtime_error: str = ""      # the runtime cannot work (not signed in): the run stopped
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
    def __init__(self, api: UApplyApi, folder: WorkingFolder, runner: Runner, *, force_ocr: bool = False,
                 timeout_s: int = 300, session_id: Optional[str] = None):
        self.api = api
        self.folder = folder
        self.runner = runner
        self.force_ocr = force_ocr
        self.timeout_s = timeout_s
        self.session_id = session_id or f"{runner.name}-{uuid.uuid4().hex[:8]}"

    # ---- inputs ----

    def _download_inputs(self, task: dict) -> list[Path]:
        """Download each input document into the task's cache dir; return the raw files."""
        cache = self.folder.cache / task["id"]
        cache.mkdir(parents=True, exist_ok=True)
        files = []
        for inp in task["payload"].get("inputs", []):
            name = inp.get("file_name") or f"{inp.get('document_id') or 'input'}.bin"
            dest = cache / Path(name).name
            if not dest.exists():
                # llm_call inputs carry a short-lived URL; document inputs are fetched by id
                url = inp.get("url") or self.api.document_download_url(inp["document_id"])
                self.api.download_to(url, dest)
            files.append(dest)
        return files

    def _prompt_files(self, task: dict, cwd: Path, system_prompt: str, user_prompt: str):
        """Long prompts and documents go to files the model reads: Windows caps a command line at
        ~32 KB and section/analysis prompts embed whole documents."""
        text_files = []
        for tf in task["payload"].get("text_files", []) or []:
            p = cwd / Path(tf.get("file_name") or "input.txt").name
            p.write_text(tf.get("text", ""), encoding="utf-8")
            text_files.append(p)
        if len(system_prompt) > PROMPT_INLINE_MAX:
            p = cwd / "instructions.md"
            p.write_text(system_prompt, encoding="utf-8")
            text_files.append(p)
            system_prompt = ("Your complete instructions are in instructions.md in the current directory. "
                             "Read the whole file first and follow it exactly.")
        if len(user_prompt) > PROMPT_INLINE_MAX:
            p = cwd / "task.md"
            p.write_text(user_prompt, encoding="utf-8")
            text_files.append(p)
            user_prompt = "The task is in task.md in the current directory. Read the whole file, then answer."
        return system_prompt, user_prompt, text_files

    def _for_model(self, path: Path, cache: Path) -> list[Path]:
        """Images the model looks at: a PDF's first pages rendered locally (Read's `pages` needs poppler)."""
        if path.suffix.lower() == ".pdf":
            return render_pdf_pages(path, cache, 1, min(CLASSIFY_PAGES, pdf_page_count(path)))
        return [to_image_for_model(path, cache)]

    # ---- generic kinds (classification, sections, ...) ----

    def _run_generic(self, task: dict, stats: RunStats) -> None:
        payload = task["payload"]
        schema = payload["output_schema"]
        cwd = self.folder.cache / task["id"]
        files = self._download_inputs(task)
        images = [img for p in files for img in self._for_model(p, cwd)]
        user_prompt = payload.get("user_prompt", "")
        for t in payload.get("text_inputs", []):
            user_prompt += f"\n\n--- document {t.get('document_id')} ---\n{t.get('text', '')}"
        rejection = task.get("rejection") or {}
        if rejection.get("code") == "SCHEMA_INVALID":   # handed back by the server with the reason
            user_prompt += f"\n\nA previous answer was rejected: {rejection.get('message')}. Fix that."
        system_prompt, user_prompt, text_files = self._prompt_files(task, cwd, payload.get("system_prompt", ""),
                                                                    user_prompt)
        note = ""
        for _ in range(ATTEMPTS_PER_TASK):
            rr = self.runner.run(system_prompt=system_prompt, user_prompt=user_prompt + note, schema=schema,
                                 images=images, cwd=cwd, text_files=text_files, timeout_s=self.timeout_s)
            stats.bump("model_calls")
            local_err = _schema_check(schema, rr.output)
            if local_err:
                note = f"\n\nYour previous answer was invalid ({local_err}). Return JSON matching the schema exactly."
                continue
            outcome = self._submit(task, rr.output, rr.model, rr.usage, stats)
            if outcome.done:
                return
            note = outcome.retry_note
        self.api.release_task(task["id"], "executor: could not produce a valid result in this run")
        stats.bump("released")

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
                logger.info("task %s: %d pages from the text layer, no model call", task["id"], len(pages))
                stats.bump("text_layer_docs")
                self._submit(task, {"pages": pages}, "pdfplumber", {}, stats, runtime="local")
                return
            pages, usage, model = self._ocr_pdf(src, payload, schema, cwd, stats)
        elif ext in IMAGE_EXTENSIONS:
            img = to_image_for_model(src, cwd)
            prompt = payload["user_prompt"] + "\n\nThis is a single-page document: return pages=[{n: 1, text}]."
            rr = self.runner.run(system_prompt=payload["system_prompt"], user_prompt=prompt, schema=schema,
                                 images=[img], cwd=cwd, timeout_s=self.timeout_s)
            stats.bump("model_calls")
            pages = [{"n": 1, "text": (rr.output.get("pages") or [{}])[0].get("text", "")}]
            usage, model = rr.usage, rr.model
        else:
            raise RunnerError(f"extract_content: unsupported file type {ext}")

        if not pages:
            raise RunnerError("extract_content: the runtime returned no pages")
        if not self._submit(task, {"pages": pages}, model, usage, stats).done:
            self.api.release_task(task["id"], "executor: OCR result rejected")
            stats.bump("released")

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
            logger.warning("OCR missed pages %s; retrying them individually", missing[:10])
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
        # Rendered here with PyMuPDF: Claude's Read needs poppler for a page range, which Windows lacks.
        images = render_pdf_pages(src, cwd, first, last)
        prompt = (f"{payload['user_prompt']}\n\nThe images are pages {first} to {last} of the document, in order. "
                  f"Return exactly {last - first + 1} entries, numbered n={first} to n={last}.")
        timeout = max(self.timeout_s, OCR_SECONDS_PER_PAGE * (last - first + 1))
        rr = self.runner.run(system_prompt=payload["system_prompt"], user_prompt=prompt, schema=schema,
                             images=images, cwd=cwd, timeout_s=timeout)
        stats.bump("model_calls")
        got = [p for p in (rr.output.get("pages") or []) if isinstance(p, dict)]
        # Some models number a chunk's pages from 1; shift them to the document's numbering.
        if got and all(1 <= int(p.get("n", 0)) <= (last - first + 1) for p in got) and first != 1:
            for p in got:
                p["n"] = int(p["n"]) + first - 1
        return [{"n": int(p.get("n", 0)), "text": str(p.get("text", ""))} for p in got], rr.usage or {}, rr.model

    # ---- submit ----

    def _submit(self, task: dict, output: dict, model: str, usage: dict, stats: RunStats,
                runtime: Optional[str] = None) -> SubmitOutcome:
        out = self.api.submit_result(task["id"], output, model, runtime or self.runner.name, usage, self.session_id)
        if out.get("accepted"):
            stats.bump("accepted")
            logger.info("task %s (%s) accepted: %s", task["id"], task["kind"], json.dumps(output)[:120])
            return SubmitOutcome(done=True)
        rej = out.get("rejection") or {}
        stats.bump("rejected")
        if out.get("task_status") in FINAL_TASK_STATUSES:
            stats.note_failure({"task_id": task["id"], "kind": task["kind"],
                                "reason": rej.get("message", out.get("task_status"))})
            return SubmitOutcome(done=True)
        return SubmitOutcome(done=False, retry_note=f"\n\nYour previous answer was rejected: {rej.get('code')}: "
                                                    f"{rej.get('message')}. Fix it and return JSON only.")

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
            stats.bump("released")
            raise
        except RuntimeUnavailable as e:
            stats.runtime_error = str(e)
            self._release_quietly(task, f"runtime unavailable: {e}")
            stats.bump("released")
            raise
        except (RunnerError, ApiError, OSError, ValueError) as e:
            self._fail(task, stats, str(e))
        except Exception as e:  # never let one task abort the run with its lease held
            logger.exception("task %s failed unexpectedly", task["id"])
            self._fail(task, stats, f"{type(e).__name__}: {e}")

    def _fail(self, task: dict, stats: RunStats, reason: str) -> None:
        logger.error("task %s failed: %s", task["id"], reason)
        stats.bump("failed")
        stats.note_failure({"task_id": task["id"], "kind": task["kind"], "reason": reason[:300]})
        self._release_quietly(task, reason)

    def _release_quietly(self, task: dict, reason: str) -> None:
        try:
            self.api.release_task(task["id"], reason)
        except Exception as e:
            logger.warning("releasing task %s failed: %s", task["id"], e)

    # ---- the loop ----

    def run(self, max_tasks: Optional[int] = None, workers: int = 2, kinds: Optional[list] = None,
            batch: int = 5, budget_s: Optional[float] = None) -> RunStats:
        """`budget_s`: stop pulling new tasks after this long (running ones finish), so the chat
        gets control back and can report progress instead of one silent call."""
        deadline = time.monotonic() + budget_s if budget_s else None
        stats = RunStats()
        survey_ids = self.folder.family_survey_ids
        if not survey_ids:
            raise RunnerError("folder has no case; run `uapply-agent init --survey <id>` first")
        done = 0
        workers = max(1, min(int(workers or 1), MAX_WORKERS))
        if deadline:
            batch = workers  # one round per worker slot keeps each check-in short
        while max_tasks is None or done < max_tasks:
            if deadline and time.monotonic() >= deadline:
                break
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
            except (PlanLimited, RuntimeUnavailable):
                break
            done += len(tasks)
        try:
            st = self.api.task_stats(survey_ids[0])
            stats.remaining = int(st.get("queued", 0)) + int(st.get("leased", 0))
        except ApiError:
            logger.debug("task stats unavailable", exc_info=True)
        return stats
