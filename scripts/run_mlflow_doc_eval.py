"""합성 golden dataset으로 문서 RAG retrieval 또는 실제 agent 평가를 실행한다."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from smb_finder.config import load_settings
from smb_finder.evaluation.datasets import GoldenDatasetError, load_golden_dataset
from smb_finder.evaluation.document_chatbot import (
    CorpusSnapshotError,
    capture_corpus_snapshot,
    run_retrieval_evaluation,
)
from smb_finder.evaluation.end_to_end import run_end_to_end_evaluation
from smb_finder.evaluation.mlflow_adapter import MlflowEvaluationError, MlflowEvaluationLogger
from smb_finder.evaluation.models import EndToEndEvaluationReport
from smb_finder.evaluation.tracing import MlflowTraceObserver
from smb_finder.playground.agent import PlaygroundAgent
from smb_finder.playground.skills import SkillStore
from smb_finder.playground.tools import PlaygroundRuntime, build_tool_registry
from smb_finder.rag_search import RagVectorSearcher
from smb_finder.telemetry import NoopTraceObserver


def build_parser() -> argparse.ArgumentParser:
    """Phase 1 retrieval과 Phase 2 end-to-end 평가 CLI parser를 만든다."""

    parser = argparse.ArgumentParser(description="문서 챗봇 MLflow 평가")
    parser.add_argument("--mode", choices=["retrieval", "end-to-end"], default="retrieval")
    parser.add_argument(
        "--dataset",
        default="data/evaluation/document_chatbot_golden.jsonl",
        help="합성 golden JSONL 경로",
    )
    parser.add_argument("--dataset-version", default="synthetic-smoke-v1")
    parser.add_argument("--provider", choices=["openai", "local"], default="openai")
    parser.add_argument("--model", default="gpt-4.1-mini", help="비교 run metadata용 생성 모델")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--skill-id", default="rag-grounded-answer")
    parser.add_argument("--max-cases", type=int, default=0, help="0이면 전체, 양수면 앞에서부터 일부 case만 실행")
    parser.add_argument(
        "--include-trace-content",
        action="store_true",
        help="합성 평가 trace에 질문·답변·chunk 본문을 포함",
    )
    parser.add_argument("--tracking-uri", default="")
    parser.add_argument("--experiment-name", default="")
    parser.add_argument("--run-name", default="")
    parser.add_argument("--output", default="", help="전체 평가 JSON을 추가 저장할 경로")
    parser.add_argument("--no-mlflow", action="store_true", help="MLflow 기록 없이 로컬 metric만 계산")
    return parser


def _default_run_name(mode: str, model: str, top_k: int) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_model = model.replace("/", "-").replace(":", "-")
    return f"{mode}-{safe_model}-k{top_k}-{timestamp}"


def main(argv: list[str] | None = None) -> int:
    """Tracking preflight 후 선택한 평가를 실행하고 요약 JSON을 출력한다."""

    args = build_parser().parse_args(argv)
    settings = load_settings()
    if args.top_k < 1 or args.top_k > settings.rag_db_max_limit:
        print(f"--top-k는 1~{settings.rag_db_max_limit} 범위여야 합니다.", file=sys.stderr)
        return 2
    if args.max_cases < 0:
        print("--max-cases는 0 이상의 값이어야 합니다.", file=sys.stderr)
        return 2

    try:
        dataset = load_golden_dataset(args.dataset, version=args.dataset_version)
        if args.max_cases:
            dataset = dataset.model_copy(update={"cases": dataset.cases[: args.max_cases]})
        logger = None
        server_version = ""
        if not args.no_mlflow:
            logger = MlflowEvaluationLogger(
                tracking_uri=args.tracking_uri or settings.mlflow_tracking_uri,
                experiment_name=args.experiment_name or settings.mlflow_experiment_name,
                timeout_ms=settings.mlflow_tracking_timeout_ms,
            )
            server_version = logger.preflight()

        corpus = capture_corpus_snapshot(settings)
        skill = SkillStore(settings.playground_skills_dir).get(args.skill_id) if args.skill_id else None
        skill_fingerprint = (
            hashlib.sha256(skill.document.encode("utf-8")).hexdigest()[:16] if skill is not None else ""
        )
        run_id = ""
        trace_count = 0
        parameters = {
            "provider": args.provider,
            "model": args.model,
            "top_k": args.top_k,
            "skill_id": args.skill_id,
            "skill_fingerprint": skill_fingerprint,
            "evaluated_case_count": len(dataset.cases),
            "dataset_name": dataset.name,
            "dataset_version": dataset.version,
            "dataset_fingerprint": dataset.fingerprint,
            "corpus_fingerprint": corpus.fingerprint,
            "corpus_document_count": corpus.document_count,
            "corpus_chunk_count": corpus.chunk_count,
            "embedding_models": ",".join(corpus.embedding_models),
            "mlflow_server_version": server_version,
            "judge_enabled": False,
            "trace_content_included": bool(
                args.include_trace_content or settings.mlflow_trace_include_content
            ),
        }
        run_name = args.run_name or _default_run_name(args.mode, args.model, args.top_k)

        if args.mode == "retrieval":
            searcher = RagVectorSearcher(settings)
            try:
                report = run_retrieval_evaluation(
                    dataset,
                    searcher,
                    corpus=corpus,
                    top_k_override=args.top_k,
                )
            finally:
                searcher.close()
            if logger is not None:
                run_id = logger.log_report(
                    report,
                    run_name=run_name,
                    parameters=parameters,
                    tags={"phase": "retrieval-only", "dataset_version": dataset.version},
                )
        else:
            eval_settings = settings.model_copy(
                update={
                    "rag_db_default_limit": args.top_k,
                    "langsmith_tracing": False,
                }
            )

            def execute_end_to_end(
                observer: NoopTraceObserver | MlflowTraceObserver,
            ) -> EndToEndEvaluationReport:
                searcher = RagVectorSearcher(eval_settings, trace_observer=observer)
                try:
                    registry = build_tool_registry(PlaygroundRuntime(settings=eval_settings, rag_searcher=searcher))
                    agent = PlaygroundAgent(eval_settings, trace_observer=observer)
                    return run_end_to_end_evaluation(
                        dataset,
                        agent,
                        registry,
                        corpus=corpus,
                        provider=args.provider,
                        model=args.model,
                        skill_id=args.skill_id,
                        skill_fingerprint=skill_fingerprint,
                        openai_api_key=eval_settings.openai_api_key,
                        trace_observer=observer,
                    )
                finally:
                    searcher.close()

            if logger is None:
                report = execute_end_to_end(NoopTraceObserver())
            else:
                with logger.evaluation_run(
                    run_name=run_name,
                    parameters=parameters,
                    tags={"phase": "end-to-end", "evaluation_mode": args.mode},
                ) as session:
                    observer = MlflowTraceObserver(
                        session.mlflow,
                        run_id=session.run_id,
                        include_content=bool(
                            args.include_trace_content or settings.mlflow_trace_include_content
                        ),
                    )
                    report = execute_end_to_end(observer)
                    session.log_report(report, artifact_name="end_to_end_evaluation_results.json")
                    session.mlflow.log_metric("trace_error_count", float(len(observer.errors)))
                    run_id = session.run_id
                trace_count = logger.trace_count(run_id)

        if args.output:
            output_path = Path(args.output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")

        print(
            json.dumps(
                {
                    "run_id": run_id or None,
                    "mlflow_logged": bool(run_id),
                    "dataset": dataset.name,
                    "dataset_version": dataset.version,
                    "corpus_fingerprint": corpus.fingerprint,
                    "trace_count": trace_count,
                    "aggregate": report.aggregate.model_dump(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except (GoldenDatasetError, CorpusSnapshotError, MlflowEvaluationError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
