"""합성 evaluation candidate를 사람이 검토하고 승격 전 결정을 기록하는 CLI."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

from smb_finder.evaluation.models import GoldenCase

DEFAULT_DATASET = Path("data/evaluation/document_chatbot_candidates.jsonl")
DECISIONS = ("approve", "revise", "reject")


class CandidateReviewError(ValueError):
    """검토 입력이나 candidate 파일 계약 오류."""


def _load_cases(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise CandidateReviewError(f"candidate 파일을 읽을 수 없습니다: {path}") from exc

    cases: list[dict[str, Any]] = []
    for line_number, raw in enumerate(lines, start=1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        try:
            payload = json.loads(raw)
            case = GoldenCase.model_validate(payload)
        except (json.JSONDecodeError, ValueError) as exc:
            raise CandidateReviewError(f"candidate {line_number}번째 줄이 유효하지 않습니다: {exc}") from exc
        if case.provenance.review_status != "candidate":
            raise CandidateReviewError(f"candidate 파일에는 review_status=candidate만 허용합니다: {case.case_id}")
        cases.append(payload)
    if not cases:
        raise CandidateReviewError("검토할 candidate가 없습니다.")
    return cases


def _summary(case: dict[str, Any]) -> str:
    provenance = case["provenance"]
    decision = provenance.get("review_decision") or "pending"
    category = case.get("tags", {}).get("category", "")
    return f"[{decision}] {case['case_id']} | {category} | {case['inputs']['question']}"


def record_review(
    path: Path,
    *,
    case_id: str,
    decision: str,
    reviewed_on: str,
    notes: str = "",
    replace: bool = False,
) -> dict[str, Any]:
    """한 candidate의 사람 검토 결정을 원자적으로 기록한다."""

    if decision not in DECISIONS:
        raise CandidateReviewError(f"지원하지 않는 decision입니다: {decision}")
    try:
        date.fromisoformat(reviewed_on)
    except ValueError as exc:
        raise CandidateReviewError("reviewed_on은 YYYY-MM-DD 형식이어야 합니다.") from exc
    normalized_notes = " ".join(notes.split())
    if len(normalized_notes) > 500:
        raise CandidateReviewError("notes는 500자 이하여야 합니다.")
    if decision == "revise" and not normalized_notes:
        raise CandidateReviewError("revise 결정에는 수정할 내용을 notes로 적어야 합니다.")

    cases = _load_cases(path)
    selected: dict[str, Any] | None = None
    for case in cases:
        if case["case_id"] != case_id:
            continue
        provenance = case["provenance"]
        if provenance.get("review_decision") and not replace:
            raise CandidateReviewError("이미 검토된 candidate입니다. 덮어쓰려면 --replace를 사용하세요.")
        provenance["review_decision"] = decision
        provenance["reviewed_on"] = reviewed_on
        provenance["notes"] = normalized_notes
        selected = case
        break
    if selected is None:
        raise CandidateReviewError(f"case_id를 찾을 수 없습니다: {case_id}")

    for case in cases:
        GoldenCase.model_validate(case)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(
            "\n".join(json.dumps(case, ensure_ascii=False, separators=(",", ":")) for case in cases) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise CandidateReviewError(f"candidate 검토 결과를 저장할 수 없습니다: {path}") from exc
    return selected


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="합성 MLflow evaluation candidate 사람 검토")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--case-id", default="", help="비우면 전체 검토 queue를 출력")
    parser.add_argument("--decision", choices=DECISIONS)
    parser.add_argument("--reviewed-on", default=date.today().isoformat())
    parser.add_argument("--notes", default="")
    parser.add_argument("--replace", action="store_true", help="기존 결정을 명시적으로 덮어씀")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cases = _load_cases(args.dataset)
        if not args.case_id:
            for case in cases:
                print(_summary(case))
            return 0
        selected = next((case for case in cases if case["case_id"] == args.case_id), None)
        if selected is None:
            raise CandidateReviewError(f"case_id를 찾을 수 없습니다: {args.case_id}")
        if not args.decision:
            print(json.dumps(selected, ensure_ascii=False, indent=2))
            return 0
        reviewed = record_review(
            args.dataset,
            case_id=args.case_id,
            decision=args.decision,
            reviewed_on=args.reviewed_on,
            notes=args.notes,
            replace=args.replace,
        )
        print(_summary(reviewed))
        print("검토 결정만 기록했습니다. golden baseline 승격은 별도 변경과 검증이 필요합니다.")
        return 0
    except CandidateReviewError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
