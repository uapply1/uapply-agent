"""What an MCP tool call works with: settings, the bound client folder and a backend client."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .api import UApplyApi
from .config import Settings
from .folder import WorkingFolder


class ToolError(Exception):
    """A refusal or failure reported to the chat model as {ok: false, error: {code, message, hint}}."""

    def __init__(self, code: str, message: str, hint: str = ""):
        super().__init__(message)
        self.code, self.message, self.hint = code, message, hint

    def as_result(self) -> dict:
        return {"ok": False, "error": {"code": self.code, "message": self.message, "hint": self.hint}}


def ok(**data) -> dict:
    return {"ok": True, **data}


CLAUDE_CODE_CLIENT = "Claude Code"      # what the Claude Code MCP client sends as its name
TASK_RUNNERS = ("auto", "session", "cli")


def runner_mode(setting: str, client_name: str | None) -> str:
    """Where model tasks run: subagents of the connected session ("session", Claude Code only) or a
    headless CLI ("cli")."""
    if setting in ("session", "cli"):
        return setting
    return "session" if (client_name or "").startswith(CLAUDE_CODE_CLIENT) else "cli"


NO_CASE_HINT = ("ask the RCIC: create a new case (list_application_types → confirmation → create_case) "
                "or bind an existing one (they paste the survey id → init_case)")


@dataclass
class ServerContext:
    settings: Settings
    folder: WorkingFolder
    _api: UApplyApi | None = field(default=None, repr=False)

    @classmethod
    def from_environment(cls, folder: Path | str) -> ServerContext:
        return cls(Settings.load(), WorkingFolder(folder))

    @property
    def api(self) -> UApplyApi:
        if self._api is None:
            self._api = UApplyApi(self.settings)
        return self._api

    @property
    def survey_id(self) -> str:
        """The bound case's survey id; raises NO_CASE when the folder is not bound."""
        sid = self.folder.survey_id
        if not sid:
            raise ToolError("NO_CASE", "this folder is not bound to a case", NO_CASE_HINT)
        return sid
