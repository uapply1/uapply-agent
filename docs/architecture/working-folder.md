# Working Folder

The client folder is the agent's unit of work and its only local state. The
design goal is that **re-running on the same folder is always safe**: nothing
is uploaded twice, nothing is re-created, and the agent carries on from the
server's current state.

## Layout

```
~/Clients/Zhang_Wei/
├── passport_zhang_wei.pdf
├── bank_statement_2025-03.pdf
├── IMG_2041.HEIC
├── spouse/
│   └── passport_li_na.pdf
├── uApply output/                 # written by final_report
│   ├── report.md                  # the end-of-run report
│   ├── <case>_final_package.zip   # the dashboard's final package
│   └── Forms/                     # filled forms unpacked from the package
└── .uapply/                       # created when the folder is bound to a case
    ├── .gitignore                 # ignores everything under .uapply/
    ├── case.json                  # survey id, name, LLM mode, dependents
    ├── manifest.json              # sha256 → {path, document_id, applicant, uploaded_at, uploaded_path}
    ├── autofill.json              # auto-fill progress between autofill_forms calls
    ├── chat/                      # chat transcripts, their PDFs, intake hints (optional)
    ├── cache/                     # task inputs, rendered pages, converted HEICs, preview/ PNGs
    └── output/                    # imm_pdfs/: forms filled locally with Acrobat Pro
```

Supported files are PDF, JPEG, PNG, HEIC/HEIF, DOCX/DOC and XLSX/XLS.
Folders and files whose names start with `.`, Office lock files (`~$…`) and
`.tmp` files are ignored. A file in a subfolder carries the subfolder's name as
an `applicant_hint` in `scan_folder`; the model uses it when choosing the
`applicant` for `sync_documents`.

## `case.json`

Written by `init_case`, `create_case` and `uapply-agent init`:

```json
{
  "schema": 1,
  "backend": "https://api.uapply.io",
  "survey_id": "…",
  "name": "Zhang Wei",
  "llm_mode": "local_agent",
  "dependents": [
    {"survey_id": "…", "name": "Li Na", "relationship": "spouse"}
  ],
  "stage_history": [
    {"stage": "init", "at": "…"}
  ]
}
```

`case.json` is a pointer, not the source of truth: the agent calls
`case_status` first and trusts the server. The executor pulls tasks for the
principal and every dependent listed here.

## `manifest.json`

```json
{
  "schema": 1,
  "files": {
    "3a7f…e1": {"path": "passport_zhang_wei.pdf", "document_id": "…", "applicant": "principal",
                "uploaded_at": "…", "uploaded_path": null},
    "9c02…b4": {"path": "IMG_2041.HEIC", "document_id": "…", "applicant": "principal",
                "uploaded_at": "…", "uploaded_path": ".uapply/cache/IMG_2041.jpg"}
  }
}
```

Rules:

- Keyed by **sha256 of the original file**, not the path. Renaming a file does
  not re-upload it; editing it does (the new hash is a new file).
- `uploaded_path` is set when the uploaded file differs from the original
  (HEIC converted to JPEG, a chat transcript's PDF).
- Before uploading, `sync_documents` also checks the case's documents: one of
  the same type, name and size is recorded in the manifest instead of being
  uploaded again (for example after a lost local record).

## Privacy of local state

- `.uapply/cache/` holds task inputs downloaded from uApply and rendered
  pages; `.uapply/output/` holds forms filled locally. Both may contain
  personal data; `uapply-agent clean` deletes the files in both.
- `.uapply/chat/` holds chat transcripts and is not removed by `clean` (it is
  the record of what was filed); see
  [chat-sources.md](chat-sources.md#data-flow-and-consent).
- `uApply output/` holds the final package and the report for the RCIC; it is
  not removed by `clean`.
- `.uapply/.gitignore` keeps the state out of git if the RCIC keeps the folder
  in a repository.

## Multiple machines

The manifest is per machine. On another machine, `uapply-agent init --survey
<id>` (or `init_case`) binds the folder again; `sync_documents` then records
files already on the case (same type, name and size) instead of uploading
them twice.
