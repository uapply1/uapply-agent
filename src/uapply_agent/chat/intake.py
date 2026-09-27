"""One local headless call: transcript → intake hints. Runs on the RCIC's plan."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from ..runners.base import Runner
from .base import Transcript

PROMPT_PATH = Path(__file__).parent / "prompts" / "intake.md"


class Applicant(BaseModel):
    family_name: Optional[str] = None
    given_name: Optional[str] = None
    native_name: Optional[str] = None
    birthdate: Optional[str] = None
    gender: Optional[str] = None
    citizenship: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    current_country: Optional[str] = None
    current_status: Optional[str] = None


class FamilyMember(BaseModel):
    name: str
    relationship: str
    note: Optional[str] = None


class ApplicationGuess(BaseModel):
    program: Optional[str] = None
    visa_type: Optional[str] = None
    visa_location: Optional[str] = None
    suggested_application_type_id: Optional[str] = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    rationale: Optional[str] = None


class IntakeHints(BaseModel):
    applicant: Applicant = Field(default_factory=Applicant)
    family: list[FamilyMember] = Field(default_factory=list)
    application: ApplicationGuess = Field(default_factory=ApplicationGuess)
    key_facts: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


def system_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def cap_transcript(md_path: Path, max_chars: int, cwd: Path) -> Path:
    """Keep the most recent part of a long transcript; the model reads the file, not argv."""
    text = md_path.read_text(encoding="utf-8", errors="replace")
    if len(text) <= max_chars:
        return md_path
    cut = text[-max_chars:]
    cut = cut[cut.find("\n") + 1:]  # start on a whole line
    out = cwd / (md_path.stem + ".recent.md")
    cwd.mkdir(parents=True, exist_ok=True)
    out.write_text(f"(earlier messages omitted; showing the most recent {max_chars} characters)\n\n" + cut, encoding="utf-8")
    return out


def run_intake(runner: Runner, transcript: Transcript, application_types: list[dict], cwd: Path,
               max_chars: int = 200_000) -> tuple[IntakeHints, dict]:
    """Returns (hints, usage). The catalog goes in the prompt; the transcript as a file to read."""
    src = cap_transcript(transcript.path, max_chars, cwd)
    catalog = "\n".join(
        f"- id={t.get('id')} | {t.get('name')} | program={t.get('program')} visa_type={t.get('visa_type')} "
        f"visa_location={t.get('visa_location')}" for t in application_types) or "- (no catalog provided)"
    user_prompt = (
        f"Chat history between the RCIC and the client '{transcript.contact}' "
        f"({transcript.date_from} to {transcript.date_to}) is in the file {src.name} in the current directory. "
        f"Read it fully, then answer.\n\nApplication type catalog (choose suggested_application_type_id from these ids only):\n{catalog}"
    )
    rr = runner.run(system_prompt=system_prompt(), user_prompt=user_prompt, schema=IntakeHints.model_json_schema(),
                    images=[], cwd=cwd, text_files=[src])
    hints = IntakeHints.model_validate(rr.output)
    if hints.application.suggested_application_type_id and hints.application.suggested_application_type_id not in {
            str(t.get("id")) for t in application_types}:
        hints.application.suggested_application_type_id = None
    return hints, rr.usage or {}
