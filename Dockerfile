# syntax=docker/dockerfile:1
# Remote (Streamable HTTP) image for monarch-mcp. Multi-arch: builds natively on arm64
# (Raspberry Pi) and amd64. Contains no credentials or sessions -- the session file is
# mounted at runtime into /state. See deploy/README.md.

ARG PYTHON_IMAGE=python:3.13-slim-bookworm

FROM ghcr.io/astral-sh/uv:0.12.5 AS uv

FROM ${PYTHON_IMAGE} AS builder
# git is needed only to fetch the pinned monarchmoneycommunity commit from uv.lock.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

FROM ${PYTHON_IMAGE} AS runtime
RUN groupadd --system --gid 10001 monarch \
    && useradd --system --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent \
       --shell /usr/sbin/nologin monarch \
    && install -d -o 10001 -g 10001 -m 0700 /state
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY server.py browser_auth.py http_app.py ./

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MONARCH_TRANSPORT=http \
    MONARCH_HTTP_HOST=0.0.0.0 \
    MONARCH_HTTP_PORT=8000 \
    MONARCH_SESSION_DIR=/state

USER 10001:10001
EXPOSE 8000
VOLUME ["/state"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status == 200 else 1)"]

CMD ["python", "server.py", "--transport", "http"]
