# Case Workflow

The stages an agent drives in `/uapply:run`, the tools it uses at each, what
the backend does, and where the RCIC is asked. Tool names are defined in
[reference/mcp-tools.md](../reference/mcp-tools.md); endpoints in
[reference/backend-api.md](../reference/backend-api.md); the instructions the
model follows are in the playbook (`src/uapply_agent/SOURCE.md`).

Legend: **[RCIC]** = the RCIC is asked (through the runtime's question tool,
e.g. `AskUserQuestion`, and the run continues with the answer); **[agent]** =
the agent decides and says so in one line.

```
intake ──▶ upload ──────▶ processing ──▶ analysis ──▶ finishing ──▶ review and submit
[RCIC]     [agent/RCIC]   [agent]        [agent]      [agent]       [RCIC]
```

Every stage is re-runnable on the same folder; `case_status` is called first
and the server's state wins over `.uapply/case.json`. See
[working-folder.md](working-folder.md) for the local state.

---

## 1. Intake: folder → case

**Agent**

1. `case_status()`. If the folder is bound, continue with the first stage that
   is not done.
2. Not bound (`NO_CASE`): `preview_document` the identity documents to propose
   the client name and application type (`list_application_types`).
3. [RCIC] One question: create a new case (first option; shows the proposed name,
   type and that the account is charged) or use an existing case (the RCIC
   pastes the survey id).
4. New case: `create_case(name, application_type_id, confirmation="create case")`,
   only after the RCIC picked "Create case" or typed "create case" / 确认创建.
   Existing case: `init_case(survey_id)`.
5. Nothing is uploaded before the folder is bound.

Optionally the case starts from the client's chat history
(`/uapply:intake-from-chat`): `chat_sources` → `chat_find_contact` →
`chat_fetch`, then the same create-case question. See
[chat-sources.md](chat-sources.md).

**Backend**: `POST /api/survey/surveys/` with `llm_mode=local_agent` and the
application type's default IMM forms; `POST /api/ai-parse/agent/surveys/{id}/llm_mode/`
when binding an existing case.

---

## 2. Upload: files → Documents

**Agent**

1. `scan_folder()` lists supported files (PDF, images including HEIC, Office
   files) with sha256 and whether they were uploaded before; dot-folders,
   dotfiles and Office lock files are ignored.
2. For each new file whose type is not obvious from its name,
   `preview_document` renders page 1 locally and the model picks a type from
   `list_document_types`. [RCIC] Only when the pages do not settle it, all unclear
   files in one question. Filled IMM forms go under the generic Agent Survey
   type; any other file with no matching type is asked about, never filed
   under Agent Survey.
3. `sync_documents(document_type_id, paths, applicant?)` per type: skips files
   already in the manifest, records a case document with the same type, name
   and size instead of uploading again, converts HEIC to JPEG, calls
   `bulk_upload` one file at a time and records `sha256 → document_id` in
   `manifest.json`.

**Backend**: `POST /api/survey/documents/bulk_upload/`. For agent cases an
upload starts nothing.

Uploading to the RCIC's own uApply case is the expected effect of the run, so
there is no separate confirmation.

---

## 3. Processing: Documents → text and document data

**Agent**

1. `start_processing()` starts every uploaded, failed or stopped top-level
   document of a processable type (or the given `document_ids`).
2. Loop `run_tasks()` → `wait_for_stage("processing")` until `done` is true
   and `remaining` is 0, printing one progress line per call. The executor
   runs each task in a fresh headless runtime (see
   [runtime-modes.md](runtime-modes.md)):
   - `extract_content`: page-marked text for the whole document. PDFs with a
     text layer skip the model (pdfplumber); scans are read by the runtime.
   - `classify_document`: the pipeline's passport / visa / permit sub-type
     classification.
   - `llm_call`: every other model call of the pipeline (sections, Financial
     Proof, DOCX/XLSX transcription), intercepted on the server (D14).
3. Failed documents are named as soon as they appear. `plan_limited` or
   `runtime_error` stops the run with the reason.

**Backend**: `POST /api/ai-parse/surveys/{id}/start_document_processing/`,
`GET /api/ai-parse/agent/surveys/{id}/wait/?stage=processing`, task pull /
result / release.

---

## 4. Analysis: Documents → SurveyValues

**Agent**

1. `start_analysis()` once every document is processed
   (`PROCESSING_NOT_DONE` otherwise).
2. Loop `run_tasks()` → `wait_for_stage("analysis")` until done.

**Backend**: `POST /api/ai-parse/surveys/{id}/start_analysis/`; writes
`SurveyValue`s with status `CONFIRMED | DOUBTFUL | CONFLICT | MISSING`, as for
a dashboard case.

---

## 5. Finishing: archives, forms, report

**Agent**

1. `confirm_documents()`: the dashboard's Confirm step. Each archive is merged
   into one PDF and compression is queued on uApply (no AI). Refused with
   `ANALYSIS_NOT_DONE` before the analysis is done.
2. `autofill_forms()` until `remaining` is 0. With Adobe Acrobat Pro on the
   RCIC's Windows PC, the backend sends each form's recorded field operations,
   the agent replays them in Acrobat as JavaScript batches, as pdf_auto does,
   and uploads the result (copies in `.uapply/output/imm_pdfs/`). Otherwise, or
   when Acrobat fails or a form gets no field written, it returns
   `mode: platform`: uApply's platform filler fills the forms (no AI) and the
   agent loops `wait_for_stage("filling")`.
3. `final_report()`: status, AI Check counts (conflict, doubtful, missing),
   documents, IMM forms with fill rate, archives, and links to the dashboard's
   AI Check and Submit steps and to start the online portal. The final package
   zip is saved in `uApply output/` with its forms unpacked in
   `uApply output/Forms/`, and the report in `uApply output/report.md`. The
   agent shows the report as is, then one line on anything waiting on the
   RCIC.

**Backend**: `POST /api/survey/surveys/{id}/generate_archive_files/`;
`POST /api/ai-parse/agent/surveys/{id}/autofill/claim/`, `.../autofill/{imm_pdf_id}/result/`,
`.../autofill/finish/`, or `POST /api/survey/surveys/{id}/start_auto_filling/`
for the platform filler; `GET /api/ai-parse/agent/surveys/{id}/report/` and
`POST /api/survey/surveys/{id}/download_zip_submit_files/`.

---

## 6. RCIC review and submit

[RCIC] The RCIC reviews the AI Check in the dashboard, resolves conflicts and
doubtful values, and submits. The online portal automation starts from the
dashboard (the uApply Chrome extension talks only to the dashboard page). The
agent never submits to an IRCC portal and never contacts the client.

---

## Planned (not implemented)

- **Review queue and resolution by the agent.** The agent would read
  DOUBTFUL / CONFLICT / MISSING values with evidence (document, page, verbatim
  quote), resolve non-significant ones with a rationale, and propose values
  for legally significant fields (names, dates of birth, passport data,
  marital status, history gaps) for the RCIC to approve. Needs per-value
  evidence (`SurveyValueEvidence`) in the backend.
- **Dashboard approval pages** for case creation, proposal batches and
  auto-fill start, replacing the confirmation phrase (D10).
- **Auto-fill preflight** that blocks on open conflicts and pending proposals.
- **Reclassification and missing documents**: `reclassify_document` with an
  audited reason, and required document types with no document per applicant.
- **Client questions** stored on the case and shown in the dashboard.
