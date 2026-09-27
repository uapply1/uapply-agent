# Playbook — Operator Instructions for the Coding Agent

The playbook is the natural-language half of the product: it tells Claude Code
or Codex *how* to run a uApply case with the tools in
[mcp-tools.md](mcp-tools.md). It is written once in Markdown and packaged for
each runtime.

## Packaging

The playbook ships **inside the MCP server**, which every runtime already
loads:

- **Server `instructions`** — the behavioural rules below, sent to the runtime
  on connect. Claude Code, Claude Desktop and Codex all surface them to the
  model.
- **MCP prompts** — `uapply/run`, `uapply/intake`, `uapply/review`,
  `uapply/autofill`, `uapply/status`. Runtimes expose these as slash commands
  (`/uapply:run` in Claude Code; the prompt picker in Claude Desktop; Codex's
  prompt list). One definition, every surface, no build step.
- **Resource** `uapply://playbook/resolution-policy` — the long-form policy
  the model can read when it reaches stage 5.

```
playbook/
├── SOURCE.md                # single source: instructions, prompts, policy
└── wrappers/                # optional, thin, generated from SOURCE.md
    ├── claude-code-plugin/  # only adds /uapply:* to the command palette
    └── codex/AGENTS.md      # only for RCICs who prefer file-based config
```

`uapply-agent setup` (run by the installer) registers the MCP server with
whichever runtimes it detects; the wrappers are opt-in.

## Commands

| Command | Does |
|---|---|
| `/uapply:run` | Full pipeline with gates. Starts by calling `case_status` and resuming from the first incomplete stage |
| `/uapply:intake` | Stage 1 only — propose and confirm case + family, create it |
| `/uapply:review` | Stage 5 only — work the review queue, produce proposals and client questions |
| `/uapply:autofill` | Stage 6 only — preflight, gate, start, download |
| `/uapply:status` | Print `case_status` in a readable form |

## Behavioural rules (the heart of the playbook)

Written as instructions to the model. Abbreviated here; `SOURCE.md` has the
full text.

**Always**

- Call `case_status` first. Trust the server over `case.json` and over your own
  memory of earlier turns.
- Prefer tools over reasoning about files: use `get_document_text` /
  `render_pages` rather than guessing from filenames, but only when a decision
  needs it — every look costs the RCIC's plan.
- Execute pipeline work only through `run_tasks`. Never ask for a task's
  prompt or inputs; you orchestrate, the executor runs the model.
- Give a rationale and evidence (document, page, quote) for every resolution.
- Write questions for the client into the review report; never contact anyone.

**Gates — these tools return an approval link, not a result**

`propose_case`, `add_dependent`, `request_approval`, `request_autofill` never
act directly. Show the RCIC the summary in chat, give them the link the tool
returned (never construct one yourself), then call `wait_for_approval`. If it
is still pending after a few calls, say so and stop; do not retry the action
another way.

**Gates — stop and ask the RCIC before**

- Creating a case or adding a dependent.
- Approving any proposal on a legally significant field (the server will
  refuse `resolve_value` for those anyway).
- Starting auto-fill.
- Re-running a stage that will overwrite RCIC-entered values (the server tells
  you via `case_status.blockers`).

**Never**

- Submit anything to a government portal.
- Delete documents or cases.
- Upload files from outside the working folder.
- Loop on a failing tool more than twice; report instead.
- Silently switch the case to server-mode LLM to get past a plan limit — ask.

**Resolution policy** (mirrors [case-workflow.md § 5](../architecture/case-workflow.md#5-resolve--doubtful--conflict--missing--decisions))

1. Read all candidates and their quotes.
2. Prefer primary documents (passport, birth certificate, official letters)
   over secondary (forms the client filled, translations).
3. Prefer the candidate whose quote you can see verbatim on the page.
4. If two primary documents disagree, that is a real conflict: propose, don't
   resolve, and add a client question.
5. Dates: normalise to ISO; if a document shows only month/year, say so in the
   rationale rather than inventing a day.
6. Names: keep the passport spelling; note aliases from other documents.

## Context hygiene

- `run_tasks` returns counts only; loop it with `wait_for_stage` and report
  progress in one line per iteration.
- For overnight or multi-client work, suggest batch mode
  (`uapply-agent run --stages classify,extract`) — same executor, no chat.
- Do not paste document text into the chat unless the RCIC asks.

## Eval hooks

The playbook contains no runtime-specific flags and no backend URLs; those
live in the MCP server and `runners/`. This keeps one playbook testable
against every runtime in `evals/`.
