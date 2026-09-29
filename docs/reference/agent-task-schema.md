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

The executor passes `system_prompt` as the spawned runtime's actual system
prompt and `user_prompt` (plus inputs) as the user turn, so the local model
sees exactly what the hosted model would. Payloads never reach the RCIC's
chat model.

## Kinds

| Kind | Stage | Inputs | Result (validated by `output_schema`) | Evidence |
|---|---|---|---|---|
| `extract_content` | classify | the **whole PDF / image** (D12); text-layer PDFs never reach the model | `{pages: [{n, text}]}` → server joins with `## 第N页` markers into the parent document's `RawMemo` | — |
| `classify_document` | classify | first N page images or text | `{document_type: <file_name>, confidence: 0–1, reasoning}`; type must be in the survey's allowed set | — |
| `extract_section` | extract | `text_inputs` (document text) | section-specific schema (as today) | yes — each field carries `{value, quote, page}` |
| `analyze_section` | extract | multiple `text_inputs` across documents | analysis schema | yes |
| `extract_values` | extract | analysis output | `{values: [{field_key, value}]}` | — |
| `infer_family_relationships` | extract | identity docs text | as today | yes |
| `financial_extract` | classify | bank statement text | Financial Proof Tier-1 schema (facts + transactions) | yes (already verified today) |
| `llm_call` | any | `system_prompt`, `user_prompt`, `output_mode` (`text` \| `json`), `output_schema`, `inputs` (`{blob, url, mime}`), `text_files` (`{file_name, text}`) | the caller's JSON schema, or `{"text": "..."}` in text mode | — |

Status: the backend creates `extract_content` and `classify_document` tasks
(prepare/continue, selected by `AGENT_LOCAL_TASK_KINDS`) and `llm_call` tasks
for every other model call of a local case (D14). The section, analysis,
family and Financial Proof kinds above are the original design; those calls
arrive as `llm_call` today.

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
submitted with `"model": "pdfplumber"` and `"runtime": "local"`.

## Rejection codes

| Code | Meaning | Agent action |
|---|---|---|
| `SCHEMA_INVALID` | result fails `output_schema` | fix and resubmit (attempt+1) |
| `EVIDENCE_UNVERIFIED` | all quotes for a required-evidence field missing from text | re-read the page; if the text truly lacks it, return the field with `value: null` |
| `TYPE_NOT_ALLOWED` | classification outside the allowed values | pick from `payload.allowed_values` |
| `PAGE_COUNT_MISMATCH` | an OCR result does not cover every page of the document | return every page |
| `TASK_CANCELLED` / `TASK_FAILED` | the task is no longer open | drop |

After `max_attempts` (3) the server marks the task `failed`, ends its batch
with `failed=True`, and the continuation marks the document/analysis job
`FAILED` with the reason. The RCIC can re-run the stage, optionally with `llm_mode=server` for
that case.

## Local pre-processing (no model) the agent does before a task

| Task | Local step |
|---|---|
| `extract_content` on a PDF with a text layer (pdfplumber yields > N chars/page) | submit pdfplumber text directly; no model call |
| `extract_content` on a scanned PDF | render the pages to PNG locally (PyMuPDF) and send them in chunks (20 pages per call for Claude Code, 10 for Codex) |
| `extract_content` on an image | one model call on the image (HEIC converted to JPEG first) |
| Any upload of a HEIC file | convert to JPEG first (the uploaded file is the JPEG) |
| DOCX / XLSX | no local extraction; the server's transcription call arrives as an `llm_call` task (D14) |

These mirror the server's content-loading cascade minus the hosted-model
strategies (Gemini vision, LlamaParse), which are replaced by the local model.
