# syntax=docker/dockerfile:1.7

FROM node:22-alpine AS frontend-build

WORKDIR /workspace/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build


FROM ghcr.io/astral-sh/uv:0.10.12 AS uv-bin


FROM python:3.11-slim-bookworm AS python-deps

ARG UV_INSECURE_HOST=""

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app
COPY --from=uv-bin /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN if [ -n "${UV_INSECURE_HOST}" ]; then \
        uv sync --native-tls --allow-insecure-host "${UV_INSECURE_HOST}" --frozen --no-dev --no-install-project; \
    else \
        uv sync --native-tls --frozen --no-dev --no-install-project; \
    fi


FROM python:3.11-slim-bookworm AS runtime

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONPATH=/app/backend/src \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/app \
    TMPDIR=/tmp

RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app --no-create-home app

WORKDIR /app
COPY --from=python-deps --chown=10001:10001 /app/.venv /app/.venv
COPY --chown=10001:10001 backend/src /app/backend/src
COPY --from=frontend-build --chown=10001:10001 /workspace/backend/src/smb_finder/web /app/backend/src/smb_finder/web

USER 10001:10001

EXPOSE 8011

HEALTHCHECK --interval=30s --timeout=3s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8011/health', timeout=2).read(1)"]

CMD ["uvicorn", "smb_finder.api:app", "--host", "0.0.0.0", "--port", "8011"]
