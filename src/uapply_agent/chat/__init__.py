"""Optional chat-history intake: fetch a client's conversation from a local chat
archive (AnyChat CLI), derive intake hints locally, and file the transcript on
the case as an agent_survey document. See docs/architecture/chat-sources.md."""
from .base import Availability, ChatSource, Contact, Transcript

__all__ = ["Availability", "ChatSource", "Contact", "Transcript"]
