from pathlib import Path

import pytest

from uapply_agent.executor import Executor, RunStats
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

    def pull_tasks(self, survey_ids, n, session_id, runtime, kinds=None, lease_s=None):
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

    def run(self, *, system_prompt, user_prompt, schema, images, cwd, timeout_s=300):
        self.calls.append({"system_prompt": system_prompt, "user_prompt": user_prompt, "images": images, "cwd": cwd})
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
