---
name: review
description: Orchestrate a full service review. Use when the user says "/review" or asks for an overall service review that should combine code-reviewer, security-reviewer, and service-reviewer perspectives; spawn the three reviewer subagents when available, collect their independent findings, and synthesize one prioritized review with concrete fixes.
---

# Review Orchestrator

Run a coordinated review of `automation_smb` by combining three reviewer roles:

- `code-reviewer`: read-only code/change review for correctness, error handling, performance, naming, and repository conventions.
- `security-reviewer`: sensitive data leakage, API/LLM exfiltration, logging/storage, and boundary risk review.
- `service-reviewer`: UI-only review from a non-developer internal diagnostic lab user's perspective.

`/review` is explicit permission to use subagents for these three reviews. Keep the three reviews independent, then synthesize them into one final service-level report.

## Inputs

Use the user's provided scope if present:

- Service URL, such as `http://localhost:7860`.
- Target branch, commit, PR, staged diff, or changed files.
- Specific workflow to test.
- Whether to include only findings or also implementation recommendations.

If no URL is provided, try to infer a local service URL from existing repo context or recent user messages. If the service cannot be opened and no URL can be inferred, still run the code and security reviews, and mark the service/UI review as blocked.

## Subagent Plan

Spawn three independent subagents when the multi-agent tool is available:

1. Code review subagent
   - Agent type: `explorer`.
   - Skill: `code-reviewer`.
   - Scope: current working tree changes by default; use user-provided scope when present.
   - Output: findings only, no patches.

2. Security review subagent
   - Agent type: `explorer`.
   - Skill: `security-reviewer`.
   - Scope: service URL if available, plus local code/config/API paths relevant to outbound calls, LLM/provider use, logs, storage, and SMB access.
   - Output: findings, mitigations, and verification steps. Do not reveal actual sensitive values.

3. Service usability subagent
   - Agent type: `default` or `explorer`.
   - Skill: `service-reviewer`.
   - Scope: service URL and user-facing workflow only.
   - Output: UI/UX feedback in plain Korean from the lab worker perspective. Do not inspect code or logs.

While subagents run, the orchestrator may gather non-overlapping context such as `git status --short`, available service URLs, or changed file lists. Do not duplicate a subagent's full review locally.

If subagents are unavailable, run the three review passes sequentially in the same thread using the same role boundaries and clearly note that parallel subagents were unavailable.

## Prompt Shape For Subagents

Keep prompts self-contained and role-specific. Use this pattern:

```text
Use $code-reviewer at .codex/skills/code-reviewer to review <scope>. Return findings only, ordered by severity. Do not edit files.
```

```text
Use $security-reviewer at .codex/skills/security-reviewer to review <service URL and/or scope> for sensitive data leakage and API/LLM exfiltration risks. Return findings, mitigations, and verification steps. Do not reveal actual sensitive values.
```

```text
Use $service-reviewer at .codex/skills/service-reviewer to open <service URL> and review the UI as a non-developer diagnostic lab user. Use only the visible UI. Return plain Korean usability feedback.
```

## Synthesis Rules

Create one final review, not three pasted reports.

- Deduplicate overlapping findings. If security and code reviewers report the same issue, keep the higher severity and merge evidence.
- Preserve disagreements or uncertainty explicitly.
- Prioritize by risk to users and data first, then service correctness, latency, and usability.
- Do not expose sensitive values. Redact or categorize them.
- Convert role-specific recommendations into actionable service-level fixes.
- Keep positive observations brief and only where useful.

## Final Report Format

Use Korean for the final report. Keep file names, identifiers, API names, and URLs in their original form.

1. **총평**
   - One short paragraph stating whether the service is ready to recommend, conditionally usable, or not ready.

2. **우선순위 Top 5**
   - Ordered list of the most important fixes across all reviewers.
   - Include severity, affected surface, and why it matters.

3. **상세 발견 사항**
   - Group by category:
     - 보안/데이터 반출
     - 코드 안정성/성능
     - 사용자 경험
   - For each item include:
     - **심각도**: Critical / High / Medium / Low
     - **근거**: UI behavior or `path:line` reference when available
     - **영향**: What can fail, leak, confuse, or slow down
     - **해결 방안**: Concrete fix direction
     - **검증 방법**: How to confirm the fix

4. **리뷰 한계**
   - Mention unavailable service URL, blocked browser tools, unrun tests, or scope limits.

5. **다음 액션**
   - Short ordered checklist of the next engineering actions.

## Safety

- Never print credentials, patient identifiers, full internal paths, API keys, raw prompts containing sensitive data, or full logs.
- Do not commit, push, deploy, or mutate production/stateful resources during review.
- Do not ask subagents to fix code unless the user explicitly requests fixes after the review.
