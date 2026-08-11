"""외부 기안 dataset의 context preflight·oracle baseline·prediction 평가 CLI."""

from __future__ import annotations

import argparse
import json
import sys

from smb_finder.evaluation.proposal_runner import (
    ProposalEvaluationError,
    run_proposal_evaluation,
    write_proposal_evaluation_report,
)


def build_parser() -> argparse.ArgumentParser:
    """SMB나 LLM을 호출하지 않는 기안 평가 인자를 정의한다."""

    parser = argparse.ArgumentParser(description="기안 생성 도구 오프라인 평가")
    parser.add_argument("--documents", required=True, help="외부 documents.jsonl 로컬 경로")
    parser.add_argument("--proposal-cases", required=True, help="외부 proposal_cases.jsonl 로컬 경로")
    parser.add_argument("--mode", choices=["preflight", "baseline", "predictions"], default="preflight")
    parser.add_argument("--predictions", default="", help="predictions 모드의 실행 결과 JSONL")
    parser.add_argument("--scope", choices=["all", "current_tool", "structure_only"], default="all")
    parser.add_argument("--runtime-context-window-tokens", type=int, default=24_576)
    parser.add_argument("--reserved-prompt-tokens", type=int, default=1_024)
    parser.add_argument("--reserved-output-tokens", type=int, default=4_096)
    parser.add_argument(
        "--model-context-limit-tokens",
        type=int,
        default=0,
        help="0이면 미상. 모델 metadata 한도이며 runtime budget과 별도 집계",
    )
    parser.add_argument("--pass-threshold", type=float, default=80.0)
    parser.add_argument("--hard-gate-score-cap", type=float, default=49.0)
    parser.add_argument("--output", required=True, help="비식별 평가 JSON 저장 경로")
    parser.add_argument("--fail-on-quality", action="store_true", help="평가 실패 case가 있으면 종료 코드 1")
    return parser


def main(argv: list[str] | None = None) -> int:
    """평가 후 본문·경로 없는 요약만 stdout에 출력한다."""

    args = build_parser().parse_args(argv)
    if args.mode == "predictions" and not args.predictions:
        print("predictions_required", file=sys.stderr)
        return 2
    if args.mode != "predictions" and args.predictions:
        print("predictions_not_allowed", file=sys.stderr)
        return 2
    try:
        report = run_proposal_evaluation(
            args.documents,
            args.proposal_cases,
            mode=args.mode,
            predictions_path=args.predictions or None,
            scope=args.scope,
            runtime_context_window_tokens=args.runtime_context_window_tokens,
            reserved_prompt_tokens=args.reserved_prompt_tokens,
            reserved_output_tokens=args.reserved_output_tokens,
            model_context_limit_tokens=args.model_context_limit_tokens or None,
            pass_threshold=args.pass_threshold,
            hard_gate_score_cap=args.hard_gate_score_cap,
        )
        write_proposal_evaluation_report(args.output, report)
    except ProposalEvaluationError as exc:
        print(str(exc).split(":", maxsplit=1)[0], file=sys.stderr)
        return 2
    summary = {
        "mode": report.mode,
        "dataset_fingerprint": report.dataset_fingerprint,
        "total_case_count": report.context.total_case_count,
        "evaluated_case_count": report.aggregate.evaluated_case_count,
        "skipped_case_count": report.context.skipped_reference_missing_count,
        "passed_case_count": report.aggregate.passed_case_count,
        "failed_case_count": report.aggregate.failed_case_count,
        "average_total_score": report.aggregate.average_total_score,
        "runtime_over_budget_count": report.context.runtime_over_budget_count,
        "model_over_limit_count": report.context.model_over_limit_count,
        "context_failure_count": report.aggregate.context_failures.failure_count,
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    if args.fail_on_quality and report.aggregate.failed_case_count:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
