"""Which tasks a smaller model can run: JSON-to-JSON reformat and translate calls. Everything else
(OCR, classification, section and analysis prompts) stays on the runtime's own model."""
from __future__ import annotations

LIGHT = "light"
STANDARD = "standard"
LIGHT_PURPOSES = {"reformat_extract", "reformat_translate"}


def tier(task: dict) -> str:
    payload = task.get("payload") or {}
    return LIGHT if task.get("kind") == "llm_call" and payload.get("purpose") in LIGHT_PURPOSES else STANDARD
