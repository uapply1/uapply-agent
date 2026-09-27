from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol


class ChatError(RuntimeError):
    def __init__(self, code: str, message: str, hint: str = ""):
        super().__init__(message)
        self.code, self.hint = code, hint


@dataclass
class Availability:
    source: str
    ok: bool
    state: str            # ok | not_installed | unsupported_platform | not_logged_in | cli_error | disabled
    detail: str = ""
    hint: str = ""

    def as_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class Contact:
    display_name: str
    kind: str = "friend"  # friend | group
    raw_id: Optional[str] = None   # never shown to the chat model

    def public(self) -> dict:
        return {"display_name": self.display_name, "kind": self.kind}


@dataclass
class Transcript:
    source: str
    contact: str
    date_from: str
    date_to: str
    path: Path                     # markdown transcript
    message_count: int = 0
    chars: int = 0
    meta: dict = field(default_factory=dict)

    def public(self) -> dict:
        return {"source": self.source, "contact": self.contact, "from": self.date_from, "to": self.date_to,
                "path": str(self.path), "messages": self.message_count, "chars": self.chars}


class ChatSource(Protocol):
    name: str

    def available(self) -> Availability: ...
    def resolve(self, name: str) -> list[Contact]: ...
    def fetch(self, contact: str, days: int, out_dir: Path) -> Transcript: ...
