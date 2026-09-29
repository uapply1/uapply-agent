"""Values shared with the uApply backend (document statuses) and the file types the agent handles."""
from __future__ import annotations

from enum import StrEnum


class DocStatus(StrEnum):
    UPLOADED = "uploaded"
    STARTED = "started"
    ANALYZING = "analyzing"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"


RUNNING_STATUSES = frozenset({DocStatus.STARTED, DocStatus.ANALYZING})
FINISHED_STATUSES = frozenset({DocStatus.COMPLETED, DocStatus.FAILED, DocStatus.STOPPED})
STARTABLE_STATUSES = frozenset({DocStatus.UPLOADED, DocStatus.FAILED, DocStatus.STOPPED})
DONE_ANALYSIS = frozenset({"completed", "failed"})

HEIC_EXTENSIONS = frozenset({".heic", ".heif"})
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp"}) | HEIC_EXTENSIONS
PREVIEWABLE_IMAGES = IMAGE_EXTENSIONS | {".gif", ".bmp", ".tif", ".tiff"}
# What the backend accepts as case documents.
UPLOADABLE_EXTENSIONS = frozenset({".pdf", ".jpg", ".jpeg", ".png", ".docx", ".doc", ".xlsx", ".xls"}) | HEIC_EXTENSIONS
