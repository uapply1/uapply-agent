"""IMM PDF auto-fill with the RCIC's own Adobe Acrobat Pro (Windows, IAC COM through pywin32).

The backend records each form fill as a list of operations (pdf_auto record mode); this module
replays them in Acrobat exactly as the platform's filler does: `formattedValue` + event, and
choice lists matched with the same ChoiceMapper rules.
"""
from __future__ import annotations

import logging
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

PD_SAVE_FULL = 1


# ---- detection ----

def _registry_has_acrobat() -> Optional[str]:
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
            with AcrobatDoc(pdf) as d:
                if d.jso is None:
                    return {"available": False, "path": path,
                            "reason": "Acrobat is installed but its automation is unavailable (Reader or no Pro licence)"}
        return {"available": True, "path": path, "reason": ""}
    except Exception as e:
        return {"available": False, "path": None, "reason": f"Acrobat automation failed: {e}"[:300]}


# ---- one open document ----

def _dispatch(prog_id: str):
    import win32com.client
    return win32com.client.Dispatch(prog_id)


class AcrobatDoc:
    """AcroExch.PDDoc + its JavaScript object. Never kills Acrobat; quits it only if we started it
    with nothing else open."""

    def __init__(self, path: Path, dispatch: Callable = _dispatch):
        self.path, self._dispatch = Path(path), dispatch
        self.app = self.pd = self.jso = None

    def __enter__(self) -> "AcrobatDoc":
        self.app = self._dispatch("AcroExch.App")
        self.pd = self._dispatch("AcroExch.PDDoc")
        if not self.pd.Open(str(self.path)):
            raise RuntimeError(f"Acrobat could not open {self.path.name}")
        self.jso = self.pd.GetJSObject()
        return self

    def save(self, out: Path) -> None:
        if not self.pd.Save(PD_SAVE_FULL, str(out)):
            raise RuntimeError(f"Acrobat could not save {out.name}")

    def __exit__(self, *exc) -> None:
        try:
            if self.pd is not None:
                self.pd.Close()
            if self.app is not None and not self.app.GetNumAVDocs():
                self.app.Exit()
        except Exception as e:
            logger.debug(f"closing Acrobat: {e}")


# ---- choice matching (pdf_auto ChoiceMapper) ----

def _normalize(text) -> str:
    if not text:
        return ""
    text = str(text)
    for a, b in (("’", "'"), ("‘", "'"), ("ʼ", "'"), ("`", "'"), ("´", "'"),
                 ("“", '"'), ("”", '"')):
        text = text.replace(a, b)
    return text.lower().strip()


def choice_matches(option: str, value: str, mapper: Optional[dict]) -> bool:
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

def _display_items(field) -> list[str]:
    # As pdf_auto's XfaHelper: items from index 1 until an empty one; setItemState uses the same numbering.
    items, i = [], 1
    while i < 1000:
        item = field.getDisplayItem(i)
        if item is None or item == "":
            break
        items.append(str(item))
        i += 1
    return items


def apply_op(xfa, op: dict) -> Optional[str]:
    """Apply one recorded operation; returns an error string or None."""
    node = op.get("node")
    try:
        field = xfa.resolveNode(node)
        if field is None:
            return f"{node}: not found in the form"
        kind = op.get("op")
        if kind == "set":
            value = op.get("value")
            field.formattedValue = "" if value is None else str(value)
            if op.get("event"):
                field.execEvent(op["event"])
        elif kind == "event":
            field.execEvent(op["event"])
        elif kind == "choice":
            for index, option in enumerate(_display_items(field)):
                if choice_matches(option, op.get("value"), op.get("mapper")):
                    field.setItemState(index + 1, True)
                    field.execEvent("exit")
                    return None
            return f"{node}: no option matches {op.get('value')!r}"
        else:
            return f"{node}: unknown operation {kind!r}"
        return None
    except Exception as e:
        return f"{node}: {e}"[:300]


def fill(template: Path, ops: list, out: Path, doc_factory: Callable = AcrobatDoc,
         on_progress: Optional[Callable[[int, int], None]] = None) -> dict:
    """Open the blank IMM template, replay the ops, save to `out`."""
    errors = []
    clicker = DialogClicker()
    clicker.start()
    try:
        with doc_factory(template) as doc:
            if doc.jso is None:
                raise RuntimeError("Acrobat returned no JavaScript object (Reader, or Pro not licensed)")
            xfa = doc.jso.xfa
            for n, op in enumerate(ops, start=1):
                if err := apply_op(xfa, op):
                    errors.append(err)
                if on_progress and n % 50 == 0:
                    on_progress(n, len(ops))
            doc.save(out)
    finally:
        clicker.stop()
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
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

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
            except Exception:
                pass

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
        except Exception:
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
            logger.info(f"Acrobat dialog '{win32gui.GetWindowText(hwnd)}': clicking OK")
            win32gui.SendMessage(buttons[0], win32con.BM_CLICK, 0, 0)
            time.sleep(0.1)
        return True
