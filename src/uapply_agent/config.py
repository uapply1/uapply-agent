"""Settings (~/.config/uapply-agent/config.json) and credential storage."""
from __future__ import annotations

import json
import logging
import os
import stat
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from .util import write_text_atomic

logger = logging.getLogger(__name__)

APP = "uapply-agent"
KEYRING_USER = "access_token"


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(Path.home(), ".config")
    return Path(base) / APP


@dataclass
class Settings:
    # Production defaults: the same Auth0 native client the desktop app uses
    # (Device Code + Refresh Token grants enabled). `config --set` switches tenants.
    backend_url: str = "https://api.uapply.io"
    app_url: str = ""              # dashboard; empty = derived from backend_url (api.x → app.x)
    auth0_domain: str = "uapply-prod-tenant.us.auth0.com"
    auth0_client_id: str = "q5ByF8FNi6byld0JmnkW2CkcWpHuzxXd"
    auth0_audience: str = "https://uapply.io"
    runtime: str = "auto"          # auto | claude-code | codex
    model: str = ""                # runtime default when empty
    installed_sha: str = ""        # commit the installer put in ~/.local/bin (versions/<sha>/ carry their own)
    auto_update: bool = True       # check GitHub main at `mcp` / `run` start and switch to the newest build
    claude_bin: str = ""           # absolute paths recorded by `setup`; GUI apps run with a minimal PATH
    codex_bin: str = ""
    workers: int = 2
    force_ocr: bool = False        # ignore PDF text layers and always OCR with the model
    team_id: str = ""              # team to create cases in (business accounts)
    chat_source: str = "anychat"   # anychat | none
    anychat_bin: str = ""          # override the AnyChat CLI location
    chat_default_days: int = 180
    chat_upload: bool = True       # file transcripts on the case (False: local hints only)
    chat_max_chars: int = 200_000  # transcript cap for the local intake call
    extra: dict = field(default_factory=dict)

    @classmethod
    def load(cls) -> "Settings":
        s = cls()
        p = config_dir() / "config.json"
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            for k, v in data.items():
                if hasattr(s, k):
                    setattr(s, k, v)
        # Environment overrides make CI and tests easy.
        for k, env in (("backend_url", "UAPPLY_BACKEND_URL"), ("app_url", "UAPPLY_APP_URL"), ("runtime", "UAPPLY_RUNTIME"),
                       ("model", "UAPPLY_MODEL")):
            if os.environ.get(env):
                setattr(s, k, os.environ[env])
        for k, env in (("team_id", "UAPPLY_TEAM_ID"), ("anychat_bin", "ANYCHAT_BIN"), ("chat_source", "UAPPLY_CHAT_SOURCE"),
                       ("claude_bin", "UAPPLY_CLAUDE_BIN"), ("codex_bin", "UAPPLY_CODEX_BIN")):
            if os.environ.get(env):
                setattr(s, k, os.environ[env])
        if os.environ.get("UAPPLY_FORCE_OCR"):
            s.force_ocr = os.environ["UAPPLY_FORCE_OCR"].lower() in ("1", "true", "yes")
        s.backend_url = s.backend_url.rstrip("/")
        return s

    @property
    def dashboard_url(self) -> str:
        if self.app_url:
            return self.app_url.rstrip("/")
        from urllib.parse import urlsplit
        u = urlsplit(self.backend_url)
        host = u.hostname or ""
        if host in ("localhost", "127.0.0.1"):
            return f"{u.scheme}://{host}:8080"
        if host.startswith("api."):
            return f"{u.scheme}://app.{host[4:]}"
        return f"{u.scheme}://{host}"

    def save(self) -> None:
        d = config_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.json").write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")


class Credentials:
    """Access and refresh tokens in the OS keychain; a 0600 file when no keychain is available.
    `UAPPLY_TOKEN` overrides the stored access token (CI, tests)."""

    ACCESS, REFRESH = KEYRING_USER, "refresh_token"

    @staticmethod
    def _file() -> Path:
        return config_dir() / "credentials.json"

    @classmethod
    def _read_file(cls) -> dict:
        p = cls._file()
        try:
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        except (OSError, json.JSONDecodeError):
            logger.warning("ignoring unreadable %s", p)
            return {}

    @classmethod
    def _get(cls, key: str) -> Optional[str]:
        try:
            import keyring
            value = keyring.get_password(APP, key)
            if value:
                return value
        except Exception:  # no usable keychain backend on this machine
            logger.debug("keyring read failed", exc_info=True)
        return cls._read_file().get("access_token" if key == cls.ACCESS else "refresh_token")

    @classmethod
    def get_token(cls) -> Optional[str]:
        return os.environ.get("UAPPLY_TOKEN") or cls._get(cls.ACCESS)

    @classmethod
    def get_refresh_token(cls) -> Optional[str]:
        return cls._get(cls.REFRESH)

    @classmethod
    def set_token(cls, token: str, refresh_token: Optional[str] = None) -> str:
        """Store a new login. Without a refresh token any previous one is removed, so a pasted
        token can never be silently refreshed back into an earlier account."""
        try:
            import keyring
            keyring.set_password(APP, cls.ACCESS, token)
            if refresh_token:
                keyring.set_password(APP, cls.REFRESH, refresh_token)
            else:
                cls._delete_keyring(cls.REFRESH)
            return "keyring"
        except Exception:
            logger.debug("keyring write failed, using the credentials file", exc_info=True)
        d = config_dir()
        d.mkdir(parents=True, exist_ok=True)
        p = cls._file()
        write_text_atomic(p, json.dumps({"access_token": token, "refresh_token": refresh_token}))
        p.chmod(stat.S_IRUSR | stat.S_IWUSR)
        return str(p)

    @staticmethod
    def _delete_keyring(key: str) -> None:
        try:
            import keyring
            keyring.delete_password(APP, key)
        except Exception:  # absent entry or no backend
            logger.debug("keyring delete of %s skipped", key, exc_info=True)

    @classmethod
    def clear(cls) -> None:
        cls._delete_keyring(cls.ACCESS)
        cls._delete_keyring(cls.REFRESH)
        cls._file().unlink(missing_ok=True)
