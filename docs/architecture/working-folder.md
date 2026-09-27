# Working Folder

The client folder is the agent's unit of work and its only local state. The
design goal is that **re-running on the same folder is always safe**: nothing
is uploaded twice, nothing is re-created, and the agent resumes from the first
incomplete stage.

## Layout

```
~/Clients/Zhang_Wei/
├── passport_zhang_wei.pdf
├── bank_statement_2025-03.pdf
├── IMG_2041.HEIC
├── spouse/
│   └── passport_li_na.pdf
└── .uapply/                       # created by `uapply-agent init` or first run
    ├── case.json                  # survey ids, application type, family map, stage
    ├── manifest.json              # sha256 → {document_id, applicant, path, uploaded_at}
    ├── review.md                  # human-readable run report + client questions
    ├── cache/                     # rendered pages, converted HEICs, preview/ PNGs (safe to delete)
    └── output/                    # L3 JSON, filled IMM PDFs (from auto-fill)
```

Subfolders are allowed and carry a hint: a folder named like a dependent
(`spouse/`, `child_1/`, or the person's name) maps to that applicant. The hint
is a proposal at intake, confirmed by the RCIC, and then recorded in
`case.json` so later files in that folder are attributed automatically.

## `case.json`

```json
{
  "schema": 1,
  "backend": "https://api.uapply.io",
  "team_id": "…",
  "principal": {"survey_id": "f7ee9c66-…", "name": "Zhang Wei"},
  "dependents": [
    {"survey_id": "…", "name": "Li Na", "relationship": "spouse", "folder": "spouse/"}
  ],
  "application_type_id": "…",
  "llm_mode": "local_agent",
  "stage": "resolve",
  "stage_history": [
    {"stage": "intake", "at": "2026-09-25T14:02:11Z", "by": "claude-code"},
    {"stage": "upload", "at": "…"},
    {"stage": "classify", "at": "…"},
    {"stage": "extract", "at": "…"}
  ]
}
```

`stage` is advisory: the agent asks the server for the real state
(`case_status` tool) and reconciles — the server is the source of truth,
`case.json` is a pointer.

## `manifest.json`

```json
{
  "schema": 1,
  "files": {
    "3a7f…e1": {"path": "passport_zhang_wei.pdf", "document_id": "…", "applicant": "principal",
                "size": 1048576, "uploaded_at": "…", "converted_from": null},
    "9c02…b4": {"path": "IMG_2041.HEIC", "document_id": "…", "applicant": "principal",
                "converted_from": "heic", "uploaded_path": ".uapply/cache/IMG_2041.jpg"}
  }
}
```

Rules:

- Keyed by **sha256 of the original file**, not the path. Renaming a file does
  not re-upload it; editing it does (the new hash is a new document, and the
  agent tells the RCIC the old one still exists on the server).
- A file present in the manifest but missing from disk is not an error; the
  agent can still complete tasks for it by downloading from S3.
- Files under `.uapply/`, dotfiles, and `~$`/`.tmp` office lock files are
  ignored.

## `review.md`

Written by the agent, meant for the RCIC. Regenerated at the end of every run
(previous versions kept in `.uapply/review-<timestamp>.md`). It contains the
run summary, agent-resolved values with rationale, pending proposals, client
questions and missing documents — see the end of
[case-workflow.md](case-workflow.md).

It is the only place the agent "sends" anything: questions for the client are
written here for the RCIC to forward, never emailed.

## Privacy of local state

- `.uapply/` contains ids and a report, not copies of documents (except
  `cache/`, which holds derived images of files already in the folder).
- `cache/` and `output/` may contain personal data; the CLI offers
  `uapply-agent clean` and the playbook tells the agent to run it at the end
  of a batch run if the RCIC asked for it.
- Nothing under `.uapply/` should be committed if the RCIC happens to keep the
  folder in git; `init` writes a `.gitignore` there.

## Multiple machines

The manifest is per-machine. If a colleague runs the agent on the same case
from another laptop, `init --survey <id>` rebuilds the manifest from the
server's document list (matching on the stored sha256). This is why the backend
must store `sha256` on `Document` *(new field)*.
