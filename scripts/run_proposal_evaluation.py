"""외부 기안 dataset의 offline 계약 검사와 온프레미스 live 평가 CLI."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from smb_finder.config import load_settings
from smb_finder.evaluation.proposal_content_judge import LocalProposalContentJudge
from smb_finder.evaluation.proposal_runner import (
    ProposalEvaluationError,
    run_proposal_evaluation,
    write_proposal_evaluation_report,
)
from smb_finder.evaluation.proposal_live_runner import run_live_proposal_predictions, write_proposal_live_report


def build_parser() -> argparse.ArgumentParser:
    """SMB는 호출하지 않고 live에서만 로컬 LLM을 호출하는 평가 인자를 정의한다."""

    parser = argparse.ArgumentParser(description="기안 생성 도구 offline·온프레미스 live 평가")
    parser.add_argument("--documents", required=True, help="외부 documents.jsonl 로컬 경로")
    parser.add_argument("--proposal-cases", required=True, help="외부 proposal_cases.jsonl 로컬 경로")
    parser.add_argument(
        "--mode",
        choices=["preflight", "oracle_contract_check", "baseline", "predictions", "live"],
        default="preflight",
    )
    parser.add_argument("--predictions", default="", help="predictions 모드의 실행 결과 JSONL")
    parser.add_argument("--checkpoint", default="", help="live raw prediction checkpoint JSONL 로컬 경로")
    parser.add_argument("--artifact-directory", default="", help="live XLSX exclusive 저장 로컬 디렉터리")
    parser.add_argument("--live-report", default="", help="live 비식별 case/artifact 보고서 exclusive 저장 경로")
    parser.add_argument("--resume", action="store_true", help="기존 live checkpoint의 완료 case를 건너뜀")
    parser.add_argument(
        "--confirm-local-sensitive-data",
        action="store_true",
        help="live 원문·생성물은 저장소 밖 로컬 경로에만 저장됨을 확인",
    )
    parser.add_argument("--max-cases", type=int, default=0, help="0이면 scope 전체")
    parser.add_argument("--deadline-seconds", type=float, default=0.0, help="0이면 전체 deadline 없음")
    parser.add_argument("--judge-model", default="", help="live 내용 45점을 판정할 온프레미스 Ollama 모델")
    parser.add_argument("--judge-num-ctx", type=int, default=32_768)
    parser.add_argument("--judge-max-tokens", type=int, default=1_024)
    parser.add_argument("--judge-timeout-ms", type=int, default=180_000)
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
    if args.mode == "live" and not (args.checkpoint and args.artifact_directory and args.live_report):
        print("live_paths_required", file=sys.stderr)
        return 2
    if args.mode == "live" and not args.confirm_local_sensitive_data:
        print("local_sensitive_data_confirmation_required", file=sys.stderr)
        return 2
    if args.mode == "live" and not args.judge_model.strip():
        print("content_judge_model_required", file=sys.stderr)
        return 2
    if args.mode != "live" and args.judge_model:
        print("content_judge_live_only", file=sys.stderr)
        return 2
    if not (4_096 <= args.judge_num_ctx <= 262_144):
        print("content_judge_num_ctx_invalid", file=sys.stderr)
        return 2
    if not (256 <= args.judge_max_tokens <= 4_096):
        print("content_judge_max_tokens_invalid", file=sys.stderr)
        return 2
    if not (1_000 <= args.judge_timeout_ms <= 600_000):
        print("content_judge_timeout_invalid", file=sys.stderr)
        return 2
    repository_root = Path(__file__).resolve().parents[1]
    if args.mode == "live" and any(
        Path(raw_path).resolve().is_relative_to(repository_root)
        for raw_path in (args.checkpoint, args.artifact_directory)
    ):
        print("live_raw_output_must_be_outside_repository", file=sys.stderr)
        return 2
    if args.mode != "live" and (
        args.checkpoint
        or args.artifact_directory
        or args.live_report
        or args.resume
        or args.confirm_local_sensitive_data
        or args.max_cases
        or args.deadline_seconds
    ):
        print("live_options_not_allowed", file=sys.stderr)
        return 2
    try:
        if args.mode == "live":
            settings = load_settings()
            content_judge = LocalProposalContentJudge(
                settings.ollama_base_url,
                args.judge_model,
                num_ctx=args.judge_num_ctx,
                max_tokens=args.judge_max_tokens,
                timeout_ms=args.judge_timeout_ms,
            )
            try:
                live_report = run_live_proposal_predictions(
                    args.documents,
                    args.proposal_cases,
                    checkpoint_path=args.checkpoint,
                    artifact_directory=args.artifact_directory,
                    scope=args.scope,
                    max_cases=args.max_cases or None,
                    deadline_seconds=args.deadline_seconds or None,
                    resume=args.resume,
                    settings=settings,
                    content_judge=content_judge,
                )
            finally:
                content_judge.close()
            write_proposal_live_report(args.live_report, live_report)
        report = run_proposal_evaluation(
            args.documents,
            args.proposal_cases,
            mode=args.mode,
            predictions_path=args.checkpoint if args.mode == "live" else args.predictions or None,
            scope=args.scope,
            runtime_context_window_tokens=args.runtime_context_window_tokens,
            reserved_prompt_tokens=args.reserved_prompt_tokens,
            reserved_output_tokens=args.reserved_output_tokens,
            model_context_limit_tokens=args.model_context_limit_tokens or None,
            pass_threshold=args.pass_threshold,
            hard_gate_score_cap=args.hard_gate_score_cap,
        )
        write_proposal_evaluation_report(args.output, report, exclusive=args.mode == "live")
    except ProposalEvaluationError as exc:
        print(str(exc).split(":", maxsplit=1)[0], file=sys.stderr)
        return 2
    summary = {
        "mode": report.mode,
        "evaluation_kind": report.evaluation_kind,
        "product_quality_eligible": report.product_quality_eligible,
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
        "evidence_filter_evaluated_case_count": report.aggregate.evidence_filter.evaluated_case_count,
        "evidence_filter_failed_case_count": report.aggregate.evidence_filter.failed_case_count,
        "content_judge_completed_case_count": report.aggregate.content_judge.completed_case_count,
        "content_judge_failed_case_count": report.aggregate.content_judge.failed_case_count,
        "content_judge_average_score": report.aggregate.content_judge.average_total_score,
        "event_structure_failed_case_count": report.aggregate.supplemental.event_failed_case_count,
        "revision_failed_case_count": report.aggregate.supplemental.revision_failed_case_count,
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    if args.fail_on_quality and report.aggregate.failed_case_count:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
