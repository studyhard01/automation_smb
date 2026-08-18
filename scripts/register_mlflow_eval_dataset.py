"""검증 golden 또는 candidate JSONL을 로컬 MLflow Evaluation Dataset으로 등록한다."""

from __future__ import annotations

import argparse
import json
import sys

from smb_finder.config import load_settings
from smb_finder.evaluation.datasets import (
    GoldenDatasetError,
    load_candidate_dataset,
    load_golden_dataset,
)
from smb_finder.evaluation.document_chatbot import CorpusSnapshotError, capture_corpus_snapshot
from smb_finder.evaluation.managed_datasets import register_managed_dataset
from smb_finder.evaluation.mlflow_adapter import MlflowEvaluationError, MlflowEvaluationLogger


_DATASET_DEFAULTS = {
    "golden": (
        "data/evaluation/document_chatbot_golden.jsonl",
        "synthetic-golden-v2",
        "automation-smb-doc-chatbot-golden",
    ),
    "candidate": (
        "data/evaluation/document_chatbot_candidates.jsonl",
        "synthetic-candidates-v1",
        "automation-smb-doc-chatbot-candidates",
    ),
}


def build_parser() -> argparse.ArgumentParser:
    """Dataset tier별 기본값을 제공하는 등록 CLI parser를 만든다."""

    parser = argparse.ArgumentParser(description="MLflow Evaluation Dataset 등록")
    parser.add_argument("--tier", choices=["golden", "candidate"], default="golden")
    parser.add_argument("--dataset", default="")
    parser.add_argument("--dataset-version", default="")
    parser.add_argument("--dataset-name", default="")
    parser.add_argument("--tracking-uri", default="")
    parser.add_argument("--experiment-name", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Tracking preflight 후 JSONL record를 idempotent하게 등록한다."""

    args = build_parser().parse_args(argv)
    default_path, default_version, default_name = _DATASET_DEFAULTS[args.tier]
    dataset_path = args.dataset or default_path
    dataset_version = args.dataset_version or default_version
    dataset_name = args.dataset_name or default_name
    settings = load_settings()

    try:
        if args.tier == "golden":
            dataset = load_golden_dataset(dataset_path, version=dataset_version)
        else:
            dataset = load_candidate_dataset(dataset_path, version=dataset_version)
        corpus = capture_corpus_snapshot(settings)
        logger = MlflowEvaluationLogger(
            tracking_uri=args.tracking_uri or settings.mlflow_tracking_uri,
            experiment_name=args.experiment_name or settings.mlflow_experiment_name,
            timeout_ms=settings.mlflow_tracking_timeout_ms,
        )
        server_version = logger.preflight()
        try:
            registration = register_managed_dataset(
                logger.load_mlflow(),
                tracking_uri=logger.tracking_uri,
                experiment_name=logger.experiment_name,
                dataset_name=dataset_name,
                dataset=dataset,
                corpus=corpus,
            )
        except Exception as exc:
            raise MlflowEvaluationError(f"MLflow Evaluation Dataset 등록에 실패했습니다: {exc}") from exc
        print(
            json.dumps(
                {
                    **registration.__dict__,
                    "mlflow_server_version": server_version,
                    "corpus_fingerprint": corpus.fingerprint,
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
