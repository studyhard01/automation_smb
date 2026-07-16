"""MLflow 미설치 서비스 환경에서도 import 가능한 평가 전용 adapter."""

from __future__ import annotations

import importlib
import math
import os
from collections.abc import Mapping
from typing import Any

import httpx

from .models import EvaluationReport


class MlflowEvaluationError(RuntimeError):
    """평가 CLI에서 사용자에게 표시할 MLflow 연결·기록 오류."""


class NoopEvaluationLogger:
    """정상 서비스 경로에서 사용할 수 있는 명시적 no-op logger."""

    enabled = False

    def log_report(self, report: EvaluationReport, **kwargs: Any) -> str | None:  # noqa: ARG002
        return None


class MlflowEvaluationLogger:
    """선택 설치된 MLflow Tracking Server에 평가 run을 기록한다."""

    enabled = True

    def __init__(self, *, tracking_uri: str, experiment_name: str, timeout_ms: int = 2000) -> None:
        self.tracking_uri = tracking_uri.strip().rstrip("/")
        self.experiment_name = experiment_name.strip()
        self.timeout_seconds = max(0.1, timeout_ms / 1000)
        self._mlflow: Any | None = None

    def _load_mlflow(self) -> Any:
        if self._mlflow is not None:
            return self._mlflow
        os.environ.setdefault("MLFLOW_HTTP_REQUEST_TIMEOUT", str(max(1, math.ceil(self.timeout_seconds))))
        os.environ.setdefault("MLFLOW_HTTP_REQUEST_MAX_RETRIES", "1")
        os.environ.setdefault("MLFLOW_HTTP_REQUEST_BACKOFF_FACTOR", "1")
        try:
            self._mlflow = importlib.import_module("mlflow")
        except ImportError as exc:
            raise MlflowEvaluationError(
                "MLflow 평가 의존성이 없습니다. `uv sync --native-tls --extra evaluation`을 실행하세요."
            ) from exc
        return self._mlflow

    def preflight(self) -> str:
        """client 설치와 Tracking Server 버전 endpoint를 확인한다."""

        mlflow = self._load_mlflow()
        try:
            response = httpx.get(f"{self.tracking_uri}/version", timeout=self.timeout_seconds)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise MlflowEvaluationError(
                f"MLflow Tracking Server에 연결할 수 없습니다: {self.tracking_uri}"
            ) from exc
        server_version = response.text.strip()
        client_version = str(getattr(mlflow, "__version__", "")).strip()
        if client_version and server_version and client_version != server_version:
            raise MlflowEvaluationError(
                f"MLflow client/server 버전이 다릅니다(client={client_version}, server={server_version})."
            )
        return server_version

    def log_report(
        self,
        report: EvaluationReport,
        *,
        run_name: str,
        parameters: Mapping[str, Any],
        tags: Mapping[str, Any] | None = None,
    ) -> str:
        """집계 metric과 case별 JSON artifact를 한 run에 기록한다."""

        mlflow = self._load_mlflow()
        try:
            mlflow.set_tracking_uri(self.tracking_uri)
            mlflow.set_experiment(self.experiment_name)
            run_tags = {"synthetic": "true", "evaluation_mode": report.mode, **dict(tags or {})}
            with mlflow.start_run(run_name=run_name, tags=run_tags) as run:
                safe_parameters = {key: str(value)[:500] for key, value in parameters.items()}
                mlflow.log_params(safe_parameters)
                mlflow.log_metrics(report.aggregate.as_mlflow_metrics())
                mlflow.log_dict(report.model_dump(mode="json"), "evaluation_results.json")
                failed = [case.model_dump(mode="json") for case in report.cases if case.status == "error"]
                mlflow.log_dict({"failed_cases": failed}, "failed_cases.json")
                return str(run.info.run_id)
        except MlflowEvaluationError:
            raise
        except Exception as exc:
            raise MlflowEvaluationError(f"MLflow evaluation run 기록에 실패했습니다: {exc}") from exc
