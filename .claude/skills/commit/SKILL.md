---
name: commit
description: 현재 작업물을 안전하게 git 커밋한다. 변경 사항 요약과 커밋 날짜를 메시지에 함께 기록한다. 사용자가 "/commit" 또는 "커밋해줘"라고 할 때 사용.
---

# /commit — 변경 사항 + 날짜 기록 커밋

이 저장소의 작업물을 커밋한다. 이 프로젝트는 **사내 공유폴더의 민감 의료 데이터**를 다루므로,
저장소 경계와 보안 규칙(`AGENTS.md`)을 속도보다 우선해 지킨다.

## 절차

1. **현황 파악**
   - `git status --short` 와 `git diff --stat` 으로 무엇이 바뀌었는지 확인한다.
   - 커밋할 게 없으면 그대로 알리고 멈춘다.

2. **스테이징 (비밀정보 차단)**
   - `git add -A` 로 스테이징한다(`.gitignore`가 `.env`·`*.index.json`·`.cache/`·`data/`를 막아준다).
   - 스테이징 후 **반드시 검증**한다:
     - `git diff --cached --name-only` 출력에 `.env`(정확히 그 이름)나 인덱스 캐시(`*.index.json`)가 있으면
       **즉시 멈추고** 해당 파일을 `git restore --staged <file>` 로 빼낸 뒤 사용자에게 알린다.
     - `git diff --cached` 에 SMB 자격증명·비밀번호·내부 IP가 평문으로 보이면 **멈추고 알린다**(커밋 금지).

3. **날짜 확보**
   - 셸에서 `date '+%Y-%m-%d %H:%M %z'` 로 현재 시각을 구한다(스크립트 환경엔 시간 함수가 없으니 셸에서 받는다).

4. **메시지 작성 후 커밋**
   - 제목: `<type>: <한국어 한 줄 요약>` (type 예: feat/fix/docs/refactor/test/chore).
   - 본문: **변경 사항을 항목별로** 한국어로 적는다(파일/모듈 단위 무엇이 왜 바뀌었는지).
   - 본문 끝에 아래 두 줄을 **반드시** 넣는다:
     ```
     Commit-Date: <위에서 구한 YYYY-MM-DD HH:MM +ZZZZ>

     Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
     ```
   - heredoc로 커밋한다(PowerShell이 아니라 Bash 도구 사용):
     ```bash
     DATE=$(date '+%Y-%m-%d %H:%M %z')
     git commit -m "$(cat <<EOF
     <type>: <요약>

     - <변경 항목 1>
     - <변경 항목 2>

     Commit-Date: $DATE

     Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
     EOF
     )"
     ```

5. **결과 보고**
   - `git log --oneline -1` 로 커밋 해시를 확인해 사용자에게 보고한다.

## 규칙

- **푸시는 하지 않는다.** `git push`·원격 배포는 사용자가 명시적으로 요청할 때만(이 스킬 범위 밖).
- 기본은 main에 직접 커밋해도 되지만, 사용자가 브랜치를 원하면 따른다.
- 훅을 건너뛰지 않는다(`--no-verify` 금지). 훅 실패 시 원인을 고친다.
- 메시지 본문은 한국어, 파일명·식별자는 영문 그대로.
