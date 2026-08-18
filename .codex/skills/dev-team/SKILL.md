---
name: dev-team
description: Orchestrate the automation_smb development team. Use when the user asks to build, improve, refactor, review, or plan a feature using the development team; coordinate project-planner, frontend-developer, backend-developer, and a read-only code-reviewer, reject purpose drift and unnecessary complexity before finalizing each testable code patch, integrate changes, and report the final implementation status.
---

# Development Team Orchestrator

Coordinate a development team with four roles:

- `project-planner`: Defines product/technical direction, improves project structure, splits work, communicates user decisions, and routes blockers.
- `frontend-developer`: Directly implements UI/UX work and reports product/API/security blockers to the planner.
- `backend-developer`: Directly implements server/API/data/operations work and reports product/frontend/security blockers to the planner.
- `code-reviewer`: Reviews a testable code patch read-only before it is finalized. Checks correctness and security, then challenges
  purpose drift, speculative abstractions, duplicated adapters/configuration, unused feature gates, and runtime guards that are unnecessary
  for the repository's current synthetic-only product stage.

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

4. Run the code review gate.
   - Treat a "finalized code patch" as a testable developer handoff or local integration diff before staging, committing, or Docker build;
     do not interrupt every keystroke.
   - Invoke `code-reviewer` for every finalized code patch. The reviewer must not edit files.
   - Require the reviewer to read `AGENTS.md`, the request, canonical plan/status, and only the relevant diff/source/tests.
   - Ask first whether the patch preserves the current product purpose and synthetic-only stage. Then check whether each new module,
     abstraction, dependency, setting, feature flag, cache, endpoint, and guard is required by a current acceptance criterion.
   - Prefer removal or a smaller repo-native implementation when an added layer has no current consumer, duplicates an existing contract,
     restores inactive legacy scope, or anticipates an unapproved future requirement.
   - Do not treat normal timeout, read-only, credential, or external-transfer boundaries required by `AGENTS.md` as overengineering.
   - Resolve actionable findings and invoke `code-reviewer` again on the revised finalized patch. Proceed only when no blocking findings remain.

5. Integrate.
   - Review changed files from each developer.
   - Resolve conflicts and ensure frontend/backend contracts match.
   - Run relevant tests/builds/smoke checks.
   - If integration changes code after the review gate, run the code reviewer again before quality/Docker gates.
   - Do not commit or push unless the user separately asks.

6. Complete the feedback delivery loop for product changes.
   - Treat each user feedback round as: plan → implement → targeted tests and artifact/UI QA → project quality gate → Docker build → healthy container replacement → health/OpenAPI/UI/synthetic read-only smoke → report the result and URL → wait for the next feedback round.
   - For spreadsheet output, include structural checks and a rendered or real-application visual check when a renderer is available; never treat a valid ZIP alone as visual success.
   - Run tests and build the replacement image before replacing a healthy container. If either fails, keep the existing healthy container running and report the failure.
   - Docker registry push, remote deployment, real SMB writes, commit, and git push still require separate explicit user authorization.
   - Skip Docker only for documentation-only changes or when the user explicitly excludes Docker reflection, and state the reason in the final report.

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

Code reviewer:

```text
Review the finalized patch read-only as the code-reviewer. Read AGENTS.md, the user request, canonical plan/status, and the relevant diff,
source, and tests. Report findings by severity. Check correctness, security, error handling, performance, and naming, but explicitly audit
whether the patch still serves automation_smb's current purpose and synthetic-only phase. For every new abstraction, dependency, setting,
feature flag, cache, endpoint, and guard, decide whether a current acceptance criterion requires it; flag redundant layers, inactive legacy
restoration, speculative future-proofing, and simpler repo-native alternatives. Do not edit files. If there are no actionable findings,
state that the purpose/necessity gate passes.
```

## Local Orchestrator Duties

- Keep the critical path moving. Do not wait on subagents while useful non-overlapping integration or inspection work is available.
- Do not duplicate delegated implementation locally unless needed to integrate or fix a returned patch.
- Protect user changes. Never revert unrelated changes.
- Keep security constraints from `automation_smb` active across all work.
- Prefer conservative, repo-native implementation patterns.
- Keep a review ledger per finalized patch: reviewed scope, reviewer result, fixes made, and re-review result. Do not claim the code review
  gate passed from tests alone.
- Start a local dev server after frontend/app changes when needed and provide the URL.
- Record the exact Docker image/service, exposed local URL, health result, and safe smoke scope after each product feedback cycle.

## Final Report Format

Use Korean by default.

- **완료 요약**: What was built or changed.
- **역할별 결과**: Planner, frontend, backend outcomes.
- **코드 리뷰**: Reviewed patch units, purpose/necessity result, findings fixed, and remaining non-blocking risks.
- **수정 파일**: Important paths changed.
- **검증**: Commands, builds, tests, or browser checks run.
- **미해결 사항**: Blockers, assumptions, or user decisions still needed.
- **다음 단계**: Short actionable list.
