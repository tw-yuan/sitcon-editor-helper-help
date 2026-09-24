FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=0
COPY --from=ghcr.io/astral-sh/uv:0.8.22 /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev
COPY config ./config
COPY migrations ./migrations
RUN groupadd --gid 10001 appuser && useradd --uid 10001 --gid 10001 --create-home appuser && mkdir -p /data && chown appuser:appuser /data
USER 10001:10001
ENTRYPOINT ["/app/.venv/bin/python", "-m", "editorial_bot"]

FROM base AS test
USER root
RUN uv sync --frozen
COPY tests ./tests
ENTRYPOINT ["/app/.venv/bin/python", "-m", "pytest"]
