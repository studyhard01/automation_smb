"""LLMOps GraphSpace의 문서 Version 관계를 Neo4j에서 읽기 전용으로 조회한다."""

from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Any

from neo4j import GraphDatabase, Query, READ_ACCESS
from neo4j.exceptions import Neo4jError

from .config import Settings
from .models import DocumentGraphResponse, GraphEdge, GraphNode, StoreConnectionState


class LlmopsGraphError(RuntimeError):
    """내부 URI·계정·Cypher를 포함하지 않는 Graph 오류."""

    def __init__(self, code: str, message: str, elapsed_ms: float = 0.0) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.elapsed_ms = elapsed_ms


class LlmopsGraphReader:
    """기본/home DB의 `space_id` 범위에서 Version chain만 읽는다."""

    _QUERY_TEXT = """
        MATCH (d:Document {space_id: $space_id, doc_id: $doc_id})
        OPTIONAL MATCH (d)-[:HAS_REVISION]->(r:Revision)
        OPTIONAL MATCH (d)-[:LATEST]->(latest:Revision)
        OPTIONAL MATCH (r)-[:SUPERSEDES]->(previous:Revision)
        RETURN
            d.doc_id AS doc_id,
            coalesce(d.title, d.document_key, '문서') AS document_label,
            coalesce(d.status, '') AS document_status,
            r.revision_id AS revision_id,
            coalesce(r.source_version, toString(r.revision_number), 'Revision') AS revision_label,
            coalesce(r.status, '') AS revision_status,
            latest.revision_id AS latest_revision_id,
            previous.revision_id AS previous_revision_id
        ORDER BY r.revision_number
        """
    _SEARCH_QUERY_TEXT = """
        MATCH (d:Document {space_id: $space_id})
        OPTIONAL MATCH (d)-[:LATEST]->(latest:Revision)
        WITH d, latest,
             toLower(
                 coalesce(d.title, '') + ' ' +
                 coalesce(d.document_key, '') + ' ' +
                 coalesce(d.logical_name, '') + ' ' +
                 coalesce(latest.source_version, '') + ' ' +
                 coalesce(toString(latest.revision_number), '')
             ) AS searchable
        WHERE any(term IN $terms WHERE searchable CONTAINS term)
        RETURN d.doc_id AS doc_id, latest.revision_id AS revision_id
        LIMIT $limit
        """

    def __init__(self, settings: Settings, *, driver: Any | None = None) -> None:
        self._settings = settings
        timeout_s = max(0.1, settings.llmops_neo4j_timeout_ms / 1000)
        self._query = Query(self._QUERY_TEXT, timeout=timeout_s)
        self._search_query = Query(self._SEARCH_QUERY_TEXT, timeout=timeout_s)
        self._status_query = Query("RETURN 1 AS ok", timeout=timeout_s)
        self._driver = driver or GraphDatabase.driver(
            settings.neo4j_uri.strip(),
            auth=(settings.neo4j_user, settings.neo4j_password),
            connection_timeout=timeout_s,
            connection_acquisition_timeout=timeout_s,
            max_connection_pool_size=4,
        )

    def read_document_graph(self, doc_id: str) -> DocumentGraphResponse:
        """Document/HAS_REVISION/LATEST/SUPERSEDES만 안전한 공개 graph로 변환한다."""

        started = time.perf_counter()
        warnings: list[str] = []
        database = self._preferred_database(warnings)
        degraded = bool(warnings)
        try:
            rows = self._read(database, doc_id)
        except Neo4jError as exc:
            if database and self._is_database_not_found(exc):
                warnings.append("neo4j_home_database_fallback")
                degraded = True
                try:
                    rows = self._read(None, doc_id)
                except Exception as fallback_exc:
                    raise self._unavailable(started) from fallback_exc
            else:
                raise self._unavailable(started) from exc
        except Exception as exc:
            raise self._unavailable(started) from exc

        if not rows:
            raise LlmopsGraphError(
                "graph_document_not_found",
                "선택한 문서의 Version Graph를 찾지 못했습니다.",
                round((time.perf_counter() - started) * 1000, 1),
            )
        nodes: dict[str, GraphNode] = {}
        edges: dict[tuple[str, str, str], GraphEdge] = {}
        document_node_id = f"document:{doc_id}"
        first = rows[0]
        nodes[document_node_id] = GraphNode(
            id=document_node_id,
            type="document",
            label=str(first.get("document_label") or "문서"),
            status=str(first.get("document_status") or ""),
        )
        for row in rows:
            revision_id = str(row.get("revision_id") or "").strip()
            if not revision_id:
                continue
            revision_node_id = f"revision:{revision_id}"
            nodes[revision_node_id] = GraphNode(
                id=revision_node_id,
                type="revision",
                label=str(row.get("revision_label") or "Revision"),
                status=str(row.get("revision_status") or ""),
            )
            self._add_edge(edges, document_node_id, revision_node_id, "HAS_REVISION")
            if revision_id == str(row.get("latest_revision_id") or ""):
                self._add_edge(edges, document_node_id, revision_node_id, "LATEST")
            previous_id = str(row.get("previous_revision_id") or "").strip()
            if previous_id:
                previous_node_id = f"revision:{previous_id}"
                nodes.setdefault(
                    previous_node_id,
                    GraphNode(id=previous_node_id, type="revision", label="이전 Revision"),
                )
                self._add_edge(edges, revision_node_id, previous_node_id, "SUPERSEDES")
        return DocumentGraphResponse(
            doc_id=doc_id,
            nodes=list(nodes.values()),
            edges=list(edges.values()),
            degraded=degraded,
            warnings=warnings,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
        )

    def search_document_refs(self, terms: Iterable[str], *, limit: int) -> set[tuple[str, str]]:
        """Document/Revision label에서 검색어와 일치하는 활성 pair를 찾는다."""

        started = time.perf_counter()
        normalized_terms = list(dict.fromkeys(term.casefold().strip() for term in terms if term.strip()))
        if not normalized_terms:
            return set()
        warnings: list[str] = []
        database = self._preferred_database(warnings)
        try:
            rows = self._search(database, normalized_terms, limit)
        except Neo4jError as exc:
            if database and self._is_database_not_found(exc):
                try:
                    rows = self._search(None, normalized_terms, limit)
                except Exception as fallback_exc:
                    raise self._unavailable(started) from fallback_exc
            else:
                raise self._unavailable(started) from exc
        except Exception as exc:
            raise self._unavailable(started) from exc
        return {
            (str(row["doc_id"]), str(row["revision_id"]))
            for row in rows
            if row.get("doc_id") and row.get("revision_id")
        }

    def _read(self, database: str | None, doc_id: str) -> list[dict[str, Any]]:
        with self._driver.session(database=database, default_access_mode=READ_ACCESS) as session:
            # neo4j 6.x의 ManagedTransaction.run()은 timeout을 담은 Query 객체를
            # 받지 않아 TypeError를 낸다. READ_ACCESS autocommit session은 Query의
            # timeout과 읽기 라우팅을 모두 보존하므로 여기서 직접 실행한다.
            records = list(
                session.run(
                    self._query,
                    space_id=self._settings.llmops_graph_space,
                    doc_id=doc_id,
                )
            )
        return [record.data() for record in records]

    def _search(self, database: str | None, terms: list[str], limit: int) -> list[dict[str, Any]]:
        with self._driver.session(database=database, default_access_mode=READ_ACCESS) as session:
            records = list(
                session.run(
                    self._search_query,
                    space_id=self._settings.llmops_graph_space,
                    terms=terms,
                    limit=limit,
                )
            )
        return [record.data() for record in records]

    def _preferred_database(self, warnings: list[str]) -> str | None:
        mode = self._settings.llmops_neo4j_mode.strip().lower()
        configured = self._settings.llmops_neo4j_database.strip()
        if mode == "graph_space":
            if configured:
                warnings.append("neo4j_logical_database_ignored_for_graph_space")
            return None
        return configured or None

    @staticmethod
    def _is_database_not_found(exc: Neo4jError) -> bool:
        return getattr(exc, "code", "") == "Neo.ClientError.Database.DatabaseNotFound"

    @staticmethod
    def _add_edge(
        edges: dict[tuple[str, str, str], GraphEdge],
        source: str,
        target: str,
        edge_type: str,
    ) -> None:
        key = (source, target, edge_type)
        edges[key] = GraphEdge(source=source, target=target, type=edge_type)

    @staticmethod
    def _unavailable(started: float) -> LlmopsGraphError:
        return LlmopsGraphError(
            "graph_unavailable",
            "문서 Version Graph를 조회할 수 없습니다. 문서 검색과 Q&A는 계속 사용할 수 있습니다.",
            round((time.perf_counter() - started) * 1000, 1),
        )

    def status(self) -> StoreConnectionState:
        """Node를 열거하지 않고 home/default read session 연결만 확인한다."""

        started = time.perf_counter()
        if not self._settings.llmops_neo4j_configured:
            return StoreConnectionState(
                configured=False,
                connected=False,
                degraded=True,
                message="LLMOps Neo4j가 구성되지 않았습니다.",
            )
        warnings: list[str] = []
        database = self._preferred_database(warnings)
        degraded = bool(warnings)
        try:
            with self._driver.session(database=database, default_access_mode=READ_ACCESS) as session:
                ok = session.run(self._status_query).single()["ok"] == 1
        except Neo4jError as exc:
            if database and self._is_database_not_found(exc):
                warnings.append("neo4j_home_database_fallback")
                degraded = True
                try:
                    with self._driver.session(database=None, default_access_mode=READ_ACCESS) as session:
                        ok = session.run(self._status_query).single()["ok"] == 1
                except Exception:
                    ok = False
            else:
                ok = False
        except Exception:
            ok = False
        return StoreConnectionState(
            configured=True,
            connected=bool(ok),
            degraded=degraded or not ok,
            message="연결됨" if ok else "Neo4j 연결을 확인할 수 없습니다.",
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
            metadata={
                "mode": self._settings.llmops_neo4j_mode,
                "space_configured": True,
                "warnings": warnings,
            },
        )

    def close(self) -> None:
        """Neo4j driver pool을 닫는다."""

        self._driver.close()
