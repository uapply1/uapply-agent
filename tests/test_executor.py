from pathlib import Path

import pytest

from uapply_agent.executor import Executor
from uapply_agent.folder import WorkingFolder
from uapply_agent.runners.base import PlanLimited, Runner, RunResult

SCHEMA = {"type": "object", "properties": {"file_types": {"type": "array"}}, "required": ["file_types"]}


def task(tid="t1", inputs=None):
    return {"id": tid, "kind": "classify_document", "payload": {
        "system_prompt": "classify", "user_prompt": "对上传的图像进行分类", "output_schema": SCHEMA,
        "inputs": inputs if inputs is not None else [{"document_id": "d1", "file_name": "page.jpg", "mime": "image/jpeg"}],
    }}


class FakeApi:
    def __init__(self, tasks, verdicts=None):
        self.queue = list(tasks)
        self.verdicts = verdicts or {}
        self.submitted, self.released = [], []
        self.downloads = 0

    def pull_tasks(self, survey_ids, n, session_id, runtime, kinds=None):
        out, self.queue = self.queue[:n], self.queue[n:]
        return out

    def document_download_url(self, document_id):
        return f"https://s3/{document_id}"

    def download_to(self, url, dest: Path):
        self.downloads += 1
        dest.write_bytes(b"\xff\xd8 fake jpeg")
        return dest

    def submit_result(self, task_id, result, model, runtime, usage, session_id):
        self.submitted.append((task_id, result, model, runtime))
        verdict = self.verdicts.get(task_id)
        if callable(verdict):
            return verdict(result)
        return verdict or {"accepted": True, "task_status": "accepted"}

    def release_task(self, task_id, reason):
        self.released.append((task_id, reason))
        return {}

    def task_stats(self, survey_id):
        return {"queued": len(self.queue), "leased": 0}


class FakeRunner(Runner):
    name = "fake"
    binary = "fake"

    def __init__(self, outputs):
        super().__init__()
        self.outputs = list(outputs)
        self.calls = []

    def run(self, *, system_prompt, user_prompt, schema, images, cwd, timeout_s=300, text_files=()):
        self.calls.append({"system_prompt": system_prompt, "user_prompt": user_prompt, "images": images, "cwd": cwd,
                           "text_files": list(text_files)})
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return RunResult(output=out, model="fake-model", usage={"input_tokens": 1, "output_tokens": 1})


@pytest.fixture
def folder(tmp_path):
    f = WorkingFolder(tmp_path)
    f.init_case("s-1", "https://api.example")
    return f


def test_accepts_first_valid_answer(folder):
    api = FakeApi([task()])
    runner = FakeRunner([{"file_types": ["Passport"]}])
    stats = Executor(api, folder, runner=runner).run()
    assert stats.accepted == 1 and stats.rejected == 0 and stats.released == 0
    assert api.submitted[0][1] == {"file_types": ["Passport"]}
    assert api.submitted[0][3] == "fake"
    assert api.downloads == 1
    call = runner.calls[0]
    assert call["system_prompt"] == "classify"
    assert call["images"][0].name == "page.jpg" and call["images"][0].exists()
    assert call["cwd"] == folder.cache / "t1"


def test_local_schema_check_retries_before_submitting(folder):
    api = FakeApi([task()])
    runner = FakeRunner([{"wrong": 1}, {"file_types": ["Visa"]}])
    stats = Executor(api, folder, runner=runner).run()
    assert stats.accepted == 1
    assert len(api.submitted) == 1
    assert "previous answer was invalid" in runner.calls[1]["user_prompt"]


def test_server_rejection_feeds_back_then_releases(folder):
    api = FakeApi([task()], verdicts={"t1": {"accepted": False, "task_status": "queued",
                                            "rejection": {"code": "TYPE_NOT_ALLOWED", "message": "not allowed: ['Card']"}}})
    runner = FakeRunner([{"file_types": ["Card"]}, {"file_types": ["Card"]}])
    stats = Executor(api, folder, runner=runner).run()
    assert stats.accepted == 0 and stats.rejected == 2 and stats.released == 1
    assert "TYPE_NOT_ALLOWED" in runner.calls[1]["user_prompt"]
    assert api.released[0][0] == "t1"


def test_plan_limit_releases_and_stops(folder):
    api = FakeApi([task("t1"), task("t2")])
    runner = FakeRunner([PlanLimited("usage limit reached")])
    stats = Executor(api, folder, runner=runner).run(workers=1)
    assert stats.plan_limited and stats.released == 1
    assert api.released[0][0] == "t1" and "plan limit" in api.released[0][1]
    assert len(runner.calls) == 1  # t2 never attempted


def test_runner_error_is_recorded_and_task_released(folder):
    from uapply_agent.runners.base import RunnerError
    api = FakeApi([task()])
    runner = FakeRunner([RunnerError("claude exited 1")])
    stats = Executor(api, folder, runner=runner).run()
    assert stats.failed == 1 and stats.failures[0]["reason"].startswith("claude exited")
    assert api.released[0][0] == "t1"


def test_no_case_raises(tmp_path):
    from uapply_agent.runners.base import RunnerError
    with pytest.raises(RunnerError):
        Executor(FakeApi([]), WorkingFolder(tmp_path), runner=FakeRunner([])).run()


def test_budget_stops_pulling_new_tasks(folder, monkeypatch):
    """A time-boxed run returns after the running round, leaving the rest queued for the next call."""
    import uapply_agent.executor as ex_mod
    clock = iter([0.0, 0.0, 1000.0, 1000.0, 1000.0])
    monkeypatch.setattr(ex_mod.time, "monotonic", lambda: next(clock))
    api = FakeApi([task(f"t{i}") for i in range(6)])
    runner = FakeRunner([{"file_types": ["Passport"]}] * 6)
    stats = Executor(api, folder, runner=runner).run(workers=2, budget_s=60)
    assert stats.accepted == 2 and stats.remaining == 4   # one round of `workers` tasks, then the deadline


def test_runtime_unavailable_stops_the_run_after_one_task(folder):
    """54 identical "Not logged in" failures helped nobody: the first one ends the run with the fix."""
    from uapply_agent.runners import RuntimeUnavailable
    api = FakeApi([task(f"t{i}") for i in range(6)])
    runner = FakeRunner([RuntimeUnavailable("the Claude Code CLI is not signed in: run `claude auth login`")] * 6)
    stats = Executor(api, folder, runner=runner).run(workers=1)
    assert len(runner.calls) == 1 and "claude auth login" in stats.runtime_error
    assert stats.released == 1 and stats.failed == 0   # the rest of the batch is never started



def llm_task(tid="L1", prompt="filter the passport", system="SYS", mode="text", inputs=None, text_files=None,
             rejection=None):
    schema = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]} if mode == "text" \
        else {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
    return {"id": tid, "kind": "llm_call", "rejection": rejection, "payload": {
        "system_prompt": system, "user_prompt": prompt, "output_mode": mode, "output_schema": schema,
        "inputs": inputs or [], "text_files": text_files or []}}


def test_llm_call_text_task_is_answered_and_submitted(folder):
    api = FakeApi([llm_task()])
    runner = FakeRunner([{"text": "## Passport\n- Name: ZHANG"}])
    stats = Executor(api, folder, runner=runner).run()
    assert stats.accepted == 1 and api.submitted[0][1] == {"text": "## Passport\n- Name: ZHANG"}
    assert runner.calls[0]["system_prompt"] == "SYS" and runner.calls[0]["user_prompt"] == "filter the passport"


def test_long_prompts_and_documents_go_to_files(folder):
    """Windows caps a command line at ~32 KB; section prompts embed whole documents."""
    long_doc = "第1页\n" + "姓名 ZHANG WEI\n" * 2000
    api = FakeApi([llm_task(prompt=long_doc, system="RULES " * 3000,
                            text_files=[{"file_name": "document.html", "text": "<p>hi</p>"}])])
    runner = FakeRunner([{"text": "ok"}])
    Executor(api, folder, runner=runner).run()
    call = runner.calls[0]
    names = sorted(p.name for p in call["text_files"])
    assert names == ["document.html", "instructions.md", "task.md"]
    assert len(call["user_prompt"]) < 200 and len(call["system_prompt"]) < 200
    task_md = next(p for p in call["text_files"] if p.name == "task.md")
    assert task_md.read_text(encoding="utf-8") == long_doc


def test_blob_inputs_are_downloaded_from_their_url(folder):
    api = FakeApi([llm_task(inputs=[{"blob": "agent-inputs/L1/input1.png", "file_name": "input1.png",
                                     "mime": "image/png", "url": "https://s3/presigned"}])])
    urls = []
    api.download_to = lambda url, dest: (urls.append(url), dest.write_bytes(b"\x89PNG"), dest)[2]
    api.document_download_url = lambda _id: pytest.fail("blob inputs must not be fetched by document id")
    runner = FakeRunner([{"text": "Passport"}])
    Executor(api, folder, runner=runner).run()
    assert urls == ["https://s3/presigned"] and runner.calls[0]["images"]


def test_server_rejection_reason_is_given_back_to_the_model(folder):
    api = FakeApi([llm_task(mode="json", rejection={"code": "SCHEMA_INVALID", "message": "age: field required"})])
    runner = FakeRunner([{"name": "Zhang"}])
    Executor(api, folder, runner=runner).run()
    assert "age: field required" in runner.calls[0]["user_prompt"]


def test_unexpected_error_fails_the_task_but_not_the_run(folder):
    api = FakeApi([task("t1"), task("t2")])
    runner = FakeRunner([KeyError("pages"), {"file_types": ["Passport"]}])
    stats = Executor(api, folder, runner=runner).run(workers=1)
    assert stats.failed == 1 and stats.accepted == 1
    assert api.released[0][0] == "t1" and "KeyError" in api.released[0][1]
