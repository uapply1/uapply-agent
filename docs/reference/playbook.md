# Playbook — Operator Instructions for the Coding Agent

The playbook is the natural-language half of the product: it tells Claude Code
or Codex *how* to run a uApply case with the tools in
[mcp-tools.md](mcp-tools.md). It is written once in Markdown and packaged for
each runtime.

## Packaging

```
playbook/
├── SOURCE.md                       # single source; sections tagged for each command
├── claude-code/                    # Claude Code plugin
│   ├── .claude-plugin/plugin.json
│   ├── skills/uapply/SKILL.md      # auto-triggers on uApply context; links to reference
│   ├── skills/uapply/reference/    # workflow.md, resolution-policy.md, task-loop.md
│   └── commands/
│       ├── run.md                  # /uapply:run
│       ├── intake.md               # /uapply:intake
│       ├── review.md               # /uapply:review
│       └── autofill.md             # /uapply:autofill
└── codex/
    ├── AGENTS.md                   # same content, Codex conventions
    └── skills/uapply/…
```

A build step renders both from `SOURCE.md` so they cannot drift. `uapply-agent
init` installs the right one (plugin registration for Claude Code; `AGENTS.md`
into the client folder or `~/.codex/` for Codex) and registers the MCP server.

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
- When executing an `AgentTask`, run the task's own prompt on the task's own
  inputs and return only JSON matching `output_schema`. Do not add case
  knowledge from other documents; do not "improve" the prompt.
- Give a rationale and evidence (document, page, quote) for every resolution.
- Write questions for the client into the review report; never contact anyone.

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

- Task loops run in subagents / fresh contexts (Claude Code `Agent` tool; Codex
  sub-tasks), ≤ 5 tasks per subagent, reporting only counts and rejections.
- For > ~15 documents, suggest batch mode to the RCIC before starting
  classification (`uapply-agent run --stages classify,extract`).
- Do not paste document text into the chat unless the RCIC asks.

## Eval hooks

The playbook contains no runtime-specific flags and no backend URLs; those
come from the MCP server. This keeps one playbook testable against both
runtimes in `evals/`.
