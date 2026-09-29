# Chat Sources (optional intake from WeChat and other local chat archives)

RCICs talk to clients on WeChat long before documents arrive. `uapply-agent`
can read that history through the **AnyChat** CLI, save it in the client
folder, derive intake hints with the RCIC's own Claude Code / Codex, and file
the transcript on the case so the normal pipeline extracts the self-reported
facts (D13).

## What AnyChat is (and is not)

[github.com/jackyzhang69/plugins](https://github.com/jackyzhang69/plugins),
`plugins/anychat`: a Claude Code / Codex plugin whose skill drives a bundled,
closed-source CLI. Facts that shape the integration:

| Fact | Consequence for us |
|---|---|
| Binaries for macOS arm64 and Windows x64 only; no Linux | `chat_sources` reports `unsupported_platform` on Linux; tests use a fake CLI |
| Needs a Portal login (`login --token-stdin`) | we never handle the token; `not_logged_in` points the RCIC at AnyChat's own login |
| The CLI reads the local chat archive; the skill forbids touching its storage paths | we only run its documented commands: `whoami --json`, `resolve --query`, `query --mode friend --target … --days N --format md -o file` |
| WeChat is the primary source (wxids in the docs); iMessage/Telegram/WhatsApp experimental | v1 uses `--mode friend` only; `--source`/`--all-sources` left for later |
| No published message schema | the markdown export is treated as opaque text |

Discovery order mirrors the plugin: `$ANYCHAT_BIN` →
`~/.jackyzhang.app/plugins/anychat/current/bin/<platform>/anychat` →
`~/.local/bin/anychat` → `PATH`.

## Flow

```
chat_sources          availability (installed? logged in?)
chat_find_contact     anychat resolve → display names only (raw ids never reach the chat model)
chat_fetch(contact, days=180)   # default: setting chat_default_days
   ├─ anychat query … --format md -o .uapply/chat/anychat_<contact>_<from>_<to>.md
   ├─ intake call: the transcript and the application-type catalog go to the RCIC's
   │  Claude Code / Codex (headless, RCIC's plan) → IntakeHints → .uapply/chat/*.intake.json
   └─ upload (unless chat_upload=false): render a text PDF → bulk_upload as agent_survey
              (category "other") — immediately when a case is bound, else queued in
              .uapply/chat/pending_uploads.json
list_application_types
create_case(name, application_type_id, confirmation="create case")
   → POST /api/survey/surveys/ (charges the account) → bind the folder → file queued uploads
```

Why a PDF: the backend rejects `.txt`/`.md` uploads, and a text PDF in local
mode goes through `pdfplumber` on the RCIC's machine, with no model call for
OCR. The pipeline's Agent Survey sections then run like any other model call
of a local case, on the RCIC's plan (D14), and analysis ranks `agent_survey`
values lowest in conflicts, which is the right weight for self-reported chat.

## IntakeHints

`chat/intake.py`: applicant identity (Latin and native names, birthdate,
citizenship, contact), family members with relationship, an application guess
(`program`/`visa_type`/`visa_location`, `suggested_application_type_id` chosen
only from the catalog, confidence, rationale), key facts, open questions. The
system prompt is `chat/prompts/intake.md`. Transcripts longer than
`chat_max_chars` (200,000) are cut to their most recent part.

## Data flow and consent

- Fetch only for a contact the RCIC named; the playbook says so and the tool
  takes an explicit name.
- Where the transcript goes:
  1. **Client folder.** `.uapply/chat/` holds the transcript (Markdown), its
     PDF copy, the intake hints, `index.json` and `pending_uploads.json`.
     `uapply-agent clean` does not delete them (they are the record of what
     was filed).
  2. **The RCIC's runtime.** The intake call sends the transcript (up to
     `chat_max_chars`) to the RCIC's Claude Code / Codex, under the RCIC's
     account and terms.
  3. **The uApply case**, by default. The PDF is filed as an Agent Survey
     document and processed by the pipeline. `uapply-agent config --set
     chat_upload=false` keeps transcripts in the folder and returns hints only.
- Message bodies are not returned into the chat conversation: tool results
  carry counts, paths and the hints. `strip_chat_ids` replaces raw WeChat
  account ids (`wxid_…`) and group ids (`…@chatroom`) in tool results; names
  and message text are unchanged, including in the transcript and the PDF.
- `create_case` is refused unless `confirmation` is exactly "create case" or
  "确认创建", which the model passes only after the RCIC picked "Create case" in
  the question tool or typed those words. This consent travels through the
  model; see [guardrails.md](../design/guardrails.md#case-creation-consent).
  A dashboard approval page is planned to replace it (D10).

## Config

`chat_source` (`anychat` | `none`), `anychat_bin`, `chat_default_days` (180),
`chat_max_chars` (200000), `chat_upload` (default true; false keeps the
transcript in the client folder and returns hints only, for firms that do not
want client chats stored on uApply), `team_id` for `create_survey` (auto-detected when the RCIC
belongs to exactly one team). Env: `ANYCHAT_BIN`, `UAPPLY_CHAT_SOURCE`,
`UAPPLY_TEAM_ID`.

Uploads are deduplicated on the transcript's sha256 (`index.json`), so
re-fetching the same history files nothing twice. `create_case` attaches the
application type's default IMM forms (confirmed main and required forms) so
auto-fill has forms to fill.

Onboarding note for RCICs: the transcript is always read by the RCIC's own
runtime, and with `chat_upload` on it is also stored on uApply and read by the
section-extraction step. The RCIC is responsible for having the client's
consent to process it.

## Not implemented

Other sources (manual exports, AnyChat `--source` for WhatsApp/Telegram), group
chats, media/voice, and running the Agent Survey sections locally.
