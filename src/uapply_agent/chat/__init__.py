"""Optional chat-history intake: fetch a client's conversation from the local chat archive
(AnyChat CLI), derive intake hints with the RCIC's own Claude Code / Codex, and file the transcript
on the case as an Agent Survey document (setting `chat_upload`). The transcript is stored in the
client folder under .uapply/chat/. See docs/architecture/chat-sources.md."""
from .base import Availability, ChatSource, Contact, Transcript

__all__ = ["Availability", "ChatSource", "Contact", "Transcript"]
