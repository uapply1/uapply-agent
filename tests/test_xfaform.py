"""XfaFormLib registration: the keys regasm /codebase writes, without admin."""
from uapply_agent import xfaform


class FakeWinreg:
    HKEY_LOCAL_MACHINE, HKEY_CURRENT_USER = "HKLM", "HKCU"
    KEY_READ, KEY_WRITE, KEY_WOW64_64KEY, KEY_WOW64_32KEY = 1, 2, 256, 512
    REG_SZ = 1

    def __init__(self, deny=()):
        self.values, self.deny, self.writes = {}, set(deny), 0

    class _Key:
        def __init__(self, path):
            self.path = path

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    def _path(self, hive, sub, access):
        return (hive, access & (self.KEY_WOW64_64KEY | self.KEY_WOW64_32KEY), sub.lower())

    def CreateKeyEx(self, hive, sub, _, access):
        if hive in self.deny:
            raise PermissionError("access denied")
        return self._Key(self._path(hive, sub, access))

    def OpenKeyEx(self, hive, sub, _, access):
        path = self._path(hive, sub, access)
        if not any(k[0] == path for k in self.values):
            raise FileNotFoundError(sub)
        return self._Key(path)

    def SetValueEx(self, key, name, _, kind, value):
        self.writes += 1
        self.values[(key.path, name)] = value

    def QueryValueEx(self, key, name):
        return self.values[(key.path, name)], self.REG_SZ

    def get(self, hive, view, sub, name):
        return self.values.get(((hive, view, sub.lower()), name))


def dll(tmp_path, name="XfaFormLib.dll"):
    p = tmp_path / name
    p.write_bytes(b"MZ")
    return p


def test_registers_machine_wide_in_both_views(tmp_path):
    reg, d = FakeWinreg(), dll(tmp_path)
    assert xfaform.ensure_registered(reg, d) is None
    inproc = rf"Software\Classes\CLSID\{xfaform.CLSID}\InprocServer32"
    for view in (reg.KEY_WOW64_64KEY, reg.KEY_WOW64_32KEY):
        assert reg.get("HKLM", view, inproc, "") == "mscoree.dll"
        assert reg.get("HKLM", view, inproc, "CodeBase") == d.resolve().as_uri()
        assert reg.get("HKLM", view, inproc, "Class") == "XfaFormLib.XfaFormHelper"
        assert reg.get("HKLM", view, r"Software\Classes\XfaFormLib.XfaFormHelper\CLSID", "") == xfaform.CLSID


def test_falls_back_to_the_current_user_without_admin(tmp_path):
    reg = FakeWinreg(deny={"HKLM"})
    assert xfaform.ensure_registered(reg, dll(tmp_path)) is None
    assert reg.get("HKCU", reg.KEY_WOW64_64KEY, rf"Software\Classes\CLSID\{xfaform.CLSID}", "") == xfaform.PROG_ID


def test_idempotent_and_rewritten_when_the_dll_moves(tmp_path):
    reg, d = FakeWinreg(), dll(tmp_path)
    xfaform.ensure_registered(reg, d)
    writes = reg.writes
    xfaform.ensure_registered(reg, d)
    assert reg.writes == writes                                              # nothing to do
    (tmp_path / "v2").mkdir()
    moved = dll(tmp_path / "v2")
    xfaform.ensure_registered(reg, moved)
    inproc = rf"Software\Classes\CLSID\{xfaform.CLSID}\InprocServer32"
    assert reg.get("HKLM", reg.KEY_WOW64_64KEY, inproc, "CodeBase") == moved.resolve().as_uri()


def test_missing_dll_and_non_windows_are_reported(tmp_path, monkeypatch):
    assert "missing" in xfaform.ensure_registered(FakeWinreg(), tmp_path / "XfaFormLib.dll")
    monkeypatch.setattr(xfaform.sys, "platform", "linux")
    assert xfaform.ensure_registered() == "XfaFormLib needs Windows"


def test_the_shipped_dll_is_in_the_package():
    assert xfaform.DLL.is_file() and xfaform.DLL.read_bytes()[:2] == b"MZ"
