"""IMM PDF auto-fill with the RCIC's own Adobe Acrobat Pro (Windows).

The backend records each form fill as a list of operations (pdf_auto record mode); this module
replays them through XfaFormLib.dll with the same calls as pdf_auto's XfaHelper: `formattedValue`
+ event, and choice lists matched with the same ChoiceMapper rules.
"""
from __future__ import annotations

import logging
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

from . import xfaform

logger = logging.getLogger(__name__)

PD_SAVE_FULL = 1
SET_PROPERTY, INVOKE_METHOD = 8192, 256    # System.Reflection.BindingFlags, as pdf_auto's XfaHelper
NO_AUTOMATION = "Acrobat is installed but its automation is unavailable (Reader, or no Pro licence)"


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
        if err := xfaform.ensure_registered() or _xfaform_load_error():
            return {"available": False, "path": path, "reason": err}
        return {"available": True, "path": path, "reason": ""}
    except Exception as e:  # noqa: BLE001  (COM/registry errors of any kind mean "not usable")
        return {"available": False, "path": None, "reason": f"Acrobat automation failed: {e}"[:300]}


def _xfaform_load_error() -> str | None:
    """Loads XfaFormLib through COM, as a fill will (needs .NET Framework 4)."""
    _co_initialize()
    try:
        _dispatch(xfaform.PROG_ID)
        return None
    except Exception as e:  # noqa: BLE001  (any COM/.NET error means the fill cannot run)
        return f"XfaFormLib did not load: {e}"[:300]
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


class XfaForm:
    """An IMM form opened through XfaFormLib.XfaFormHelper, the COM DLL pdf_auto fills with. Unlike
    pdf_auto it never kills Acrobat; the DLL's Close() quits it, which Acrobat declines while the
    RCIC has documents open."""

    def __init__(self, path: Path, dispatch: Callable = _dispatch):
        self.path, self._dispatch = Path(path), dispatch
        self.helper = None
        self._com = False

    def __enter__(self) -> XfaForm:
        if self._dispatch is _dispatch:
            if err := xfaform.ensure_registered():   # again after an update moved the DLL
                raise AcrobatError(err)
            self._com = _co_initialize()
        try:
            self.helper = self._dispatch(xfaform.PROG_ID)
            if not self.helper.Open(str(self.path)):
                raise AcrobatError(f"Acrobat could not open {self.path.name}")
        except BaseException:
            self.__exit__()
            raise
        return self

    def resolve(self, node: str):
        return self.helper.ResolveNode(node)

    def invoke(self, obj, member: str, args: list, flags: int):
        return self.helper.InvokeMember(obj, member, args, flags)

    def save(self, out: Path) -> None:
        if not self.helper.Save(str(out)):
            raise AcrobatError(f"Acrobat could not save {out.name}")

    def __exit__(self, *exc) -> None:
        try:
            if self.helper is not None:
                self.helper.Close()
        except Exception:  # closing must not mask the fill's own error
            logger.debug("closing Acrobat failed", exc_info=True)
        finally:
            self.helper = None
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

def _display_items(form: XfaForm, field) -> list[str]:
    # As pdf_auto's XfaHelper: items from index 1 until an empty one; setItemState uses the same numbering.
    items, i = [], 1
    while i < 1000:
        item = form.invoke(field, "getDisplayItem", [i], INVOKE_METHOD)
        if item is None or item == "":
            break
        items.append(str(item))
        i += 1
    return items


def apply_op(form: XfaForm, op: dict) -> str | None:
    """Apply one recorded operation; returns an error string or None."""
    node = op.get("node")
    try:
        field = form.resolve(node)
        if field is None:
            return f"{node}: not found in the form"
        kind = op.get("op")
        if kind == "set":
            value = op.get("value")
            form.invoke(field, "formattedValue", ["" if value is None else str(value)], SET_PROPERTY)
            if op.get("event"):
                form.invoke(field, "execEvent", [op["event"]], INVOKE_METHOD)
        elif kind == "event":
            form.invoke(field, "execEvent", [op["event"]], INVOKE_METHOD)
        elif kind == "choice":
            for index, option in enumerate(_display_items(form, field)):
                if choice_matches(option, op.get("value"), op.get("mapper")):
                    form.invoke(field, "setItemState", [index + 1, True], INVOKE_METHOD)
                    form.invoke(field, "execEvent", ["exit"], INVOKE_METHOD)
                    return None
            return f"{node}: no option matches {op.get('value')!r}"
        else:
            return f"{node}: unknown operation {kind!r}"
        return None
    except Exception as e:  # noqa: BLE001  (a COM error on one field must not stop the form)
        return f"{node}: {e}"[:300]


def fill(template: Path, ops: list, out: Path, doc_factory: Callable = XfaForm,
         on_progress: Callable[[int, int], None] | None = None) -> dict:
    """Open the blank IMM template, replay the ops, save to `out`."""
    errors = []
    with DialogClicker(), doc_factory(template) as doc:
        for n, op in enumerate(ops, start=1):
            if err := apply_op(doc, op):
                errors.append(err)
            if on_progress and n % 50 == 0:
                on_progress(n, len(ops))
        doc.save(out)
    return {"applied": len(ops) - len(errors), "failed": len(errors), "errors": errors[:50]}


# ---- Acrobat alert dialogs ----

class DialogClicker:
    """Clicks OK on Acrobat's own alert dialogs (form scripts raise them while filling) so the fill
    doesn't hang. Only dialogs owned by an Acrobat process; no-op off Windows."""

    def __init__(self, interval_s: float = 0.5):
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
