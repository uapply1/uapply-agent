# Chat Sources (optional intake from WeChat and other local chat archives)

RCICs talk to clients on WeChat long before documents arrive. `uapply-agent`
can read that history through the **AnyChat** CLI, derive intake hints on the
RCIC's machine, and file the transcript on the case so the normal pipeline
extracts the self-reported facts. Implemented 2026-09-27 (D13).

## What AnyChat is (and is not)

[github.com/jackyzhang69/plugins](https://github.com/jackyzhang69/plugins),
`plugins/anychat`: a Claude Code / Codex plugin whose skill drives a bundled,
closed-source CLI. Facts that shape the integration:

| Fact | Consequence for us |
|---|---|
| Binaries for macOS arm64 and Windows x64 only; no Linux | `chat_sources` reports `unsupported_platform` on Linux; tests use a fake CLI |
| Needs a Portal login (`login --token-stdin`) | we never handle the token; `not_logged_in` points the RCIC at AnyChat's own login |
| Content stays on the machine; the skill forbids touching storage paths | we only run its documented commands: `whoami --json`, `resolve --query`, `query --mode friend --target … --days N --format md -o file` |
| WeChat is the primary source (wxids in the docs); iMessage/Telegram/WhatsApp experimental | v1 uses `--mode friend` only; `--source`/`--all-sources` left for later |
| No published message schema | the markdown export is treated as opaque text |

Discovery order mirrors the plugin: `$ANYCHAT_BIN` →
`~/.jackyzhang.app/plugins/anychat/current/bin/<platform>/anychat` →
`~/.local/bin/anychat` → `PATH`.

## Flow

```
chat_sources          availability (installed? logged in?)
chat_find_contact     anychat resolve → display names only (raw ids never reach the chat model)
chat_fetch(contact, days=365)
   ├─ anychat query … --format md -o .uapply/chat/anychat_<contact>_<from>_<to>.md
   ├─ local intake call (headless runtime, RCIC's plan): transcript as a file the
   │  model Reads + the application-type catalog → IntakeHints → .uapply/chat/*.intake.json
   └─ upload: render a text PDF → bulk_upload as agent_survey (category "other")
              — immediately when a case is bound, else queued in pending_uploads.json
list_application_types
create_case(name, application_type_id, confirmation="create case")
   → POST /api/survey/surveys/ (charges the account) → init_case → flush queued uploads
```

Why a PDF: the backend rejects `.txt`/`.md`; a `.docx` would be extracted
server-side with a Gemini call; a text PDF in local mode goes through
`pdfplumber` on the RCIC's machine — zero model calls for OCR. The pipeline's
`Agent Survey` branch then runs every section (still server-side, see S4) and
analysis ranks `agent_survey` values lowest in conflicts, which is the right
weight for self-reported chat.

## IntakeHints

`chat/intake.py`: applicant identity (Latin and native names, birthdate,
citizenship, contact), family members with relationship, an application guess
(`program`/`visa_type`/`visa_location`, `suggested_application_type_id` chosen
only from the catalog, confidence, rationale), key facts, open questions. The
system prompt is `chat/prompts/intake.md`. Transcripts longer than
`chat_max_chars` (200k) are cut to their most recent part — a v1 limit.

## Consent and privacy

- Fetch only for a contact the RCIC named; the playbook says so and the tool
  takes an explicit name.
- Message bodies and ids never enter the conversation: tool results carry
  counts, paths and the redacted hints (`store.redact` strips `wxid_…` and
  `…@chatroom`).
- `create_case` is refused unless `confirmation` is exactly "create case" or
  "确认创建". This is consent through the model, a deliberate exception to D10
  chosen by the product owner; the approval-page path replaces it when the
  dashboard approvals exist.
- `.uapply/chat/` holds the transcript, the PDF and the hints; `uapply-agent
  clean` does not delete them (they are the record of what was filed).

## Config

`chat_source` (`anychat` | `none`), `anychat_bin`, `chat_default_days` (180),
`chat_max_chars` (200000), `chat_upload` (default true; false keeps the
transcript local and returns hints only — for firms that do not want client
chats in the cloud), `team_id` for `create_survey` (auto-detected when the RCIC
belongs to exactly one team). Env: `ANYCHAT_BIN`, `UAPPLY_CHAT_SOURCE`,
`UAPPLY_TEAM_ID`.

Uploads are deduplicated on the transcript's sha256 (`index.json`), so
re-fetching the same history files nothing twice. `create_case` attaches the
application type's confirmed main/required IMM forms so auto-fill has forms to
fill.

Onboarding note for RCICs: with `chat_upload` on, the client's chat is stored
on uApply and read by the section-extraction step; the RCIC is responsible for
having the client's consent to process it.

## Not done

Other sources (manual exports, AnyChat `--source` for WhatsApp/Telegram), group
chats, media/voice, and running the Agent Survey sections locally.
