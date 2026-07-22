---
name: report-workflow
description: Use to route QC draft/audit, karyotype, cytogenetics, or NGS report requests to the matching tool and present results clearly.
---

# Report Workflow

Identify the requested report family before calling a tool.

- Use `cytogenetics_karyotype_summary` for a supplied synthetic ISCN string.
- Use `draft_qc_report` when structured synthetic QC measurements should become a human-review Markdown draft.
- Use `audit_qc_report` when an uploaded PDF or Markdown QC report should be checked against local synthetic SOP rules.
- Use `cytogenetics_report` for cytogenetics report template or checklist questions.
- Use `ngs_report` for NGS report template or checklist questions.
- If the report family is ambiguous, ask one short clarification question.
- Preserve tool output facts and do not fabricate missing report content.
- Never treat a QC `DRAFT` as approved or final; preserve its deterministic verification status and evidence.
