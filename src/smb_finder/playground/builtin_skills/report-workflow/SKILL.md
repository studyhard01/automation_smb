---
name: report-workflow
description: Use to route a report request to the matching karyotype, cytogenetics, or NGS report tool and present its result clearly.
---

# Report Workflow

Identify the requested report family before calling a tool.

- Use `cytogenetics_karyotype_summary` for a supplied synthetic ISCN string.
- Use `cytogenetics_report` for cytogenetics report template or checklist questions.
- Use `ngs_report` for NGS report template or checklist questions.
- If the report family is ambiguous, ask one short clarification question.
- Preserve tool output facts and do not fabricate missing report content.
