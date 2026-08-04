"""기존 RagVectorSearcher를 재사용하는 retrieval-only 평가 실행기."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

import psycopg

from smb_finder.config import Settings
from smb_finder.rag_search import RagSearchError

from .models import CaseEvaluationResult, CorpusSnapshot, EvaluationReport, GoldenDataset
from .scorers import aggregate_case_results, build_retrieved_chunks, score_retrieval_case


class CorpusSnapshotError(RuntimeError):
    """corpus fingerprint 계산 실패."""


def capture_corpus_snapshot(
    settings: Settings,
    *,
    connect: Callable[..., Any] | None = None,
) -> CorpusSnapshot:
    """본문·파일 경로를 가져오지 않고 hash와 개수만으로 corpus를 식별한다."""

    connector = connect or psycopg.connect
    try:
        with connector(
            host=settings.rag_db_host,
            port=settings.rag_db_port,
            dbname=settings.rag_db_name,
            user=settings.rag_db_user,
            password=settings.rag_db_password,
            connect_timeout=max(1, settings.rag_db_connect_timeout_ms // 1000),
            application_name="automation_smb_mlflow_eval",
            options="-c default_transaction_read_only=on",
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT d.id, COALESCE(d.file_hash, ''), c.chunk_index,
                           COALESCE(c.content_hash, ''), COALESCE(c.embedding_model, '')
                    FROM documents AS d
                    JOIN document_chunks AS c ON c.document_id = d.id
                    WHERE d.deleted_at IS NULL
                    ORDER BY d.id, c.chunk_index
                    """
                )
                rows = cursor.fetchall()
    except Exception as exc:
        raise CorpusSnapshotError("로컬 RAG corpus fingerprint를 계산하지 못했습니다.") from exc

    digest = hashlib.sha256()
    document_ids: set[int] = set()
    models: set[str] = set()
    for document_id, file_hash, chunk_index, content_hash, embedding_model in rows:
        document_ids.add(int(document_id))
        if embedding_model:
            models.add(str(embedding_model))
        digest.update(f"{document_id}|{file_hash}|{chunk_index}|{content_hash}|{embedding_model}\n".encode())
    return CorpusSnapshot(
        fingerprint=digest.hexdigest()[:16],
        document_count=len(document_ids),
        chunk_count=len(rows),
        embedding_models=sorted(models),
    )


def run_retrieval_evaluation(
    dataset: GoldenDataset,
    searcher: Any,
    *,
    corpus: CorpusSnapshot,
    top_k_override: int | None = None,
) -> EvaluationReport:
    """RAG 검색이 기대되는 case만 실행하고 결정적 지표를 집계한다."""

    results: list[CaseEvaluationResult] = []
    for case in dataset.cases:
        expected_tools = [call.name for call in case.expectations.expected_tool_calls]
        top_k = top_k_override or case.inputs.top_k
        if "search_rag_chunks" not in expected_tools:
            scores = score_retrieval_case(case, [], actual_tool_calls=[])
            # retrieval-only runner는 이 case를 실행하지 않았으므로 no-answer 품질 집계에서도 제외한다.
            scores["no_answer_correct"] = None
            results.append(
                CaseEvaluationResult(
                    case_id=case.case_id,
                    status="skipped",
                    question=case.inputs.question,
                    top_k=top_k,
                    expected_document_keys=case.expectations.expected_document_keys,
                    expected_chunk_keys=case.expectations.expected_chunk_keys,
                    expected_tool_calls=expected_tools,
                    actual_tool_calls=[],
                    **scores,
                )
            )
            continue

        try:
            response = searcher.search(case.inputs.question, top_k)
        except RagSearchError as exc:
            results.append(
                CaseEvaluationResult(
                    case_id=case.case_id,
                    status="error",
                    question=case.inputs.question,
                    top_k=top_k,
                    expected_document_keys=case.expectations.expected_document_keys,
                    expected_chunk_keys=case.expectations.expected_chunk_keys,
                    expected_tool_calls=expected_tools,
                    actual_tool_calls=["search_rag_chunks"],
                    tool_exact_match=float(expected_tools == ["search_rag_chunks"]),
                    elapsed_ms=exc.elapsed_ms,
                    error_code=exc.code,
                    error_message=exc.message,
                )
            )
            continue

        retrieved = build_retrieved_chunks(response.hits)
        scores = score_retrieval_case(case, retrieved, actual_tool_calls=["search_rag_chunks"])
        results.append(
            CaseEvaluationResult(
                case_id=case.case_id,
                status="ok",
                question=case.inputs.question,
                top_k=top_k,
                expected_document_keys=case.expectations.expected_document_keys,
                expected_chunk_keys=case.expectations.expected_chunk_keys,
                expected_tool_calls=expected_tools,
                actual_tool_calls=["search_rag_chunks"],
                retrieved=retrieved,
                candidate_count=response.candidate_count,
                rejected_count=response.rejected_count,
                similarity_cutoff=response.similarity_cutoff,
                top_similarity=response.top_similarity,
                no_answer=response.no_answer,
                embedding_ms=response.embedding_ms,
                db_ms=response.db_ms,
                elapsed_ms=response.elapsed_ms,
                over_budget=response.over_budget,
                **scores,
            )
        )

    return EvaluationReport(
        dataset_name=dataset.name,
        dataset_version=dataset.version,
        dataset_fingerprint=dataset.fingerprint,
        corpus=corpus,
        top_k_override=top_k_override,
        cases=results,
        aggregate=aggregate_case_results(results),
    )
