# syntax=docker/dockerfile:1.7
ARG PYTHON_IMAGE=python:3.12-slim-bookworm

FROM ${PYTHON_IMAGE} AS builder
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
# Extras baked into the image; add "--extra mtproto" to enable MTProto backfill.
ARG UV_EXTRAS="--extra postgres"
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev ${UV_EXTRAS} --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev ${UV_EXTRAS} --no-editable

FROM ${PYTHON_IMAGE} AS runtime
RUN groupadd --system --gid 10001 elysium \
    && useradd --system --uid 10001 --gid elysium --home-dir /data --shell /usr/sbin/nologin elysium \
    && mkdir -p /data \
    && chown elysium:elysium /data
COPY --from=builder /app/.venv /app/.venv
COPY --chmod=0755 docker/entrypoint.sh /usr/local/bin/entrypoint.sh
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ELYSIUM_DATA_DIR=/data \
    ELYSIUM_DATABASE_URL=sqlite+aiosqlite:////data/elysium.db
USER elysium
WORKDIR /data
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD ["elysium-bot", "healthcheck"]
ENTRYPOINT ["entrypoint.sh"]
CMD ["run"]
