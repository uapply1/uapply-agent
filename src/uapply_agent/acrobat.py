"""IMM PDF auto-fill with the RCIC's own Adobe Acrobat Pro (Windows).

The backend records each form fill as a list of operations (pdf_auto record mode); this module
replays them in Acrobat as pdf_auto's XfaHelper fills: `formattedValue` + event, and choice lists
matched with the same ChoiceMapper rules. Writes run inside Acrobat as JavaScript batches (AFormAut),
one call per choice field plus one, instead of several COM calls per field.
"""
from __future__ import annotations

import json
import logging
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

PD_SAVE_FULL = 1
NO_AUTOMATION = "Acrobat is installed but its automation is unavailable (Reader, or no Pro licence)"
NO_FORMS_AUTOMATION = "Acrobat's forms automation (AFormAut) is unavailable"


class AcrobatError(RuntimeError):
    """Acrobat could not open, automate or save a form."""


# ---- detection ----

def _registry_has_acrobat() -> str | None:
    """Acrobat.exe path from App Paths, if Acrobat (not only Reader) is installed."""
    import winreg
    key = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\Acrobat.exe"
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, key) as k:
                return winreg.QueryValue(k, None) or "Acrobat.exe"
        except OSError:
            continue
    return None


def _iac_registered() -> bool:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"AcroExch.App\CLSID"):
            return True
    except OSError:
        return False


def detect(probe: bool = True) -> dict:
    """{available, path, reason}. Reader and unlicensed Acrobat register the same exe, so a live IAC
    probe (open a PDF, get its JavaScript object) decides."""
    if sys.platform != "win32":
        return {"available": False, "path": None, "reason": "local auto-fill needs Adobe Acrobat Pro on Windows"}
    try:
        path = _registry_has_acrobat()
        if not path or not _iac_registered():
            return {"available": False, "path": path, "reason": "Adobe Acrobat Pro is not installed"}
        if not probe:
            return {"available": True, "path": path, "reason": ""}
        import pymupdf
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "probe.pdf"
            doc = pymupdf.open()
            doc.new_page()
            doc.save(str(pdf))
            doc.close()
            with DialogClicker(), AcrobatDoc(pdf) as d:   # a first-run dialog must not hang the probe
                if d.jso is None:
                    return {"available": False, "path": path, "reason": NO_AUTOMATION}
        if err := _forms_automation_error():
            return {"available": False, "path": path, "reason": err}
        return {"available": True, "path": path, "reason": ""}
    except Exception as e:  # noqa: BLE001  (COM/registry errors of any kind mean "not usable")
        return {"available": False, "path": None, "reason": f"Acrobat automation failed: {e}"[:300]}


def _forms_automation_error() -> str | None:
    """Loads AFormAut, which runs the fill's JavaScript."""
    _co_initialize()
    try:
        _dispatch("AFormAut.App")
        return None
    except Exception as e:  # noqa: BLE001  (any COM error means the fill cannot run)
        return f"{NO_FORMS_AUTOMATION}: {e}"[:300]
    finally:
        import pythoncom
        pythoncom.CoUninitialize()


# ---- one open document ----

def _co_initialize() -> bool:
    import pythoncom
    pythoncom.CoInitialize()
    return True


def _dispatch(prog_id: str):
    import win32com.client
    return win32com.client.Dispatch(prog_id)


class AcrobatDoc:
    """AcroExch.PDDoc + its JavaScript object, for the probe. Never kills Acrobat; quits it only if
    we started it with nothing else open."""

    def __init__(self, path: Path, dispatch: Callable = _dispatch):
        self.path, self._dispatch = Path(path), dispatch
        self.app = self.pd = self.jso = None
        self._com = False

    def __enter__(self) -> AcrobatDoc:
        # MCP tools run on worker threads, and COM must be initialised on each thread that uses it.
        self._com = self._dispatch is _dispatch and _co_initialize()
        self.app = self._dispatch("AcroExch.App")
        self.pd = self._dispatch("AcroExch.PDDoc")
        if not self.pd.Open(str(self.path)):
            raise AcrobatError(f"Acrobat could not open {self.path.name}")
        self.jso = self.pd.GetJSObject()
        return self

    def save(self, out: Path) -> None:
        if not self.pd.Save(PD_SAVE_FULL, str(out)):
            raise AcrobatError(f"Acrobat could not save {out.name}")

    def __exit__(self, *exc) -> None:
        try:
            if self.pd is not None:
                self.pd.Close()
            if self.app is not None and not self.app.GetNumAVDocs():
                self.app.Exit()
        except Exception:  # closing must not mask the fill's own error
            logger.debug("closing Acrobat failed", exc_info=True)
        finally:
            self.app = self.pd = self.jso = None
            if self._com:
                import pythoncom
                pythoncom.CoUninitialize()


def _acrobat_path(path: Path) -> str:
    """The path as Acrobat JavaScript reports `this.path`: C:\\a\\b.pdf -> /C/a/b.pdf."""
    s = str(path).replace("\\", "/")
    if len(s) > 1 and s[1] == ":":
        return f"/{s[0]}{s[2:]}"
    return s[1:] if s.startswith("//") else s


# One batch of writes, then optionally one choice field's display items (from index 1 until an empty
# one, as pdf_auto's XfaHelper; setItemState uses the same numbering). AFormAut runs in Acrobat's active
# document, so nothing runs unless that is this form: the RCIC may have other documents open.
BATCH_JS = r"""
var expect = %s, ops = %s, read = %s, here = String(this.path);
if (here.toLowerCase() != expect.toLowerCase()) {
  event.value = JSON.stringify({wrong_doc: here});
} else {
  var errors = [], items = null;
  for (var i = 0; i < ops.length; i++) {
    var o = ops[i];
    try {
      var f = xfa.form.resolveNode(o.node);
      if (f == null) { errors.push(o.node + ": not found in the form"); continue; }
      if (o.op == "set") { f.formattedValue = o.value; if (o.event) f.execEvent(o.event); }
      else if (o.op == "event") { f.execEvent(o.event); }
      else if (o.op == "select") { f.setItemState(o.index, true); f.execEvent("exit"); }
    } catch (e) { errors.push(o.node + ": " + e); }
  }
  if (read != null) {
    try {
      var rf = xfa.form.resolveNode(read);
      if (rf != null) {
        items = [];
        for (var k = 1; k < 1000; k++) {
          var it = rf.getDisplayItem(k);
          if (it == null || it === "") break;
          items.push(String(it));
        }
      }
    } catch (e) { items = null; }
  }
  event.value = JSON.stringify({items: items, errors: errors});
}
"""


class AcrobatForm:
    """An IMM form open in Acrobat (AcroExch.AVDoc), written to with JavaScript batches (AFormAut).
    Never kills Acrobat; quits it only if nothing else is open, as the probe does."""

    FOCUS_RETRIES = 3

    def __init__(self, path: Path, dispatch: Callable = _dispatch):
        self.path, self._dispatch = Path(path), dispatch
        self.app = self.av = self.js = None
        self._com = False

    def __enter__(self) -> AcrobatForm:
        # MCP tools run on worker threads, and COM must be initialised on each thread that uses it.
        self._com = self._dispatch is _dispatch and _co_initialize()
        try:
            self.app = self._dispatch("AcroExch.App")
            self.av = self._dispatch("AcroExch.AVDoc")
            if not self.av.Open(str(self.path), ""):
                raise AcrobatError(f"Acrobat could not open {self.path.name}")
            self.js = self._dispatch("AFormAut.App").Fields
        except BaseException:
            self.__exit__()
            raise
        return self

    def run(self, ops: list[dict], read: str | None = None) -> tuple[list[str] | None, list[str]]:
        """Apply `ops` in Acrobat, then read the `read` field's options. Returns (options or None when
        that field doesn't exist, per-op errors)."""
        script = BATCH_JS % (json.dumps(_acrobat_path(self.path)), json.dumps(ops), json.dumps(read))
        for _ in range(self.FOCUS_RETRIES):
            out = json.loads(str(self.js.ExecuteThisJavascript(script)))
            if "wrong_doc" not in out:
                return out.get("items"), out.get("errors", [])
            self.av.BringToFront()   # the RCIC switched documents: make ours active again
            time.sleep(0.2)
        raise AcrobatError(f"Acrobat's active document is {out['wrong_doc']}, not {self.path.name}; "
                           "stopped so no other document is changed")

    def save(self, out: Path) -> None:
        if not self.av.GetPDDoc().Save(PD_SAVE_FULL, str(out)):
            raise AcrobatError(f"Acrobat could not save {out.name}")

    def __exit__(self, *exc) -> None:
        try:
            if self.av is not None:
                self.av.Close(True)   # discard: the filled copy was saved elsewhere
            if self.app is not None and not self.app.GetNumAVDocs():
                self.app.Exit()
        except Exception:  # closing must not mask the fill's own error
            logger.debug("closing Acrobat failed", exc_info=True)
        finally:
            self.app = self.av = self.js = None
            if self._com:
                import pythoncom
                pythoncom.CoUninitialize()


# ---- choice matching (pdf_auto ChoiceMapper) ----

def _normalize(text) -> str:
    if not text:
        return ""
    text = str(text)
    for a, b in (("’", "'"), ("‘", "'"), ("ʼ", "'"), ("`", "'"), ("´", "'"),
                 ("“", '"'), ("”", '"')):
        text = text.replace(a, b)
    return text.lower().strip()


def choice_matches(option: str, value: str, mapper: dict | None) -> bool:
    mapper = mapper or {}
    option = (mapper.get("mapping") or {}).get(option, option)
    option, value = _normalize(option), _normalize(value)
    if option == value:
        return True
    compare = mapper.get("compare", "contains")
    if compare == "equals":
        return False
    if compare == "value_contains":
        return option in value
    return value in option


# ---- replay ----

def replay(form, ops: list, on_progress: Callable[[int, int], None] | None = None) -> list[str]:
    """Apply the recorded ops in order; returns one error string per op that failed.

    Writes queue up and run as one batch; a choice op first runs the queue and reads that field's
    options (earlier writes can change them, e.g. province after country), then matches them here."""
    errors, queue = [], []

    def run(read: str | None = None) -> list[str] | None:
        items, errs = form.run(list(queue), read)
        queue.clear()
        errors.extend(e[:300] for e in errs)
        return items

    for n, op in enumerate(ops, start=1):
        node, kind = op.get("node"), op.get("op")
        if kind == "set":
            value = op.get("value")
            queue.append({"op": "set", "node": node, "value": "" if value is None else str(value),
                          "event": op.get("event")})
        elif kind == "event":
            queue.append({"op": "event", "node": node, "event": op.get("event")})
        elif kind == "choice":
            items = run(read=node)
            if items is None:
                errors.append(f"{node}: not found in the form")
            else:
                index = next((i for i, o in enumerate(items, start=1)
                              if choice_matches(o, op.get("value"), op.get("mapper"))), None)
                if index is None:
                    errors.append(f"{node}: no option matches {op.get('value')!r}")
                else:
                    queue.append({"op": "select", "node": node, "index": index})
        else:
            errors.append(f"{node}: unknown operation {kind!r}")
        if on_progress and n % 50 == 0:
            on_progress(n, len(ops))
    if queue:
        run()
    return errors


def fill(template: Path, ops: list, out: Path, doc_factory: Callable = AcrobatForm,
         on_progress: Callable[[int, int], None] | None = None) -> dict:
    """Open the blank IMM template, replay the ops, save to `out`."""
    with DialogClicker(), doc_factory(template) as doc:
        errors = replay(doc, ops, on_progress)
        doc.save(out)
    return {"applied": len(ops) - len(errors), "failed": len(errors), "errors": errors[:50]}


# ---- Acrobat alert dialogs ----

class DialogClicker:
    """Clicks OK on Acrobat's own alert dialogs (form scripts raise them while filling) so the fill
    doesn't hang. Only dialogs owned by an Acrobat process; no-op off Windows."""

    def __init__(self, interval_s: float = 0.25):
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._thread = None

    def start(self) -> None:
        if sys.platform != "win32":
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="acrobat-dialogs")
        self._thread.start()

    def __enter__(self) -> DialogClicker:
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        try:
            import win32con
            import win32gui
        except ImportError:
            return
        while not self._stop.wait(self.interval_s):
            try:
                win32gui.EnumWindows(lambda hwnd, _: self._maybe_click(hwnd, win32gui, win32con), None)
            except Exception:  # a window closing mid-enumeration; try again next tick
                logger.debug("dialog scan failed", exc_info=True)

    @staticmethod
    def _owner_is_acrobat(hwnd) -> bool:
        import win32api
        import win32con
        import win32process
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        try:
            h = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            try:
                return Path(win32process.GetModuleFileNameEx(h, 0)).name.lower() == "acrobat.exe"
            finally:
                win32api.CloseHandle(h)
        except Exception:  # noqa: BLE001  (the window's process has gone or is protected)
            return False

    def _maybe_click(self, hwnd, win32gui, win32con) -> bool:
        if not win32gui.IsWindowVisible(hwnd) or win32gui.GetClassName(hwnd) != "#32770":
            return True
        if not self._owner_is_acrobat(hwnd):
            return True
        buttons = []

        def collect(child, acc):
            if ("button" in win32gui.GetClassName(child).lower()
                    and win32gui.GetWindowText(child).strip().lower() in ("ok", "&ok")):
                acc.append(child)
            return True
        win32gui.EnumChildWindows(hwnd, collect, buttons)
        if buttons:
            logger.info("Acrobat dialog %r: clicking OK", win32gui.GetWindowText(hwnd))
            win32gui.SendMessage(buttons[0], win32con.BM_CLICK, 0, 0)
            time.sleep(0.1)
        return True
