"""합성 golden dataset으로 문서 RAG retrieval 평가를 실행한다."""

from __future__ import annotations

import argparse
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
from smb_finder.evaluation.mlflow_adapter import MlflowEvaluationError, MlflowEvaluationLogger
from smb_finder.rag_search import RagVectorSearcher


def build_parser() -> argparse.ArgumentParser:
    """Phase 1 retrieval-only 평가 CLI parser를 만든다."""

    parser = argparse.ArgumentParser(description="문서 챗봇 retrieval-only MLflow 평가")
    parser.add_argument("--mode", choices=["retrieval"], default="retrieval")
    parser.add_argument(
        "--dataset",
        default="data/evaluation/document_chatbot_golden.jsonl",
        help="합성 golden JSONL 경로",
    )
    parser.add_argument("--dataset-version", default="synthetic-smoke-v1")
    parser.add_argument("--provider", default="openai", help="비교 run metadata용 provider")
    parser.add_argument("--model", default="gpt-4.1-mini", help="비교 run metadata용 생성 모델")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--tracking-uri", default="")
    parser.add_argument("--experiment-name", default="")
    parser.add_argument("--run-name", default="")
    parser.add_argument("--output", default="", help="전체 평가 JSON을 추가 저장할 경로")
    parser.add_argument("--no-mlflow", action="store_true", help="MLflow 기록 없이 로컬 metric만 계산")
    return parser


def _default_run_name(model: str, top_k: int) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_model = model.replace("/", "-").replace(":", "-")
    return f"retrieval-{safe_model}-k{top_k}-{timestamp}"


def main(argv: list[str] | None = None) -> int:
    """Tracking preflight 후 retrieval 평가를 실행하고 요약 JSON을 출력한다."""

    args = build_parser().parse_args(argv)
    settings = load_settings()
    if args.top_k < 1 or args.top_k > settings.rag_db_max_limit:
        print(f"--top-k는 1~{settings.rag_db_max_limit} 범위여야 합니다.", file=sys.stderr)
        return 2

    try:
        dataset = load_golden_dataset(args.dataset, version=args.dataset_version)
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

        run_id = ""
        if logger is not None:
            run_id = logger.log_report(
                report,
                run_name=args.run_name or _default_run_name(args.model, args.top_k),
                parameters={
                    "provider": args.provider,
                    "model": args.model,
                    "top_k": args.top_k,
                    "dataset_name": dataset.name,
                    "dataset_version": dataset.version,
                    "dataset_fingerprint": dataset.fingerprint,
                    "corpus_fingerprint": corpus.fingerprint,
                    "corpus_document_count": corpus.document_count,
                    "corpus_chunk_count": corpus.chunk_count,
                    "embedding_models": ",".join(corpus.embedding_models),
                    "mlflow_server_version": server_version,
                    "judge_enabled": False,
                },
                tags={"phase": "retrieval-only", "dataset_version": dataset.version},
            )

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
