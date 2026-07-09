---
name: dev-team
description: Orchestrate the automation_smb development team. Use when the user asks to build, improve, refactor, or plan a feature using the development team; coordinate project-planner, frontend-developer, and backend-developer subagents, route blockers through the planner, integrate their changes, and report the final implementation status.
---

# Development Team Orchestrator

Coordinate a development team with three roles:

- `project-planner`: Defines product/technical direction, improves project structure, splits work, communicates user decisions, and routes blockers.
- `frontend-developer`: Directly implements UI/UX work and reports product/API/security blockers to the planner.
- `backend-developer`: Directly implements server/API/data/operations work and reports product/frontend/security blockers to the planner.

Explicit use of `dev-team` is permission to spawn these subagents when the multi-agent tool is available. The orchestrator remains responsible for integration, verification, and final reporting.

## Team Flow

1. Start with planning.
   - Spawn or run `project-planner` first.
   - Ask for an implementation brief with frontend/backend/shared/test responsibilities.
   - If the planner identifies a user decision that materially changes scope, ask the user before assigning implementation.

2. Assign implementation.
   - Spawn `frontend-developer` for UI/UX scope.
   - Spawn `backend-developer` for server/API/data scope.
   - Give each developer a disjoint write scope whenever possible.
   - Tell developers they are not alone in the codebase and must not revert unrelated or parallel changes.

3. Route blockers.
   - If frontend or backend reports ambiguity, contract mismatch, or security/product risk, send it to the planner.
   - The planner summarizes the decision and either chooses a conservative default or asks the user.
   - Resume developer work only after the blocker is resolved.

4. Integrate.
   - Review changed files from each developer.
   - Resolve conflicts and ensure frontend/backend contracts match.
   - Run relevant tests/builds/smoke checks.
   - Do not commit or push unless the user separately asks.

## Subagent Prompt Shapes

Planner:

```text
Use $project-planner at .codex/skills/project-planner to analyze this request and create a concise implementation brief for frontend and backend developers. Include scope, assumptions, risks, acceptance criteria, and developer handoff notes.
```

Frontend:

```text
Use $frontend-developer at .codex/skills/frontend-developer to implement the assigned frontend scope: <scope>. Do not edit backend files unless explicitly listed. Report files changed, verification, and blockers for the planner.
```

Backend:

```text
Use $backend-developer at .codex/skills/backend-developer to implement the assigned backend scope: <scope>. Do not edit frontend files unless explicitly listed. Report files changed, verification, contracts, and blockers for the planner.
```

## Local Orchestrator Duties

- Keep the critical path moving. Do not wait on subagents while useful non-overlapping integration or inspection work is available.
- Do not duplicate delegated implementation locally unless needed to integrate or fix a returned patch.
- Protect user changes. Never revert unrelated changes.
- Keep security constraints from `automation_smb` active across all work.
- Prefer conservative, repo-native implementation patterns.
- Start a local dev server after frontend/app changes when needed and provide the URL.

## Final Report Format

Use Korean by default.

- **완료 요약**: What was built or changed.
- **역할별 결과**: Planner, frontend, backend outcomes.
- **수정 파일**: Important paths changed.
- **검증**: Commands, builds, tests, or browser checks run.
- **미해결 사항**: Blockers, assumptions, or user decisions still needed.
- **다음 단계**: Short actionable list.
