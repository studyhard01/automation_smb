---
name: push
description: Safely publish completed repository work. Use when the user says "/push", "$push", "push 해줘", "PR 만들어줘", or asks Codex to push the current branch; verify the working tree and security checks, push only committed work, create a GitHub Pull Request, and after PR creation report the push/PR summary to the AI Solution Lab Slack workspace channel named "진검파트".
---

# Safe Push

Push only reviewed, committed work and create a Pull Request. This repository handles sensitive SMB and medical-context data, so the workflow prioritizes secret/PHI leakage prevention before network actions.

## Workflow

1. Confirm repository state.
   - Run `git status --short`.
   - If there are unstaged, staged, or untracked changes, do not push them implicitly. Tell the user to commit first or ask for explicit approval to run the commit workflow.
   - Run `git log --oneline -5` to understand the top commits.
   - Run `git remote -v` and identify the push remote, normally `origin`.

2. Confirm branch safety.
   - Run `git branch --show-current`.
   - Do not push directly from `main` or `master` unless the user explicitly confirms that this is intended.
   - If the branch has no upstream, plan to push with `git push -u origin HEAD`.
   - If the branch has an upstream, inspect ahead/behind state with `git status -sb` or equivalent.
   - If the branch is behind its upstream, stop and report the blocker rather than rebasing or merging without permission.

3. Run pre-push validation.
   - Run the relevant non-integration test command for this repo: `uv run --native-tls pytest -m "not integration" -p no:cacheprovider`.
   - If `uv` cache or TLS fails because of the local corporate environment, retry with a writable cache such as `UV_CACHE_DIR=C:\tmp\uv-cache` and request escalation when required.
   - If tests fail, do not push unless the user explicitly asks to push despite the failure. Report the failing command and short failure summary.

4. Run a security gate before network actions.
   - Inspect committed content and pending status for forbidden files: `.env`, `.env.*` except `.env.example`, cache directories, generated index/database files, `__pycache__`, `*.pyc`, `.pytest_cache`, `.ruff_cache`, `.venv`, and local tool state.
   - Scan the commits that will be pushed for obvious secrets and sensitive data patterns: SMB passwords, API keys, tokens, internal IPs, UNC paths, real shared-folder paths, patient names, specimen/test identifiers, and copied file listings.
   - Do not print secret or patient-like values. Report only the file, line, and issue type.
   - If sensitive data is found, stop. Do not push, create a PR, or send Slack until the leak is removed.

5. Push the branch.
   - Push only after the user request itself authorizes `/push` or `$push`.
   - Use `git push -u origin HEAD` for a new branch, otherwise `git push`.
   - Do not force-push, amend, reset, rebase, or delete remote branches unless the user explicitly requested that operation.

6. Create the Pull Request.
   - Prefer the GitHub connector/app if available; otherwise use `gh pr create`.
   - Create a draft PR by default unless the user explicitly asks for a ready-for-review PR.
   - Write the PR title in Korean and keep it concise.
   - Write the PR body in Korean with:
     - summary of functional changes,
     - validation commands and results,
     - security/privacy checks performed,
     - deployment or runtime notes,
     - known risks or follow-up items.
   - Do not include credentials, internal IPs, real SMB paths, patient/specimen/test identifiers, or raw shared-folder listings.
   - After creation, capture the PR URL, branch name, commit range or top commit, draft/ready status, and validation summary.

7. Report to Slack.
   - Use Slack tools only after the PR exists.
   - Target the AI Solution Lab workspace channel named `진검파트`.
   - If multiple matching channels exist or the workspace cannot be confirmed, ask the user before sending.
   - Send directly unless the user asked for a draft.
   - Keep the Slack report concise and Korean:
     ```text
     [automation_smb] PR 생성 완료
     - 제목: <PR title>
     - 링크: <PR URL>
     - 브랜치: <branch>
     - 검증: <test/security summary>
     - 참고: <deployment note or risk, if any>
     ```
   - Never include secrets, internal IPs, UNC paths, patient/specimen/test identifiers, or shared-folder file lists in Slack.

8. Final user report.
   - Report the pushed branch, PR URL, Slack channel report status, validation result, and any warnings.
   - Mention if Slack sending was skipped, drafted, blocked, or required user confirmation.

## Rules

- `/push` means push committed work and create a Pull Request; it does not mean deploy.
- Never push if the working tree is dirty unless the user explicitly asks to include and commit those changes first.
- Never push or PR sensitive data.
- Never push directly to production or deployment targets as part of this skill.
- Never run `git push --force` or equivalent without explicit user approval.
- Prefer fast failure with a clear blocker over a risky automated recovery.
