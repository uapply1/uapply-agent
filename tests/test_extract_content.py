"""extract_content: text-layer PDFs never reach the model; scans are chunked per runtime."""
from pathlib import Path

import pytest

from uapply_agent.executor import Executor
from uapply_agent.folder import WorkingFolder
from uapply_agent.runners.base import Runner, RunResult

SCHEMA = {"type": "object", "properties": {"pages": {"type": "array"}}, "required": ["pages"]}


def make_pdf(path: Path, pages: int, text: bool) -> Path:
    import pymupdf
    doc = pymupdf.open()
    for i in range(1, pages + 1):
        page = doc.new_page()
        if text:
            page.insert_text((72, 72), f"Bank statement page {i}. Balance 12,345.67 CAD. " * 3, fontsize=11)
        else:
            page.draw_rect(pymupdf.Rect(50, 50, 300, 300), color=(0, 0, 0), width=2)  # a "scan": drawing, no text
    doc.save(path)
    doc.close()
    return path


def task(file_name="statement.pdf", kind="extract_content"):
    return {"id": "t-ocr", "kind": kind, "payload": {
        "system_prompt": "transcribe", "user_prompt": "请提取", "output_schema": SCHEMA,
        "inputs": [{"document_id": "d1", "file_name": file_name, "mime": "application/pdf"}]}}


class FakeApi:
    def __init__(self, src: Path, tasks):
        self.src, self.queue, self.submitted, self.released = src, list(tasks), [], []

    def pull_tasks(self, survey_ids, n, session_id, runtime, kinds=None, lease_s=None):
        out, self.queue = self.queue[:n], self.queue[n:]
        return out

    def document_download_url(self, document_id):
        return "https://s3/x"

    def download_to(self, url, dest: Path):
        dest.write_bytes(self.src.read_bytes())
        return dest

    def submit_result(self, task_id, result, model, runtime, usage, session_id):
        self.submitted.append({"result": result, "model": model, "runtime": runtime, "usage": usage})
        return {"accepted": True, "task_status": "accepted"}

    def release_task(self, task_id, reason):
        self.released.append(reason)

    def task_stats(self, survey_id):
        return {"queued": 0, "leased": 0}


class ChunkRunner(Runner):
    """Returns pages numbered as asked, records prompts and inputs."""
    name = "fake"
    binary = "fake"

    def __init__(self, supports_pdf, pages_per_call, relative_numbering=False):
        super().__init__()
        self.supports_pdf, self.pages_per_call, self.relative = supports_pdf, pages_per_call, relative_numbering
        self.calls = []

    def run(self, *, system_prompt, user_prompt, schema, images, cwd, timeout_s=300):
        self.calls.append({"prompt": user_prompt, "images": images})
        import re
        m = re.search(r"pages (\d+) to (\d+)", user_prompt)
        first, last = (int(m.group(1)), int(m.group(2))) if m else (1, 1)
        nums = range(1, last - first + 2) if self.relative else range(first, last + 1)
        return RunResult(output={"pages": [{"n": n, "text": f"page {n if not self.relative else n + first - 1}"} for n in nums]},
                         model="fake-model", usage={"input_tokens": 100, "output_tokens": 10})


@pytest.fixture
def folder(tmp_path):
    f = WorkingFolder(tmp_path / "client")
    f.root.mkdir()
    f.init_case("s-1", "https://api.example")
    return f


def test_text_layer_pdf_needs_no_model(folder, tmp_path):
    src = make_pdf(tmp_path / "text.pdf", pages=3, text=True)
    api = FakeApi(src, [task()])
    runner = ChunkRunner(supports_pdf=True, pages_per_call=20)
    stats = Executor(api, folder, runner=runner).run()
    assert stats.accepted == 1 and stats.model_calls == 0 and runner.calls == []
    sub = api.submitted[0]
    assert sub["model"] == "pdfplumber" and sub["runtime"] == "local"
    pages = sub["result"]["pages"]
    assert [p["n"] for p in pages] == [1, 2, 3]
    assert "Balance 12,345.67" in pages[1]["text"]


def test_scanned_pdf_is_chunked_for_pdf_capable_runtime(folder, tmp_path):
    src = make_pdf(tmp_path / "scan.pdf", pages=5, text=False)
    api = FakeApi(src, [task()])
    runner = ChunkRunner(supports_pdf=True, pages_per_call=2)
    stats = Executor(api, folder, runner=runner).run()
    assert stats.accepted == 1 and stats.model_calls == 3
    assert [c["images"][0].suffix for c in runner.calls] == [".pdf"] * 3
    assert "pages 1 to 2" in runner.calls[0]["prompt"] and "pages 5 to 5" in runner.calls[2]["prompt"]
    pages = api.submitted[0]["result"]["pages"]
    assert [p["n"] for p in pages] == [1, 2, 3, 4, 5]
    assert api.submitted[0]["usage"]["input_tokens"] == 300


def test_scanned_pdf_renders_pages_for_image_only_runtime(folder, tmp_path):
    src = make_pdf(tmp_path / "scan.pdf", pages=3, text=False)
    api = FakeApi(src, [task()])
    runner = ChunkRunner(supports_pdf=False, pages_per_call=2, relative_numbering=True)
    stats = Executor(api, folder, runner=runner).run()
    assert stats.accepted == 1 and stats.model_calls == 2
    assert [len(c["images"]) for c in runner.calls] == [2, 1]
    assert all(img.suffix == ".png" and img.exists() for c in runner.calls for img in c["images"])
    pages = api.submitted[0]["result"]["pages"]
    assert [p["n"] for p in pages] == [1, 2, 3]          # relative numbering was re-based
    assert pages[2]["text"] == "page 3"


def test_classification_passes_pdf_to_pdf_capable_runtime(folder, tmp_path):
    src = make_pdf(tmp_path / "passport.pdf", pages=2, text=False)
    t = task(file_name="passport.pdf", kind="classify_document")
    t["payload"]["output_schema"] = {"type": "object", "required": ["file_types"]}

    class ClassifyRunner(ChunkRunner):
        def run(self, **kw):
            self.calls.append(kw)
            return RunResult(output={"file_types": ["Passport"]}, model="m", usage={})

    runner = ClassifyRunner(supports_pdf=True, pages_per_call=20)
    api = FakeApi(src, [t])
    stats = Executor(api, folder, runner=runner).run()
    assert stats.accepted == 1
    assert runner.calls[0]["images"][0].suffix == ".pdf"
    assert "first pages" in runner.calls[0]["user_prompt"]
