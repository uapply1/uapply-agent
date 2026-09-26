"""Settings (~/.config/uapply-agent/config.json) and credential storage."""
from __future__ import annotations

import json
import os
import stat
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

APP = "uapply-agent"
KEYRING_USER = "access_token"


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(Path.home(), ".config")
    return Path(base) / APP


@dataclass
class Settings:
    backend_url: str = "https://api.uapply.io"
    auth0_domain: str = ""
    auth0_client_id: str = ""
    auth0_audience: str = ""
    runtime: str = "auto"          # auto | claude-code | codex
    model: str = ""                # runtime default when empty
    workers: int = 2
    force_ocr: bool = False        # ignore PDF text layers and always OCR with the model
    extra: dict = field(default_factory=dict)

    @classmethod
    def load(cls) -> "Settings":
        s = cls()
        p = config_dir() / "config.json"
        if p.exists():
            data = json.loads(p.read_text())
            for k, v in data.items():
                if hasattr(s, k):
                    setattr(s, k, v)
        # Environment overrides make CI and tests easy.
        for k, env in (("backend_url", "UAPPLY_BACKEND_URL"), ("runtime", "UAPPLY_RUNTIME"), ("model", "UAPPLY_MODEL")):
            if os.environ.get(env):
                setattr(s, k, os.environ[env])
        if os.environ.get("UAPPLY_FORCE_OCR"):
            s.force_ocr = os.environ["UAPPLY_FORCE_OCR"].lower() in ("1", "true", "yes")
        s.backend_url = s.backend_url.rstrip("/")
        return s

    def save(self) -> None:
        d = config_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.json").write_text(json.dumps(asdict(self), indent=2))


class Credentials:
    """Access token in the OS keychain; a 0600 file when no keychain is available."""

    @staticmethod
    def _file() -> Path:
        return config_dir() / "credentials.json"

    @classmethod
    def get_token(cls) -> Optional[str]:
        if os.environ.get("UAPPLY_TOKEN"):
            return os.environ["UAPPLY_TOKEN"]
        try:
            import keyring
            tok = keyring.get_password(APP, KEYRING_USER)
            if tok:
                return tok
        except Exception:
            pass
        p = cls._file()
        if p.exists():
            return json.loads(p.read_text()).get("access_token")
        return None

    @classmethod
    def set_token(cls, token: str, refresh_token: Optional[str] = None) -> str:
        try:
            import keyring
            keyring.set_password(APP, KEYRING_USER, token)
            if refresh_token:
                keyring.set_password(APP, "refresh_token", refresh_token)
            return "keyring"
        except Exception:
            pass
        d = config_dir()
        d.mkdir(parents=True, exist_ok=True)
        p = cls._file()
        p.write_text(json.dumps({"access_token": token, "refresh_token": refresh_token}))
        p.chmod(stat.S_IRUSR | stat.S_IWUSR)
        return str(p)

    @classmethod
    def clear(cls) -> None:
        try:
            import keyring
            keyring.delete_password(APP, KEYRING_USER)
        except Exception:
            pass
        p = cls._file()
        if p.exists():
            p.unlink()
