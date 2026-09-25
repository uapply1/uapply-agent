# Case Workflow

The six stages an agent drives, the tools it uses at each, what the backend
does, and where the RCIC must be asked. Tool names are defined in
[reference/mcp-tools.md](../reference/mcp-tools.md); endpoints in
[reference/backend-api.md](../reference/backend-api.md).

Gate legend: 🧑 = stop and ask the RCIC; 🤖 = agent decides and logs rationale.

```
intake ─▶ upload ─▶ classify ─▶ extract ─▶ resolve ─▶ auto-fill
  🧑                   🤖/🧑                 🤖/🧑        🧑
```

Every stage is re-runnable on the same folder; see
[working-folder.md](working-folder.md) for how state is kept.

---

## 1. Intake — folder → case

**Agent**

1. `scan_folder()` — lists candidate files (pdf, jpg/png/heic, docx, xlsx),
   sizes, hashes; ignores `.uapply/`, hidden files, and already-manifested
   hashes.
2. If `.uapply/case.json` exists → resume; skip to the first incomplete stage.
3. Otherwise infer a proposal from filenames and, if needed, a quick look at
   1–2 obvious identity documents (this look **is** an LLM call on the local
   plan, so keep it to the minimum): applicant name(s), likely
   `ApplicationType` (program / `visa_type` / `visa_location`), family members
   and relationships.
4. `list_application_types()` to map the proposal to real ids.
5. 🧑 **Gate:** `propose_case(...)` — the server stores the proposal and
   returns an approval URL; the agent shows the summary in chat, the RCIC
   opens the page and approves, edits or rejects. Never create a case on a
   guess; the agent cannot — only the approval page can.
6. `wait_for_approval(id)` → on `approved` the case exists; the tool writes
   `case.json`. Later family members go through `add_dependent` the same way.

**Backend** — *(new)* `POST /agent/approvals/` (`kind=create_case`); on the
RCIC's decision the server itself calls the existing `POST /survey/surveys/`
(with `llm_mode=local_agent`) and `add_dependent/`.

---

## 2. Upload — files → Documents

**Agent**

1. For each unmanifested file, decide the `applicant` (principal or which
   dependent) and a provisional `document_category` /
   `document_type` from the filename or the intake look. When unsure, upload
   under the generic "agent_survey" type and let classification fix it.
2. Convert HEIC → JPEG locally before upload (existing server path handles
   HEIC, but doing it locally keeps the file the agent later reads identical to
   the one on the server).
3. `sync_documents(paths, applicant, category?, type?)` — the MCP server hashes,
   skips known hashes, calls `bulk_upload`, and records `sha256 → document_id`
   in `manifest.json`.

**Backend** — `POST /survey/documents/bulk_upload/`. *(new)* accepts an
`Idempotency-Key` header and a per-file `sha256` so a retried upload after a
network failure creates no duplicates.

No gate. Uploading to the RCIC's own uApply account is the expected side effect
of the run.

---

## 3. Classify — Documents → typed, readable Documents

**Agent**

1. `start_processing(document_ids?)` → backend starts `process_document`
   workflows in local mode.
2. `run_tasks()` — the executor pulls each task, runs it in a fresh headless
   runtime and submits (see [runtime-modes.md](runtime-modes.md)). Task kinds
   here:
   - `extract_content` — render pages locally, run the OCR/vision prompt, return
     page-marked text (`## 第N页` markers preserved so downstream page
     resolution keeps working). PDFs with a real text layer skip the model:
     `local_ops` runs pdfplumber and submits directly.
   - `classify_document` — return `{document_type, confidence, reasoning}`.
3. `wait_for_stage("processing")` — server-side long-poll; returns when all
   documents are `completed`/`failed` or after `timeout` seconds with a
   progress summary.
4. `list_documents()` — types with confidence. 🤖 For any
   `confidence < 0.8` or a type that contradicts the filename, the agent opens
   the local file, looks, and either accepts or calls
   `reclassify_document(id, type, reason)`.
5. `missing_documents()` — required `DocumentType`s for the application type
   with no document; recorded in `review.md` as an intake gap.

**Backend** — `POST /ai-parse/surveys/{id}/start_document_processing/`,
*(new)* `GET /agent/surveys/{id}/wait?stage=processing`, *(new)*
`POST /survey/documents/{id}/reclassify/` (audited).

Gate: 🧑 only if the agent cannot decide the type of a document that is
**required** for the application; otherwise proceed and list uncertainties in
`review.md`.

---

## 4. Extract — Documents → SurveyValues

**Agent**

1. `run_analysis(force=true)` — always `force`; the incremental path can strand
   a case at `CONFIRMED_DOCUMENTS` (see [known-issues.md](../design/known-issues.md)).
2. `run_tasks()` in a loop with `wait_for_stage`. Kinds: `extract_section` (per document × section),
   `analyze_section` (per case × section), `extract_values` (labels → machine
   keys), plus any analysis-stage kinds configured local.
3. `wait_for_stage("analysis")`.

**Backend** — `POST /ai-parse/surveys/{id}/start_analysis/` with `force`;
writes `SurveyValue`s with status `CONFIRMED | DOUBTFUL | CONFLICT | MISSING`
exactly as today.

No gate.

---

## 5. Resolve — DOUBTFUL / CONFLICT / MISSING → decisions

This is where the agent adds most value and where policy matters most.

**Agent**

1. `get_review_queue()` → items grouped by status, each with: field name and
   description, applicant, current candidates (value, source document, page,
   verbatim quote, status), and a `significance` flag computed server-side from
   a field allow-list (see below). Page and quote come from
   `SurveyValueEvidence` *(new)* — today a `SurveyValue` records only
   `source_documents`, so the extraction schemas must start emitting
   `{value, quote, page}` per field (Phase 2) for this stage to work as
   described.
2. Work each item:

| Item | Agent action | Gate |
|---|---|---|
| `DOUBTFUL`, evidence in the review-queue payload is unambiguous (e.g. passport MRZ agrees) | `resolve_value(id, value, rationale, evidence_refs)` → `CONFIRMED` | 🤖 |
| `DOUBTFUL`, evidence weak | open the source document locally, re-read the relevant page, then resolve or leave DOUBTFUL with a note | 🤖 |
| `CONFLICT`, **not** legally significant (e.g. address formatting, employer name spelling) | pick the better-evidenced candidate, resolve with rationale | 🤖 |
| `CONFLICT`, **legally significant** (DOB, passport number/expiry, names, marital status, dates that create travel/employment/education gaps, refusal history, criminality) | `propose_resolution(id, value, rationale, evidence_refs)` → status stays CONFLICT with a pending proposal | 🧑 `request_approval(ids)` → one approval page listing every proposal → `wait_for_approval` |
| `MISSING` | append a client question to `review.md` (what, why it's needed, which document would satisfy it) | never sent; RCIC forwards |

3. `wait_for_stage("formulas")` if formulas depend on resolved values (server
   recomputes `SurveyFieldFormula`s and invalidates their cache on resolve).

**Backend** — *(new)* `GET /agent/surveys/{id}/review-queue/`,
*(new)* `POST /agent/values/{id}/resolve/`, `.../propose/`, `.../withdraw/`,
`POST /agent/approvals/` (`kind=approve_proposals`) — approval itself is a
user-session action on the dashboard page.
Each write records actor, session, rationale, evidence refs in an append-only
`ReviewAction`-style log and invalidates the formula cache for the survey.

The significance allow-list lives server-side (settings), not in the playbook,
so Claude and Codex apply the same rule.

---

## 6. Auto-fill — SurveyValues → filled forms

**Agent**

1. `autofill_preflight()` → server checks: no open `CONFLICT`, no pending
   proposals, required `MISSING` fields listed, `automation_status` not stuck
   in `STARTED`. Returns blockers.
2. 🧑 **Gate:** `request_autofill()` — returns an approval URL whose page
   shows the preflight summary (fields filled / missing / assumed). The RCIC
   approves there; the server starts auto-fill itself. Never start auto-fill
   from the agent; it cannot.
3. `wait_for_approval(id)` → `approved`. Then either:
   - `download_output("all")` — fetch the L3 JSON and IMM PDFs into
     `.uapply/output/` for the existing desktop filler, or
   - if the desktop app is installed and configured, hand off to it (out of
     scope for v1; document the manual step).
4. `autofill_status()` until `COMPLETED | FAILED`.

**Backend** — `POST /survey/surveys/{id}/start_auto_filling/`,
`POST /survey/surveys/{id}/l3_data/`, `GET /survey/imm-pdfs/…/download/`.
*(new)* `GET /agent/surveys/{id}/autofill-preflight/`; *(fix)*
`start_auto_filling` must not leave `automation_status=STARTED` on failure.

The agent **never** submits to an IRCC portal. Filling and uploading to the
portal remain the desktop app's and the RCIC's job.

---

## End-of-run report

`/uapply:run` finishes by writing `.uapply/review.md`:

```
# Zhang Wei — Study Permit (outside Canada) — 2026-09-25
Case: https://app.uapply.io/surveys/<id>

## Documents (14 uploaded, 14 classified, 2 reclassified by agent)
## Resolved by agent (9) — value, rationale, evidence
## Awaiting your approval (3) — proposals
## Questions for the client (4)
## Missing required documents (1)
## Auto-fill: preflight blocked by 3 pending proposals
```

and printing the same summary in the chat. The RCIC approves proposals either
on the approval page the agent links (`request_approval`) or in the dashboard — both are the same server-side action under the RCIC's login.
