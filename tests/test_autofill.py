"""IMM PDF auto-fill: replaying recorded ops in Acrobat, and the local / platform orchestration."""
import sys
from pathlib import Path

import pytest

from uapply_agent import acrobat
from uapply_agent.autofill import AutoFiller
from uapply_agent.folder import WorkingFolder


class Field:
    def __init__(self, items=()):
        self.formattedValue = ""
        self.events, self.items, self.selected = [], list(items), None

    def execEvent(self, e):
        self.events.append(e)

    def getDisplayItem(self, i):
        return self.items[i - 1] if 1 <= i <= len(self.items) else ""

    def setItemState(self, i, state):
        self.selected = i


class Xfa:
    def __init__(self, fields):
        self.fields = fields

    def resolveNode(self, node):
        return self.fields.get(node)


def test_choice_matching_follows_pdf_auto_mappers():
    gender = {"compare": "equals", "mapping": {"M Male": "Male", "F Female": "Female"}}
    assert acrobat.choice_matches("M Male", "male", gender)
    assert not acrobat.choice_matches("F Female", "male", gender)             # equals, not contains
    assert acrobat.choice_matches("Married", "Married - living together", {"compare": "value_contains"})
    assert acrobat.choice_matches("China (Hong Kong SAR)", "hong kong", {"mapping": {"China (Hong Kong SAR)": "Hong Kong"}})
    assert acrobat.choice_matches("Canada’s", "canada's", None)               # quotes normalised


def test_apply_op_sets_values_events_and_choices():
    name, dob, sex = Field(), Field(), Field(items=["F Female", "M Male", "U Unknown"])
    xfa = Xfa({"n": name, "d": dob, "s": sex})
    assert acrobat.apply_op(xfa, {"op": "set", "node": "n", "value": "LI", "event": None}) is None
    assert acrobat.apply_op(xfa, {"op": "set", "node": "d", "value": 2007, "event": "change"}) is None
    assert acrobat.apply_op(xfa, {"op": "choice", "node": "s", "value": "Male",
                                  "mapper": {"compare": "equals", "mapping": {"M Male": "Male"}}}) is None
    assert (name.formattedValue, dob.formattedValue, dob.events) == ("LI", "2007", ["change"])
    assert sex.selected == 2 and sex.events == ["exit"]                       # 1-based, as XfaHelper
    assert "no option" in acrobat.apply_op(xfa, {"op": "choice", "node": "s", "value": "Other", "mapper": {"compare": "equals"}})
    assert "not found" in acrobat.apply_op(xfa, {"op": "set", "node": "missing", "value": "x"})


class FakeDoc:
    opened = []

    def __init__(self, path, fields=None):
        self.path = path
        self.jso = type("J", (), {"xfa": Xfa(fields if fields is not None else {"n": Field()})})()

    def __enter__(self):
        FakeDoc.opened.append(self.path)
        return self

    def __exit__(self, *a):
        pass

    def save(self, out):
        Path(out).write_bytes(b"%PDF filled")


def test_fill_replays_and_saves(tmp_path):
    out = tmp_path / "IMM5709.pdf"
    r = acrobat.fill(tmp_path / "blank.pdf", [{"op": "set", "node": "n", "value": "x"},
                                              {"op": "set", "node": "zz", "value": "y"}], out, doc_factory=FakeDoc)
    assert r["applied"] == 1 and r["failed"] == 1 and out.read_bytes() == b"%PDF filled"


def test_detect_is_off_when_not_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert acrobat.detect()["available"] is False


class FakeApi:
    def __init__(self, forms, need_restart=False):
        self.forms, self.need_restart = forms, need_restart
        self.calls, self.results = [], []

    def start_auto_filling(self, sid):
        self.calls.append("start_auto_filling")

    def autofill_claim(self, sid):
        self.calls.append("claim")
        return {"forms": self.forms, "skipped": []}

    def download_to(self, url, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"%PDF blank")
        return dest

    def autofill_result(self, sid, pid, pdf=None, error="", **kw):
        self.results.append((pid, pdf is not None, error, kw.get("success_rate")))

    def autofill_finish(self, sid):
        self.calls.append("finish")
        restart, self.need_restart = self.need_restart, False
        return {"need_restart": restart, "automation_status": "filling" if restart else "completed", "imm_pdfs": []}


def form(i):
    return {"imm_pdf_id": f"id-{i}", "name": f"IMM{i}", "template_url": "https://s3/x", "success_rate": 90.0,
            "ops": [{"op": "set", "node": "n", "value": "v"}], "errors": [], "survey_values_updated_at": None}


@pytest.fixture
def folder(tmp_path):
    f = WorkingFolder(tmp_path / "client")
    f.root.mkdir()
    f.init_case("s-1", "https://api.example")
    return f


def available():
    return {"available": True, "path": "Acrobat.exe", "reason": ""}


def local_fill(template, ops, out):
    return acrobat.fill(template, ops, out, doc_factory=FakeDoc)


def test_without_acrobat_the_platform_fills(folder):
    api = FakeApi([form(1)])
    filler = AutoFiller(api, folder, detect=lambda: {"available": False, "reason": "not installed"})
    r = filler.run()
    assert r["mode"] == "platform" and api.calls == ["start_auto_filling"]
    assert filler.run()["mode"] == "platform" and api.calls == ["start_auto_filling"]   # not started twice


def test_local_fill_uploads_every_form_then_finishes(folder):
    api = FakeApi([form(1), form(2)])
    r = AutoFiller(api, folder, detect=available, fill=local_fill).run()
    assert r["remaining"] == 0 and r["automation_status"] == "completed"
    assert [x["name"] for x in r["filled"]] == ["IMM1", "IMM2"]
    assert [(pid, has_pdf) for pid, has_pdf, _, _ in api.results] == [("id-1", True), ("id-2", True)]
    assert api.results[0][3] == 90.0
    assert (folder.output / "imm_pdfs" / "IMM1.pdf").exists()
    assert not (folder.state / "autofill.json").exists()


def test_fill_is_time_boxed_and_resumes(folder):
    api = FakeApi([form(1), form(2)])
    filler = AutoFiller(api, folder, detect=available, fill=local_fill)
    first = filler.run(budget_s=-1)
    assert first["remaining"] == 2 and api.calls == ["claim"]
    assert filler.run()["remaining"] == 0 and api.calls == ["claim", "finish"]           # no second claim


def test_acrobat_that_cannot_automate_hands_over_to_the_platform(folder):
    def broken(template, ops, out):
        raise RuntimeError("Acrobat returned no JavaScript object")
    api = FakeApi([form(1), form(2)])
    r = AutoFiller(api, folder, detect=available, fill=broken).run()
    assert r["mode"] == "platform" and api.calls == ["claim", "start_auto_filling"]
    assert api.results[0][2].startswith("local Acrobat fill failed")


def test_values_changed_during_the_fill_refill_once(folder):
    api = FakeApi([form(1)], need_restart=True)
    filler = AutoFiller(api, folder, detect=available, fill=local_fill)
    r = filler.run()
    assert r["remaining"] == 1 and api.calls == ["claim", "finish", "claim"]
    assert filler.run()["automation_status"] == "completed"
