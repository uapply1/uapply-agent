"""XfaFormLib.dll, the COM bridge pdf_auto fills IMM forms with, registered for this PC without regasm.

Writes the keys `regasm /codebase` writes: machine-wide when this process may (an elevated or
built-in Administrator session ignores per-user COM classes), otherwise for the current user.
Both registry views, so a 32- or 64-bit Python finds it.
"""
from __future__ import annotations

import sys
from pathlib import Path

PROG_ID = "XfaFormLib.XfaFormHelper"
CLSID = "{A7DA621C-2CFB-47ED-81F3-2E4C3B79E15D}"
VERSION = "1.0.0.0"
ASSEMBLY = f"XfaFormLib, Version={VERSION}, Culture=neutral, PublicKeyToken=null"
RUNTIME = "v4.0.30319"
MANAGED_CATEGORY = "{62C8FE65-4EBB-45E7-B440-6E39B2CDBF29}"   # ".NET Category", as regasm writes it
DLL = Path(__file__).parent / "bin" / "XfaFormLib.dll"


def entries(codebase: str) -> list[tuple[str, str, str]]:
    """(key under Software\\Classes, value name, value); '' is the default value."""
    clsid = rf"CLSID\{CLSID}"
    inproc = [("", "mscoree.dll"), ("ThreadingModel", "Both"), ("Class", PROG_ID), ("Assembly", ASSEMBLY),
              ("RuntimeVersion", RUNTIME), ("CodeBase", codebase)]
    return [
        (PROG_ID, "", PROG_ID),
        (rf"{PROG_ID}\CLSID", "", CLSID),
        (clsid, "", PROG_ID),
        (rf"{clsid}\ProgId", "", PROG_ID),
        (rf"{clsid}\Implemented Categories\{MANAGED_CATEGORY}", "", ""),
        *((rf"{clsid}\InprocServer32", n, v) for n, v in inproc),
        *((rf"{clsid}\InprocServer32\{VERSION}", n, v) for n, v in inproc if n not in ("", "ThreadingModel")),
    ]


def _registered_codebase(winreg, hive, view) -> str | None:
    try:
        with winreg.OpenKeyEx(hive, rf"Software\Classes\CLSID\{CLSID}\InprocServer32", 0,
                              winreg.KEY_READ | view) as k:
            return winreg.QueryValueEx(k, "CodeBase")[0]
    except OSError:
        return None


def _write(winreg, hive, codebase: str) -> None:
    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        if _registered_codebase(winreg, hive, view) == codebase:
            continue
        for key, name, value in entries(codebase):
            with winreg.CreateKeyEx(hive, rf"Software\Classes\{key}", 0, winreg.KEY_WRITE | view) as k:
                winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)


def ensure_registered(winreg=None, dll: Path = DLL) -> str | None:
    """Register the shipped DLL (again when its path changed after an update). None when registered,
    otherwise why not."""
    if winreg is None:
        if sys.platform != "win32":
            return "XfaFormLib needs Windows"
        import winreg
    if not dll.is_file():
        return f"XfaFormLib.dll is missing from {dll.parent}; reinstall uapply-agent"
    codebase = dll.resolve().as_uri()
    try:
        _write(winreg, winreg.HKEY_LOCAL_MACHINE, codebase)
        return None
    except PermissionError:
        pass                                        # not elevated: register for this user
    except OSError as e:
        return f"could not register XfaFormLib: {e}"[:300]
    try:
        _write(winreg, winreg.HKEY_CURRENT_USER, codebase)
        return None
    except OSError as e:
        return f"could not register XfaFormLib: {e}"[:300]
