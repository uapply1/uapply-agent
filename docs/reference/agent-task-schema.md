# AgentTask Schema

The JSON contract between the backend's `LocalAgentLLM` provider and the
agent. One task = one LLM call the server would otherwise have made.

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
| `inputs[]` | `{document_id, local_path, sha256, pages?: int[], mime}`; the MCP server resolves `local_path` (downloads from S3 into `cache/` if missing or hash mismatch) |
| `text_inputs[]` | `{document_id, text}` for kinds that operate on already-extracted text (server includes the `RawMemo` slice it would have sent to Gemini) |
| `evidence_required` | If true, quoted spans in the result are verified verbatim server-side |
| `language_hint` | e.g. `zh`, `en` — mirrors what the pipeline passes today |

The agent must treat `system_prompt`/`user_prompt` as **the prompt to run**,
not as instructions to itself about the case; the playbook is explicit that
the model executes the task exactly as a hosted model would, and returns only
the JSON.

## Kinds

| Kind | Stage | Inputs | Result (validated by `output_schema`) | Evidence |
|---|---|---|---|---|
| `extract_content` | classify | page images (rendered locally) | `{pages: [{n, text}]}` → server joins with `## 第N页` markers into `RawMemo` | — |
| `classify_document` | classify | first N page images or text | `{document_type: <file_name>, confidence: 0–1, reasoning}`; type must be in the survey's allowed set | — |
| `extract_section` | extract | `text_inputs` (document text) | section-specific schema (as today) | yes — each field carries `{value, quote, page}` |
| `analyze_section` | extract | multiple `text_inputs` across documents | analysis schema | yes |
| `extract_values` | extract | analysis output | `{values: [{field_key, value}]}` | — |
| `infer_family_relationships` | extract | identity docs text | as today | yes |
| `financial_extract` | classify | bank statement text | Financial Proof Tier-1 schema (facts + transactions) | yes (already verified today) |

Kinds not listed (web-search assisted steps, embeddings) stay server-side;
`AGENT_LOCAL_TASK_KINDS` controls the split.

## Result envelope (agent → server)

```json
{
  "result": { … matches output_schema … },
  "model": "claude-fable-5-1",
  "runtime": "claude-code",
  "usage": {"input_tokens": 8123, "output_tokens": 611},
  "notes": "optional free text for the audit log (e.g. 'page 2 rotated; read after local rotation')"
}
```

`usage` is best-effort; hosted-plan runtimes may not expose it. It feeds
`LLMTokenLog` so the existing monitoring stays whole.

## Rejection codes

| Code | Meaning | Agent action |
|---|---|---|
| `SCHEMA_INVALID` | result fails `output_schema` | fix and resubmit (attempt+1) |
| `EVIDENCE_UNVERIFIED` | all quotes for a required-evidence field missing from text | re-read the page; if the text truly lacks it, return the field with `value: null` |
| `TYPE_NOT_ALLOWED` | classification outside allowed document types | pick from `payload.allowed_types` |
| `LEASE_LOST` | another worker completed it | drop |
| `TASK_CANCELLED` | processing stopped or mode switched | drop |

After `max_attempts` (3) the server marks the task `failed`, ends its batch
with `failed=True`, and the continuation marks the document/analysis job
`FAILED` with the reason. The RCIC can re-run the stage, optionally with `llm_mode=server` for
that case.

## Local pre-processing (no model) the agent does before a task

| Task | Local step |
|---|---|
| `extract_content` on a PDF with a text layer (pdfplumber yields > N chars/page) | submit pdfplumber text directly; no model call |
| `extract_content` on scanned PDF / image | render pages at 150 dpi to `cache/`, auto-rotate, hand to model |
| Any task on HEIC | convert to JPEG first (uploaded file is the JPEG) |
| DOCX / XLSX | python-docx / openpyxl text; no model call for content extraction |

These mirror the server's `content_loader` cascade minus the hosted-model
strategies (Gemini vision, LlamaParse), which are replaced by the local model.
