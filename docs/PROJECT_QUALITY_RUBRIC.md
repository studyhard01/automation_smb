# 프로젝트 품질 Rubric

현재 품질 기준은 DB 기반 `검색 → 선택 → 근거 대화` 수직 흐름에만 적용한다. 기계 판독 원본은
[`config/project_quality_rubric.json`](../config/project_quality_rubric.json)이다.

## 점수와 Gate

| 영역 | 배점 |
|---|---:|
| 보안·데이터 경계 | 25 |
| 기능·회귀 | 20 |
| 지연·성능 | 20 |
| 코드·계약 | 15 |
| 문서·맥락 | 10 |
| 운영·관측성 | 10 |

- 목표: 85점
- 기본 실행: hard gate가 하나라도 실패하면 종료 코드 1
- `--strict-score`: hard gate 통과와 85점 이상을 모두 요구
- 현재 수직 흐름의 새 합성 baseline은 아직 없으므로 실측 15점은 `skipped`로 0점 처리한다.
- 제거된 RagVectorSearcher/MLflow 실험값은 현재 구현 점수로 재사용하지 않는다.

## Hard gate

- `.env`, DB 파일, cache, 자격증명, 내부 주소 비커밋
- PostgreSQL·MinIO·Neo4j read-only marker와 mutation 부재
- 활성 Backend pytest와 Ruff
- DB fast path, timeout, `elapsed_ms`
- Pydantic 검색·채팅 계약과 활성 Revision 재검증
- Markdown 링크와 canonical 문서 정합성
- CI·pre-commit·개발 교훈 검사 연결
- 오류 문자열 대신 안전한 error code와 예외 type 로깅

## 실측 재개 조건

합성 LLMOps 데이터셋에서 동일 질의셋을 고정한 뒤 다음 값을 새로 기록한다.

- 파일 검색 p50/p95
- 선택 문서 retrieval p50/p95
- end-to-end p50/p95
- Hit@5, no-answer accuracy, groundedness
- dataset fingerprint, 실행 설정, 측정일

현재 `measured_evidence.enabled=false`이며 이 상태는 evidence freshness와 목표 미달 항목으로 완료 보고에 명시한다.

## 실행

```powershell
uv run --no-sync python scripts/evaluate_quality.py
uv run --no-sync python scripts/evaluate_quality.py --static-only
uv run --no-sync python scripts/evaluate_quality.py --strict-score
uv run --no-sync python scripts/evaluate_quality.py --json-output .tmp/quality/report.json
```

`--static-only`는 Ruff와 pytest를 생략하므로 완료 판정이 아니다.
