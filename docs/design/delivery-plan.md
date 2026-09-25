# Delivery Plan

Phased so that each phase ships something usable and de-risks the next.
Estimates are relative (S/M/L), not dates.

## Phase 0 — Prerequisites (backend)

Must land before anything else; most are needed regardless of the agent.

| Item | Size | Why first |
|---|---|---|
| Prepare/continue refactor of the LLM-calling steps in `process_document` and analysis (`run_llm_stage` helper; server mode behaviour-identical) | L | D2; must land and be verified in server mode before any local-mode work |
| `AgentTaskBatch` / `AgentTask` models, `expire_agent_tasks` beat task, `waiting_on_agent` flag honoured by the stale sweep | M | the wait mechanism |
| Fix the [known issues](known-issues.md): auto-fill `STARTED` stuck, `force` on re-analysis, stale-job auto-fail interplay | S | an agent will loop on them |
| `Document.sha256` + dedup in `bulk_upload`; `Idempotency-Key` middleware | S | idempotency everywhere |
| Auth0 native client for the agent (device grant) + `agent:cases` scope in the DRF permission class | S | everything else needs auth |

## Phase 1 — Vertical slice: classification on local tokens

Goal: prove the round trip end-to-end with the simplest kind.

| Item | Size |
|---|---|
| Pull / result / release endpoints on the Phase 0 models, lease logic | M |
| `survey.llm_mode` + `AGENT_LOCAL_TASK_KINDS={classify_document}`; classification stage through `run_llm_stage` | M |
| Result validation: schema + allowed-type check; `LLMTokenLog` write | S |
| `uapply-agent` package: `login`, `init`, `mcp` with `whoami`, `case_status`, `scan_folder`, `sync_documents`, `start_processing`, `run_tasks`, `wait_for_stage`, `list_documents` | M |
| Executor: `runners/claude_code.py`, `runners/codex.py` (headless spawn, system prompt, JSON output, usage-limit detection), `--workers` | M |
| Local ops: page render, HEIC convert, pdfplumber text | S |
| Packaging: single-binary builds for Windows and macOS (PyMuPDF, pillow-heif, pdfplumber bundled); `init` detects installed runtimes | M |
| MCP prompts + server instructions carrying the playbook v0 | S |
| Manual test on both runtimes with one real client folder | S |

Exit criterion: a folder of 10 text-layer PDFs (so no OCR model is needed)
is uploaded, content-extracted locally with pdfplumber, and classified with
**zero server-side LLM calls**, from both Claude Code and Codex, on Windows
and macOS, with results visible in the dashboard — **and** a measured table
of model calls and (where reported) tokens per document per runtime,
replacing the estimates in [runtime-modes.md](../architecture/runtime-modes.md).
Scanned documents still use server OCR in this phase.

## Phase 2 — Full pipeline on local tokens

| Item | Size |
|---|---|
| `extract_content` kind (vision OCR local; text-layer PDFs skip the model) | M |
| `extract_section`, `analyze_section`, `extract_values`, family inference kinds; evidence verification wired to the shared verifier | M |
| Per-value evidence: extraction schemas emit `{value, quote, page}`; `SurveyValueEvidence` model written by the continuation for both server and local mode; conflict candidates keep their evidence | M |
| Financial Proof Tier-1 extraction as a kind | S |
| Batch runner (`uapply-agent run`) — stage sequencing over the Phase 1 executor, multi-folder, gate stops | S |
| Cancellation + mode-switch handling for open tasks | S |
| Dashboard: "processing on RCIC machine" badge, open-task count | S |

Exit criterion: `run --stages classify,extract` on a 40-document family case
completes overnight on a Pro-tier plan without manual intervention, or pauses
cleanly at plan limits and resumes.

## Phase 3 — Intake and review

| Item | Size |
|---|---|
| Intake tools (`list_application_types`, `propose_case`, `add_dependent`) via the approval flow | S |
| Review-queue endpoint with evidence; `resolve` (with `expected_updated_at`) / `propose` / `withdraw`; `ValueProposal` model; significant-field list; audit log | M |
| `Approval` model + endpoints + dashboard approval pages (case setup, proposal batch, auto-fill); `wait_for_approval` tool; browser-open from the MCP server | M |
| `missing_documents`, `reclassify_document`, client questions | S |
| Playbook: `/uapply:intake`, `/uapply:review`, resolution policy, context hygiene | M |
| Dashboard: proposals inbox, agent-activity tab, agent-resolved value styling | M |

Exit criterion: on the eval set, ≥ 90 % of agent resolutions on
non-significant fields match the RCIC gold answer; 100 % of significant-field
changes are proposals.

## Phase 4 — Auto-fill and polish

| Item | Size |
|---|---|
| `autofill_preflight`, `request_autofill` (approval flow), `download_output` | S |
| `write_review_report`; `/uapply:run` end-to-end; `/uapply:autofill` | S |
| `uapply-agent clean`; multi-machine `init --survey` | S |
| Rate limits, error `hint`s pass, docs for RCIC onboarding | S |

Exit criterion: an RCIC with no developer help completes a study-permit case
from folder to downloaded L3/IMM output using only `/uapply:run`.

## Evaluation

Built in Phase 1, grown every phase. Lives in `evals/`.

- **Golden folders.** 10–20 anonymised or synthetic client folders with:
  expected application type and family; expected document types; expected
  `SurveyValue`s; expected conflict resolutions and which are significant.
- **Metrics.** Classification accuracy; extraction field accuracy vs server
  (Gemini) mode; resolution agreement; proposals-on-significant-fields = 100 %;
  tasks rejected per 100; wall-clock and (where reported) tokens per case.
- **Matrix.** Every metric × {Claude Code, Codex} × {interactive, batch}.
- **Regression gate.** A release must not drop any metric > 2 points against
  the previous release on either runtime.
- **Adversarial folder.** Documents containing injected instructions; expected
  outcome: no effect on written values.

## Rollout

1. Internal RCIC (one team) on Phase 1–2, server-mode fallback always
   available per case.
2. Design partners (3–5 firms) on Phase 3, with the onboarding privacy note.
3. General availability after Phase 4 and two eval cycles.
