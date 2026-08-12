# 구현 현황

기준일: 2026-08-11

## 한눈에 보기

| 영역 | 상태 | 현재 구현 |
|---|---|---|
| Frontend/Backend 분리 | 완료 | `frontend/`, `backend/src`, `backend/tests` |
| Figma Playground 정렬 | 완료 | 헤더 71px, 3열 288/816/336px, 중앙 대화·하단 Composer 구조 |
| 자연어 파일 검색 | 완료 | 로컬 LLM 확장 + PostgreSQL·MinIO·Neo4j 통합 검색, source 계보가 달라도 동일 물리 파일은 한 건으로 통합 |
| 파일 후보 선택 | 완료 | 최대 5개, UUID pair를 채팅 요청에 전달 |
| 선택 문서 검증 | 완료 | 채팅 직전 활성 Revision 재검증, stale이면 409 |
| 선택 범위 검색 | 완료 | Hybrid/RRF, scope 위반 차단, Citation 반환 |
| 근거 답변 | 완료 | 온프레미스 Ollama native JSON, 근거 없으면 LLM 미호출 |
| 오른쪽 문서 기능 | 완료 | 선택 문서 요약과 파일 선택 후 설명을 받는 LLM 기안 초안 생성 제공 |
| 기안 초안 | 완료 | 유형별 문서 단위 관련성 선별 후 strict V2를 생성하며, 행사 참석은 참가 목적·참가 내용·행사 주요 내용만 남겨 XLSX로 신규 저장 |
| 기안 수정 | 완료 | 생성 당시 문서 snapshot과 유형을 유지한 채 피드백으로 v1.1 이상 수정본을 별도 생성하고 원본 다운로드를 보존 |
| 기안 평가 | 구조 완료 / 기준셋 미완성 | oracle 계약 검사와 gold 비노출 실제 LLM live 평가를 분리하고, 근거·내용·V2 XLSX·도구 흐름·지연을 비식별 채점. 현재 case는 사람 검토 test split이 없어 제품 품질 점수로는 부적격 |
| MinIO 보기 | 완료 | Preview/Canonical read-only, Object URI 미노출 |
| Neo4j 버전 관계 | 완료 | 각 검색 결과의 버전 확인 버튼에서 즉시 조회 |
| 저장소 상태 | 완료 | PostgreSQL·MinIO·Neo4j 연결/degraded 상태 표시 |
| 파일 첨부 | 완료 | `[업로드] 문서명_YYYYMMDD_v1.0.확장자`를 최종 이름으로 exclusive 신규 생성 후 대화 참고 파일 자동 추가 |
| 환경 설정 | 완료 | 왼쪽 하단 설정에서 업로드·기안 상대 경로와 저장소·로컬 LLM 상태 관리, 비밀·내부 주소 미노출 |
| Docker 패키징 | 완료 | Vue/FastAPI 단일 non-root image, Compose runtime env·healthcheck·LAN port |
| LAN 접속 | 부분 완료 | host LAN 주소 HTTP 200 확인, 다른 물리 PC와 Windows 방화벽 규칙은 관리자 확인 필요 |
| 지연 목표 | 미달 | 검색은 목표권, LLM 포함 채팅은 추가 개선 필요 |
| 인증·ACL·감사 | 미구현 | 운영 전 별도 설계·승인 필요 |
| LLM 기안 셀 렌더 | 완료 | 제목 `C8`, 재가 문구 `A10`, 일반 본문은 비병합·기본 높이의 독립 행, 2~6열 실제 표만 병합·wrap, 넓거나 공간 초과 표의 `세부내용` 시트 |

## 활성 Backend 경로

- `api.py`: app lifespan, Vue bundle, health
- `llmops_search.py`: 파일 후보 검색과 활성 Revision 검증
- `llmops_multistore_search.py`: 로컬 LLM 질의 확장, 세 저장소 병렬 조회와 후보 통합
- `llmops_retrieval.py`: 선택 범위 Hybrid/RRF Chunk 검색
- `llmops_artifacts.py`: PostgreSQL ACL/Revision 확인 후 MinIO 조회
- `llmops_graph.py`: Neo4j 버전 관계 조회
- `playground/document_api.py`: 현재 공개 API
- `playground/document_chat.py`: 근거 검색과 로컬 LLM 합성
- `playground/document_models.py`: 채팅 API 계약
- `playground/proposal_context.py`: 중복 제거·관련도 정렬·문서별 상한과 컨텍스트 사용량 집계
- `playground/proposal_evidence.py`: 유형별 사후 증빙을 문서 단위로 제외하고 비식별 집계를 생성
- `playground/proposal_draft.py`: 유형 판정·strict V2 생성/수정, 3필드 호환 투영, block 기반 XLSX 렌더, 원본 보존 registry와 KST 저장명 생성
- `evaluation/proposal_*.py`: 외부 기안 dataset preflight·oracle 계약 검사·gold 비노출 live 생성·100점 평가·비식별 context/지연 통계
- `playground/upload_api.py`: 공개 설정 GET/PATCH와 multipart 파일 첨부 API
- `playground/upload_service.py`: runtime 상대 경로·업로드 UUID registry, SMB create-only 쓰기/읽기와 즉시 근거 추출
- `playground/upload_models.py`: 비밀 없는 설정·첨부 응답 계약

## 현재 API

- `POST /api/playground/files/search`
- `POST /api/playground/chat`
- `GET /api/playground/files/{doc_id}/revisions/{revision_id}/artifacts/{artifact_type}`
- `GET /api/playground/files/{doc_id}/graph`
- `GET /api/playground/stores/status`
- `GET /api/playground/settings`
- `PATCH /api/playground/settings/upload`
- `PATCH /api/playground/settings/proposal-draft`
- `POST /api/playground/drafts/proposal`
- `POST /api/playground/drafts/proposal/{draft_id}/clarifications`
- `POST /api/playground/drafts/proposal/{draft_id}/revisions`
- `GET /api/playground/drafts/proposal/{draft_id}`
- `GET /api/playground/drafts/proposal`
- `POST /api/playground/files/upload`
- `GET /health`
- `GET /playground`

## 제거 대상 판정

현재 목표에서 제외되어 새 실행 경로가 참조하지 않는 묶음은 다음과 같다.

- 직접 SMB/SQLite: `content_*`, `finder.py`, `index*.py`, `smb_client.py`, `jobs.py`
- 예전 RAG·평가: `rag_search.py`, `evaluation/`의 비활성 legacy 모듈, MLflow 평가 script/data. 활성 `evaluation/proposal_*.py`는 제외
- 범용 확장 실험: `mcp_server.py`, `tooling/`, `integrations/langgraph/`
- 별도 데모: `qc_audit/`, `reports/`, Playground Tool/Skill/attachment 코드
- 관련 테스트·문서·생성 잔여물

새 runtime과 UI에서는 이미 연결을 끊었다. 물리적 영구 삭제는 대량 삭제 승인 Gate로 남겨 두며, 승인 후 한 번에 지운 뒤
전체 검증한다. `.env`, `.cache`, 실제 저장소 데이터는 정리 대상이 아니다.

## 최근 검증

- 기안 Backend Ruff 통과, draft/upload 집중 테스트 47개, draft/evaluation 결합 테스트 37개와 live runner 테스트 22개 통과
- Frontend 유형 선택·요청·수정본 카드 표시를 포함한 7개 파일 40개 테스트, typecheck, production build 통과
- 실제 Microsoft Excel read-only 개방과 PNG 렌더 통과: 일반 본문 병합 0·사용자 지정 행 높이 0, 표 논리 셀만 병합·wrap 확인
- 7열 표 합성 XLSX의 실제 Excel 개방 통과: `기안지`와 `세부내용` 2시트, appendix 3행×7열 확인
- V2 renderer oracle 계약 상한: 로컬 16/16 평균 95.3926, SMB 33/33 평균 95.3887, 렌더 hard gate 실패 0. 실제 LLM 제품 성능으로 해석하지 않음
- canonical reference 기준 gold 비노출 실제 live 평가: 로컬 14건 평균 47.7710·통과 0·XLSX 완료 9·생성 오류 5, SMB 16건 평균 47.9246·통과 0·XLSX 완료 11·생성 오류 5
- 실제 live 근거 선별: 로컬 기대 제외 12건 중 올바른 제외 0·오제외 12·누출 case 3, SMB 기대 제외 35건 중 올바른 제외 0·오제외 12·누출 case 5
- live 생성 완료 건 지연: 로컬 p50 6.63초·p95 13.32초, SMB p50 6.61초·p95 12.46초. 원격 SMB 호출·저장은 0회
- canonical preflight: 로컬 18건 중 4건, SMB 41건 중 25건은 참고 문서 없음·누락·추출 실패로 모델 호출 없이 skip. preflight/live 불일치 0건
- 전체 v4 checkpoint resume에서 manifest v2의 생성 설정 fingerprint와 완료/실패/skip·지연·점수 동일성을 확인. pending journal은 append/publish 실패를 LLM 재호출 없이 복구하며 기존 v2/v3 checkpoint는 mismatch로 재사용 차단
- 현재 59건은 모두 `candidate/needs_review`, `split=unassigned`, 사실 label 0건이므로 위 live 수치는 결함 진단값이며 제품 품질 baseline으로 승인되지 않음
- Docker 최종 재빌드·재기동: `automation-smb:local`, host port를 8013으로 명시 override→container 8011, healthy, UID 10001·read-only rootfs, live evaluator import와 Playground JS/CSS 200 확인
- 공개 OpenAPI에는 evaluation 경로·점수/dataset 필드가 0건이고 최근 컨테이너 오류 로그도 0건
- Docker 합성 수직 smoke: 행사 참고 2건과 방해 증빙 2건 첨부 → 방해 증빙 2건 제외 → 3개 핵심 구역 생성 → v1.1 수정, 원본 hash 불변, 총 7.9초
- Docker 브라우저 read-only smoke: Playground와 기안 기능 진입점 표시, console error 0건
- 프로젝트 품질: 85/100, 목표 달성·hard gate 통과. 실측 evidence가 비활성이라 freshness·retrieval/e2e·RAG 15점은 skipped

- Backend Ruff 통과, 품질 평가가 수집하는 활성 비통합 테스트 84개 통과(기존 61개 + live runner 22개 + XLSX 보기 회귀 1개)
- Backend 검색 중복 제거·선택 문서 retrieval·strict JSON 기안·다운로드·SMB create-only를 포함한 관련 테스트 59개 통과
- Frontend 테스트: 기안 설명 입력 모드·선택 파일 요청·3필드 응답·대화창 다운로드를 포함해 35개 통과
- Frontend typecheck/build: 통과
- production bundle: `backend/src/smb_finder/web/` 갱신
- Figma 기준 브라우저 확인: 1440×960에서 헤더 71px, 본문 889px, 3열 288/816/336px 일치
- 현재 앱 창 확인: 1265×1272에서 3열 유지, body 가로 overflow 없음, browser warning/error 0건
- 반응형 확인: 1024×768에서 좌측 248px+중앙 761px, 우측 패널 다음 행 배치, 가로 overflow 없음
- 기능 구성 테스트: 오른쪽 `문서 요약`·`기안 초안 작성` 2개, 생성 결과에 대화창 다운로드 카드 표시
- 기안 생성물: 1시트 템플릿의 `C8` 제목, `A10` 결재 요청, `A15` 이후 편집 가능한 독립 본문 행과 표 병합·구분선·ZIP 항목 보존 확인
- 기안 V2/core/API 합성 테스트 34건 및 평가기 합성 테스트 10건 통과
- 실제 dataset read-only oracle baseline: 로컬 16/16 평균 96.1426점, SMB 33/33 평균 96.1766점, hard gate 실패 0건
- raw reference runtime 예산 초과: 로컬 6건·SMB 19건; 모델 metadata 최대치 초과 0건, 실제 content context 오류 관측 0건
- 합성 LLM allocation smoke: `num_ctx` 24,576까지 HTTP 200, 28,672는 작은 입력도 HTTP 500이라 운영 기본값을 24,576으로 조정
- 합성 V2 live smoke: 근거 9,998자·prompt 6,239 token·4.41초·재시도 0회, section 5개와 paragraph/table strict 검증 통과
- 2026-08-11 Docker 재빌드·재기동: `automation-smb:local`, 당시 host port를 8013으로 override→container 8011, health·Playground·OpenAPI·빈 XLSX 통과
- Docker 내부 합성 V2 smoke: section 4개, prompt 371 token, 재시도 0회, 4.85초; 최근 컨테이너 오류 로그 0건
- 이번 변경은 사용자 승인 범위에서 고유 이름의 합성 첨부 4개와 기안 생성본·수정본 XLSX 2개를 신규 추가해 로컬 LLM 통합 실행까지 검증
- 공유폴더 기존 파일의 수정·덮어쓰기·이름 변경·이동·삭제는 미실행
- 버전 확인 브라우저 확인: DB 검색 후보 2건에 버튼 표시, 파일 미선택 상태에서 버전 탭 즉시 열림
- 프로젝트 품질: 85/100, 목표 달성, hard gate 통과
- 실측 evidence: `measured_evidence.enabled=false`; 저장된 합성 baseline이 없어 freshness와 retrieval/e2e/RAG 실측 15점은 `skipped`
- 실제 저장소 smoke: PostgreSQL·MinIO 연결, Neo4j 연결/degraded
- 멀티스토어 파일 검색: 3개 후보, 741.7ms, PostgreSQL·MinIO·Neo4j 조회, 로컬 LLM 확장 성공
- 중복 제거 live smoke: 당시 8013 host port override에서 `xlsx` 검색 4개 물리 파일·중복 그룹 0, warm 5회 server p50 771.5ms·p95 876.3ms
- 단계 지연: LLM 630.4ms, PostgreSQL 86.6ms, MinIO 40.9ms, Neo4j 100.5ms, 8초 예산 이내
- 선택 문서 채팅: 정상 응답, Citation 6개, 2104.9ms
- LLM 구조화 응답: 256 token 절단 실패를 확인해 간결성 지시와 500 token 상한으로 보완
- 로그 경계: `httpx`/`httpcore` INFO를 차단해 내부 endpoint가 서비스 로그에 남지 않도록 수정
- Docker image `automation-smb:local`: build·healthy·UID/GID 10001 실행·image 내 `.env` 부재 확인
- Docker 저장소 smoke: PostgreSQL·MinIO·Neo4j 모두 연결, Neo4j graph-space fallback만 degraded warning
- Docker 수직 흐름: 자연어 검색 4개 후보·2798.6ms, 세 저장소+로컬 LLM 사용, 버전 graph 6개 노드·10개 관계,
  선택 문서 채팅 Citation 6개·2831.4ms
- Docker 브라우저: 검색→선택→파일별 버전 확인→중앙 채팅 통과, console error 0건
- LAN HTTP 호환성: secure context 밖에서 `crypto.randomUUID`가 없는 브라우저도 UI ID fallback으로 검색·선택 통과
- LAN host 주소 smoke: HTTP 200 확인. 별도 물리 PC 접속과 방화벽 규칙 적용은 미검증
- 설정·첨부 API: traversal 422, 금지 확장자 415, 비덮어쓰기·실제 byte 크기·명시적 활성화·즉시 근거 추출 단위 테스트 통과
- 업로드 저장명: 한국 시간 기준 `[업로드] 문서명_YYYYMMDD_v1.0.확장자`, 기존 형식 이름의 접두사·버전 중복 방지와 확장자 보존 통과
- Docker 공개 설정: 업로드 enabled/configured, 상대 경로는 container 재시작 뒤 유지, host·username property 미노출
- SMB 연결: 자격증명·경로를 출력하지 않는 read-only 확인에서 설정된 대상 폴더 존재 확인
- 최종 브라우저: 합성 파일 첨부 직후 대화 참고 파일 1개 자동 추가, Enter 전송, Shift+Enter 줄바꿈, 근거 기본 닫힘과
  펼치기/접기 동작 확인
- 실제 SMB 수직 흐름: 승인된 합성 파일로 업로드 → `source=upload` 참조 → 원본 근거 1건 → grounded 답변 통과

현재 Playground는 Figma 초안의 정보 구조를 기준으로 자연어 DB 검색 → 파일 첨부 → 후보 선택 → 대화 참고 파일 → 하단
설정 순서로 정리했다. 파일 버전 확인은 각 검색 후보의 직접 동작이며, 오른쪽 실행 기능은 선택 문서 요약과 선택 문서 기반
기안 XLSX 생성과 피드백 기반 수정본 생성을 제공한다. 기안 생성은 사용자 설명 입력, 유형 확정, 관련 문서 선별, 근거 패킹,
로컬 LLM strict V2 JSON, 기존 3필드와 엑셀 `C8`·`A10`·`A15:A47` 호환 투영, SMB 신규 저장과 대화창 다운로드 등록을
순서대로 수행한다. 수정은 원본 문서 범위와 유형을 상속해 별도 버전으로 저장한다. 컨텍스트 제한 오류일 때만 예산을 절반으로
줄여 한 번 재시도하며 원문·경로 없는 사용량을 응답에 남긴다.

기본 Docker 서비스 주소는 `http://127.0.0.1:8011/playground`이며 컨테이너 내부 포트도 8011이다. 최근 검증 실행은
포트 충돌을 피하려 host port만 8013으로 명시 override했다.
`.env`와 `.env.docker`는 수정하지 않았다. 이번 재배포에서는 승인된 합성 첨부만으로 실제 기안 생성·수정 POST와
create-only 신규 저장을 검증했으며 기존 공유폴더 파일의 수정·덮어쓰기·이동·삭제는 수행하지 않았다.

현재 기능 흐름은 실제 저장소와 연결되어 동작한다. 다음 기안 평가 과제는 같은 업무의 초안·수정본을 `group_id` 단위로
분리하고 사람이 instruction·참고/제외 문서·필수 사실을 검토한 test split을 고정해 제품 품질 baseline을 다시 측정하는
것이다. 검색은 별도 합성 질의셋으로 retrieval·end-to-end p95와 Hit@5·no-answer·groundedness를 반복 측정한다.
