"""Docker image와 Compose의 배포·비밀 경계 계약을 검증한다."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_dockerfile_builds_vue_then_runs_fastapi_as_non_root() -> None:
    dockerfile = _read("Dockerfile")

    assert "FROM node:22-alpine AS frontend-build" in dockerfile
    assert "RUN npm ci" in dockerfile
    assert "RUN npm run build" in dockerfile
    assert "FROM python:3.11-slim-bookworm AS runtime" in dockerfile
    assert 'ARG UV_INSECURE_HOST=""' in dockerfile
    assert "uv sync --native-tls --frozen --no-dev --no-install-project" in dockerfile
    assert "COPY --from=frontend-build" in dockerfile
    assert "/workspace/backend/src/smb_finder/web" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "EXPOSE 8011" in dockerfile
    assert '["uvicorn", "smb_finder.api:app", "--host", "0.0.0.0", "--port", "8011"]' in dockerfile
    assert "HEALTHCHECK" in dockerfile


def test_docker_build_context_is_allowlisted_and_excludes_runtime_secrets() -> None:
    dockerfile = _read("Dockerfile")
    dockerignore = _read(".dockerignore")
    copy_lines = [line.strip() for line in dockerfile.splitlines() if line.strip().startswith("COPY ")]
    significant_ignore_lines = [
        line.strip()
        for line in dockerignore.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    assert significant_ignore_lines[0] == "*"
    assert "!pyproject.toml" in significant_ignore_lines
    assert "!uv.lock" in significant_ignore_lines
    assert "!frontend/**" in significant_ignore_lines
    assert "!backend/src/**" in significant_ignore_lines
    assert "frontend/.env" in significant_ignore_lines
    assert "frontend/.env.*" in significant_ignore_lines
    assert "backend/src/smb_finder/web/" in significant_ignore_lines
    assert all("COPY . " not in f"{line} " for line in copy_lines)
    assert all(".env" not in line for line in copy_lines)
    assert "PASSWORD" not in dockerfile.upper()
    assert "SECRET_KEY" not in dockerfile.upper()


def test_compose_injects_base_env_and_optional_container_overrides_at_runtime() -> None:
    compose = _read("compose.yaml")

    assert re.search(r"env_file:\s*\n\s*- path: \.env\s*\n\s*required: true", compose)
    assert re.search(r"- path: \.env\.docker\s*\n\s*required: false", compose)
    assert '"${AUTOMATION_SMB_PORT:-8011}:8011"' in compose
    assert 'UV_INSECURE_HOST: "${AUTOMATION_SMB_BUILD_UV_INSECURE_HOST:-}"' in compose
    assert '"host.docker.internal:host-gateway"' in compose
    assert "restart: unless-stopped" in compose
    assert "healthcheck:" in compose
    assert "read_only: true" in compose
    assert "./.runtime:/app/.runtime" in compose
    assert "no-new-privileges:true" in compose
    assert "data.get('ready')" not in compose
    assert "environment:" not in compose

    forbidden_literal_assignments = (
        "POSTGRES_PASSWORD:",
        "LLMOPS_MINIO_SECRET_KEY:",
        "NEO4J_PASSWORD:",
        "OPENAI_API_KEY:",
    )
    assert not any(token in compose for token in forbidden_literal_assignments)


def test_docker_env_template_contains_only_commented_endpoint_examples() -> None:
    template = _read("docker.env.example")
    assignments = [line for line in template.splitlines() if line.strip() and not line.lstrip().startswith("#")]

    assert assignments == []
    assert "PASSWORD" not in template.upper()
    assert "SECRET_KEY" not in template.upper()
    assert "OPENAI_API_KEY" not in template.upper()


def test_docker_env_preparer_does_not_log_or_embed_endpoint_values() -> None:
    script = _read("scripts/docker/prepare_env.ps1")

    assert 'Read-DotEnv $basePath' in script
    assert '$baseValues["DEV_SERVER"]' in script
    assert '$baseValues["POSTGRES_HOST"]' in script
    assert 'LLMOPS_DB_HOST=$dbHost' in script
    assert "value hidden" in script
    assert "Write-Output $dbHost" not in script
    assert 'Join-Path $repoRoot ".runtime"' in script
    assert "New-Item -ItemType Directory" in script
