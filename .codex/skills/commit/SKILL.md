---
name: commit
description: Safely create a git commit for this repository. Use when the user says "/commit", "커밋해줘", "commit 해줘", or asks Codex to commit current work; inspect status and staged diffs, block secrets and generated sensitive data, write a concise Korean explanation of the changes in the commit message, include the local commit timestamp, and never push unless separately requested.
---

# Safe Commit

현재 저장소의 변경사항을 안전하게 커밋한다. `automation_smb`는 사내 SMB 공유폴더와 민감 의료 데이터를 다룰 수 있으므로, 커밋 속도보다 보안 점검을 우선한다.

## Workflow

1. 현재 변경사항을 확인한다.
   - `git status --short`로 변경 파일을 확인한다.
   - `git diff --stat`로 변경 규모를 확인한다.
   - 커밋할 변경이 없으면 커밋하지 않고 사용자에게 알린다.

2. 커밋 범위를 정한다.
   - 사용자가 특정 파일이나 범위를 지정했으면 그 범위만 stage한다.
   - 별도 지정이 없으면 안전 점검을 전제로 현재 작업물을 stage한다.
   - stage 후 `git diff --cached --name-only`로 staged 파일 목록을 반드시 확인한다.

3. 커밋 금지 대상을 차단한다.
   - staged 목록에 `.env`가 정확히 포함되면 즉시 unstage하고 커밋을 중단한다.
   - `.cache/`, `.tmp/`, `.venv/`, `.pytest_cache/`, `.ruff_cache/`, `__pycache__/`, `*.pyc`, `*.index.json`, 생성된 인덱스/캐시/민감 데이터 덤프가 포함되면 unstage하고 사용자에게 제외 사실을 알린다.
   - `.env.example`은 비어 있는 예시 값만 포함할 때만 허용한다.

4. staged diff를 보안 관점에서 읽는다.
   - `git diff --cached`를 확인한다.
   - SMB 자격증명, 비밀번호, API 키, 토큰, 내부 IP/호스트, 환자/검체/검사 데이터, 실제 공유폴더 파일 목록이나 경로가 보이면 커밋을 중단한다.
   - 민감값은 사용자에게 그대로 출력하지 않는다. 파일명과 문제 유형만 말한다.

5. 커밋 메시지를 작성한다.
   - 제목 형식: `<type>: <짧은 한국어 요약>`
   - type은 `feat`, `fix`, `docs`, `refactor`, `test`, `chore` 중 가장 알맞은 것을 고른다.
   - 본문은 한국어 bullet로 작성하고, 파일/모듈 단위로 무엇이 왜 바뀌었는지 간단히 설명한다.
   - 로컬 시간은 PowerShell에서 `Get-Date -Format "yyyy-MM-dd HH:mm zzz"`로 구한다.
   - 본문 끝에 아래 항목을 넣는다.
     ```text
     Commit-Date: <YYYY-MM-DD HH:mm +HH:mm>

     Co-Authored-By: Codex <noreply@openai.com>
     ```

6. 커밋을 만든다.
   - 메시지가 여러 줄이면 임시 메시지 파일을 사용해 quoting 문제를 피한다.
   - `git commit -F <message-file>` 또는 동등하게 안전한 방식을 사용한다.
   - `--no-verify`는 사용하지 않는다. hook 실패 시 원인을 확인해 고치거나 사용자에게 blocker를 보고한다.

7. 결과를 확인하고 보고한다.
   - `git log --oneline -1`로 생성된 커밋을 확인한다.
   - 사용자에게 커밋 hash와 제목을 알려준다.

## Rules

- `git push`는 사용자가 별도로 명시적으로 요청한 경우에만 실행한다.
- commit amend, rebase, reset, force push 같은 이력 변경은 사용자가 명시적으로 요청한 경우에만 수행한다.
- unrelated user changes를 되돌리지 않는다.
- 커밋 메시지 본문은 한국어로 쓴다. 파일명, 함수명, 식별자는 원문 그대로 둔다.
- 보안 차단으로 커밋하지 못한 경우, 차단 사유와 사용자가 다음에 해야 할 일을 짧게 보고한다.
