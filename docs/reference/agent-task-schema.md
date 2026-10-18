# AgentTask Schema

The JSON contract between the backend's task queue and the executor in
`uapply-agent`. One task = one LLM call the server would otherwise have made.

## Envelope

```json
{
  "id": "t_2f9c…",
  "kind": "extract_section",
  "survey_id": "…",
  "document_id": "…",
  "attempt": 1,
  "lease_expires_at": "2026-09-25T15:20:00Z",
  "payload": { … kind-specific, below … }
}
```

Common payload fields:

| Field | Meaning |
|---|---|
| `system_prompt`, `user_prompt` | Resolved from `PromptTemplate` at task creation; already include section/field definitions |
| `output_schema` | JSON Schema from the pipeline's Pydantic `response_format`. The result **must** validate against it |
| `inputs[]` | input files: a `document_id` (the executor fetches a presigned download URL) or, for `llm_call`, a short-lived `url`; downloaded into `.uapply/cache/<task id>/` |
| `text_inputs[]` | `{document_id, text}` for kinds that operate on already-extracted text (server includes the `RawMemo` slice it would have sent to Gemini) |
| `evidence_required` | If true, quoted spans in the result are verified verbatim server-side |
| `language_hint` | e.g. `zh`, `en` — mirrors what the pipeline passes today |
| `purpose` | what the call is for (`section_filter`, `applicant_section`, `analyze_section`, `reformat_extract`, `reformat_translate`); logs and stats only |
| `validator` | optional import path of a server-side check run on submit, on top of the schema (e.g. the analysis result must survive the pipeline's normalisation) |

The executor passes `system_prompt` as the spawned runtime's actual system
prompt and `user_prompt` (plus inputs) as the user turn, so the local model
sees exactly what the hosted model would. Payloads never reach the RCIC's
chat model.

## Kinds

| Kind | Stage | Inputs | Result (validated by `output_schema`) | Evidence |
|---|---|---|---|---|
| `extract_content` | `doc.extract` | the **whole PDF / image / Word / Excel file** (D12); text-layer PDFs, .docx and .xlsx never reach the model; `page_count` when the server knows it | `{pages: [{n, text}]}` → server joins with `## 第N页` markers (PDF) into the document's `RawMemo` | — |
| `classify_document` | `doc.classify` | the document (first three pages rendered) | `{file_types: [...]}`, each one of `allowed_values` (`Passport`, `Permit`, `Visa`, `Stamp`, `None`) | — |
| `extract_section` | extract | `text_inputs` (document text) | section-specific schema (as today) | yes — each field carries `{value, quote, page}` |
| `analyze_section` | extract | multiple `text_inputs` across documents | analysis schema | yes |
| `extract_values` | extract | analysis output | `{values: [{field_key, value}]}` | — |
| `infer_family_relationships` | extract | identity docs text | as today | yes |
| `financial_extract` | classify | bank statement text | Financial Proof Tier-1 schema (facts + transactions) | yes (already verified today) |
| `llm_call` | `doc.filter_sections`, `doc.multi_applicant`, `analysis.section`, `analysis.reformat` | `system_prompt`, `user_prompt`, `output_mode` (`text` \| `json`), `output_schema`, `purpose`, `validator?`, `text_files` (`{file_name, text}`) | the step's JSON schema, or `{"text": "..."}` in text mode | — |

Status: the backend creates `extract_content`, `classify_document` and
`llm_call` tasks, one per request of a pipeline step (D17); every generic
model call is an `llm_call`, told apart by `purpose`. The `extract_section`,
`analyze_section`, `extract_values`, family and Financial Proof kinds above
are the original design. An `llm_call`'s payload and result are purged once
its step has applied the answers.

## Result envelope (agent → server)

```json
{
  "result": { … matches output_schema … },
  "model": "<model name reported by the runtime>",
  "runtime": "claude-code",
  "usage": {"input_tokens": 8123, "output_tokens": 611},
  "session_id": "claude-code-1a2b3c4d"
}
```

`usage` is best-effort; hosted-plan runtimes may not expose it. It feeds
`LLMTokenLog` so the existing monitoring stays whole. Text-layer PDFs are
submitted with `"model": "pdfplumber"`, Word files with `"python-docx"` and
Excel files with `"openpyxl"`, all with `"runtime": "local"`.

## Rejection codes

| Code | Meaning | Agent action |
|---|---|---|
| `SCHEMA_INVALID` | result fails `output_schema`, or the payload's `validator` | fix and resubmit (attempt+1) |
| `EVIDENCE_UNVERIFIED` | all quotes for a required-evidence field missing from text | re-read the page; if the text truly lacks it, return the field with `value: null` |
| `TYPE_NOT_ALLOWED` | classification outside the allowed values | pick from `payload.allowed_values` |
| `PAGE_COUNT_MISMATCH` | an OCR result does not cover every page of the document | return every page |
| `TASK_CANCELLED` / `TASK_FAILED` | the task is no longer open | drop |

After `max_attempts` (3) the server marks the task `failed`; once the batch
settles, its step applies the failure (the document or analysis section fails
with the reason, or a section filter is stored with defaults). The RCIC can re-run the stage, optionally with `llm_mode=server` for
that case.

## Local pre-processing (no model) the agent does before a task

| Task | Local step |
|---|---|
| `extract_content` on a PDF with a text layer (pdfplumber yields > N chars/page) | submit pdfplumber text directly; no model call |
| `extract_content` on a scanned PDF | render the pages to PNG locally (PyMuPDF) and send them in chunks (20 pages per call for Claude Code, 10 for Codex) |
| `extract_content` on an image | one model call on the image (HEIC converted to JPEG first) |
| Any upload of a HEIC file | convert to JPEG first (the uploaded file is the JPEG) |
| `extract_content` on .docx / .xlsx | read locally (python-docx: paragraphs and tables as markdown; openpyxl: each sheet as `# 表格: <name>` plus a markdown table) and submitted as one page; no model call |
| `extract_content` on .doc / .xls | not supported locally |

These mirror the server's content-loading cascade minus the hosted-model
strategies (Gemini vision, LlamaParse), which are replaced by the local model.
