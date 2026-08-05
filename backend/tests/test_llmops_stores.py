"""MinIO artifact와 Neo4j GraphSpace read-only adapter 계약을 검증한다."""

from __future__ import annotations

import io
from uuid import uuid4

import pytest
from neo4j import READ_ACCESS

from smb_finder.config import Settings
from smb_finder.llmops_artifacts import LlmopsArtifactError, LlmopsArtifactReader
from smb_finder.llmops_graph import LlmopsGraphReader


class FakePgCursor:
    def __init__(self, row: dict | None) -> None:
        self.row = row
        self.executions: list[tuple[object, object]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):  # noqa: ANN002
        return None

    def execute(self, query, parameters):  # noqa: ANN001
        self.executions.append((query, parameters))

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.row if isinstance(self.row, list) else []


class FakePgConnection:
    def __init__(self, row: dict | None) -> None:
        self.cursor_instance = FakePgCursor(row)

    def __enter__(self):
        return self

    def __exit__(self, *_args):  # noqa: ANN002
        return None

    def cursor(self, **_kwargs):  # noqa: ANN003
        return self.cursor_instance


class FakeS3Client:
    def __init__(self, *, body: bytes, content_length: int | None = None) -> None:
        self.body = body
        self.content_length = len(body) if content_length is None else content_length
        self.calls: list[tuple[str, dict]] = []

    def head_object(self, **kwargs):  # noqa: ANN003
        self.calls.append(("head_object", kwargs))
        return {"ContentLength": self.content_length}

    def get_object(self, **kwargs):  # noqa: ANN003
        self.calls.append(("get_object", kwargs))
        return {"Body": io.BytesIO(self.body)}

    def head_bucket(self, **kwargs):  # noqa: ANN003
        self.calls.append(("head_bucket", kwargs))
        return {}

    def close(self) -> None:
        self.calls.append(("close", {}))


def _artifact_settings(**overrides) -> Settings:  # noqa: ANN003
    payload = {
        "_env_file": None,
        "llmops_db_host": "db.test",
        "llmops_postgres_db": "llmops_test",
        "postgres_user": "reader",
        "postgres_password": "secret",
        "minio_endpoint": "http://minio.test",
        "llmops_minio_bucket": "synthetic-artifacts",
        "llmops_minio_access_key": "reader",
        "llmops_minio_secret_key": "secret",
    }
    payload.update(overrides)
    return Settings(**payload)


def test_artifact_reader_validates_active_revision_and_only_stats_then_gets():
    doc_id = uuid4()
    revision_id = uuid4()
    connection = FakePgConnection(
        {
            "object_uri": "s3://synthetic-artifacts/previews/synthetic.md",
            "content_type": "text/markdown",
            "size_bytes": 21,
        }
    )
    s3 = FakeS3Client(body=b"Synthetic preview only")
    captured: dict[str, object] = {}

    def connect(**kwargs):  # noqa: ANN003
        captured.update(kwargs)
        return connection

    response = LlmopsArtifactReader(_artifact_settings(), s3_client=s3, connect=connect).read(
        str(doc_id), str(revision_id), "preview"
    )

    assert response.doc_id == doc_id
    assert response.revision_id == revision_id
    assert response.content == "Synthetic preview only"
    assert [name for name, _ in s3.calls] == ["head_object", "get_object"]
    assert "default_transaction_read_only=on" in str(captured["options"])
    sql_text = connection.cursor_instance.executions[0][0].as_string()
    assert "active_revision_id" in sql_text
    assert "upper(r.status::text) = 'ACTIVE'" in sql_text
    assert "INSERT " not in sql_text.upper()
    assert "UPDATE " not in sql_text.upper()
    assert "DELETE " not in sql_text.upper()
    assert "object_uri" not in response.model_dump_json()
    assert "previews/synthetic.md" not in response.model_dump_json()


def test_artifact_reader_rejects_oversized_object_before_get():
    connection = FakePgConnection(
        {
            "object_uri": "s3://synthetic-artifacts/previews/synthetic.md",
            "content_type": "text/markdown",
            "size_bytes": 2048,
        }
    )
    s3 = FakeS3Client(body=b"unused", content_length=2048)
    reader = LlmopsArtifactReader(
        _artifact_settings(llmops_artifact_max_bytes=1024),
        s3_client=s3,
        connect=lambda **_kwargs: connection,
    )

    with pytest.raises(LlmopsArtifactError) as exc:
        reader.read(str(uuid4()), str(uuid4()), "preview")

    assert exc.value.code == "artifact_too_large"
    assert [name for name, _ in s3.calls] == ["head_object"]


def test_artifact_reader_searches_registry_and_confirms_minio_object():
    doc_id = uuid4()
    revision_id = uuid4()
    connection = FakePgConnection(
        [
            {
                "doc_id": str(doc_id),
                "revision_id": str(revision_id),
                "object_uri": "s3://synthetic-artifacts/previews/synthetic-wbs.md",
            }
        ]
    )
    s3 = FakeS3Client(body=b"unused")
    reader = LlmopsArtifactReader(
        _artifact_settings(),
        s3_client=s3,
        connect=lambda **_kwargs: connection,
    )

    refs = reader.search_document_refs(["synthetic", "wbs"], limit=10)

    assert refs == {(str(doc_id), str(revision_id))}
    assert [name for name, _ in s3.calls] == ["head_bucket", "head_object"]
    sql_text = connection.cursor_instance.executions[0][0].as_string().upper()
    assert "ILIKE ANY" in sql_text
    assert "INSERT " not in sql_text
    assert "UPDATE " not in sql_text
    assert "DELETE " not in sql_text


class FakeRecord:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def data(self) -> dict:
        return self.payload


class FakeGraphTx:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.calls: list[tuple[object, dict]] = []

    def run(self, query, **parameters):  # noqa: ANN001
        self.calls.append((query, parameters))
        return [FakeRecord(row) for row in self.rows]


class FakeGraphSession:
    def __init__(self, tx: FakeGraphTx) -> None:
        self.tx = tx

    def __enter__(self):
        return self

    def __exit__(self, *_args):  # noqa: ANN002
        return None

    def run(self, query, **parameters):  # noqa: ANN001
        return self.tx.run(query, **parameters)


class FakeGraphDriver:
    def __init__(self, rows: list[dict]) -> None:
        self.tx = FakeGraphTx(rows)
        self.sessions: list[dict] = []
        self.closed = False

    def session(self, **kwargs):  # noqa: ANN003
        self.sessions.append(kwargs)
        return FakeGraphSession(self.tx)

    def close(self) -> None:
        self.closed = True


def test_graph_reader_uses_home_database_read_mode_and_space_scope():
    doc_id = uuid4()
    revision_id = uuid4()
    driver = FakeGraphDriver(
        [
            {
                "doc_id": str(doc_id),
                "document_label": "Synthetic handbook",
                "document_status": "ACTIVE",
                "revision_id": str(revision_id),
                "revision_label": "v1",
                "revision_status": "ACTIVE",
                "latest_revision_id": str(revision_id),
                "previous_revision_id": None,
            }
        ]
    )
    settings = Settings(
        _env_file=None,
        neo4j_uri="bolt://neo4j.test",
        neo4j_user="reader",
        neo4j_password="secret",
        llmops_neo4j_database="logical-db-that-is-not-physical",
        llmops_neo4j_mode="graph_space",
        llmops_graph_space="synthetic-space",
    )

    response = LlmopsGraphReader(settings, driver=driver).read_document_graph(str(doc_id))

    assert driver.sessions == [{"database": None, "default_access_mode": READ_ACCESS}]
    assert driver.tx.calls[0][1] == {"space_id": "synthetic-space", "doc_id": str(doc_id)}
    cypher = str(driver.tx.calls[0][0])
    assert "space_id: $space_id" in cypher
    assert "CREATE " not in cypher.upper()
    assert "MERGE " not in cypher.upper()
    assert "SET " not in cypher.upper()
    assert "DELETE " not in cypher.upper()
    assert response.degraded is True
    assert response.warnings == ["neo4j_logical_database_ignored_for_graph_space"]
    assert {edge.type for edge in response.edges} == {"HAS_REVISION", "LATEST"}
    assert "bolt://" not in response.model_dump_json()


def test_graph_reader_searches_document_and_latest_revision_labels():
    doc_id = uuid4()
    revision_id = uuid4()
    driver = FakeGraphDriver([{"doc_id": str(doc_id), "revision_id": str(revision_id)}])
    settings = Settings(
        _env_file=None,
        neo4j_uri="bolt://neo4j.test",
        neo4j_user="reader",
        neo4j_password="secret",
        llmops_graph_space="synthetic-space",
    )

    refs = LlmopsGraphReader(settings, driver=driver).search_document_refs(["WBS", "계획"], limit=10)

    assert refs == {(str(doc_id), str(revision_id))}
    assert driver.sessions == [{"database": None, "default_access_mode": READ_ACCESS}]
    parameters = driver.tx.calls[0][1]
    assert parameters == {"space_id": "synthetic-space", "terms": ["wbs", "계획"], "limit": 10}
    cypher = str(driver.tx.calls[0][0]).upper()
    assert "CREATE " not in cypher
    assert "MERGE " not in cypher
    assert "DELETE " not in cypher
