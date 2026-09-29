"""One local headless call: transcript → intake hints. Runs on the RCIC's plan."""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from ..runners.base import Runner
from .base import Transcript

PROMPT_PATH = Path(__file__).parent / "prompts" / "intake.md"


class Applicant(BaseModel):
    family_name: str | None = None
    given_name: str | None = None
    native_name: str | None = None
    birthdate: str | None = None
    gender: str | None = None
    citizenship: str | None = None
    email: str | None = None
    phone: str | None = None
    current_country: str | None = None
    current_status: str | None = None


class FamilyMember(BaseModel):
    name: str
    relationship: str
    note: str | None = None


class ApplicationGuess(BaseModel):
    program: str | None = None
    visa_type: str | None = None
    visa_location: str | None = None
    suggested_application_type_id: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    rationale: str | None = None


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
    note = f"(earlier messages omitted; showing the most recent {max_chars} characters)"
    out.write_text(f"{note}\n\n{cut}", encoding="utf-8")
    return out


def intake_prompts(transcript: Transcript, application_types: list[dict], cwd: Path,
                   max_chars: int = 200_000) -> tuple[str, str, Path]:
    """(system prompt, user prompt, transcript file to read). The catalog goes in the prompt; the
    transcript is a file the model reads."""
    src = cap_transcript(transcript.path, max_chars, cwd)
    catalog = "\n".join(
        f"- id={t.get('id')} | {t.get('name')} | program={t.get('program')} visa_type={t.get('visa_type')} "
        f"visa_location={t.get('visa_location')}" for t in application_types) or "- (no catalog provided)"
    user_prompt = (
        f"Chat history between the RCIC (consultant) and the client '{transcript.contact}' "
        f"({transcript.date_from} to {transcript.date_to}) is in the file {src.name} in the current directory. "
        f"Read it fully, then answer.\n\n"
        f"Speakers: messages from '{transcript.contact}' are the client's own statements; every other speaker "
        f"is the consultant. Record only what the client states about themselves; the consultant's suggestions, "
        f"quotes or options are not client facts.\n\n"
        f"Application type catalog (choose suggested_application_type_id from these ids only):\n{catalog}"
    )
    return system_prompt(), user_prompt, src


def validate_hints(raw: dict, application_types: list[dict]) -> IntakeHints:
    """Hints as the model returned them, with a suggested type outside the catalog dropped."""
    hints = IntakeHints.model_validate(raw)
    known = {str(t.get("id")) for t in application_types}
    if hints.application.suggested_application_type_id not in known:
        hints.application.suggested_application_type_id = None
    return hints


def run_intake(runner: Runner, transcript: Transcript, application_types: list[dict], cwd: Path,
               max_chars: int = 200_000) -> tuple[IntakeHints, dict]:
    """One headless call (task_runner=cli). Returns (hints, usage)."""
    system, user, src = intake_prompts(transcript, application_types, cwd, max_chars)
    rr = runner.run(system_prompt=system, user_prompt=user, schema=IntakeHints.model_json_schema(),
                    images=[], cwd=cwd, text_files=[src])
    return validate_hints(rr.output, application_types), rr.usage or {}
