FROM python:3.12-alpine

ARG VERSION=dev
ARG BUILD_DATE
LABEL org.opencontainers.image.title="MMW" \
      org.opencontainers.image.description="Money Mgr web: a self-hosted viewer/editor for Money Manager .mmbak backups" \
      org.opencontainers.image.version="$VERSION" \
      org.opencontainers.image.created="$BUILD_DATE" \
      build_version="MMW version: $VERSION Build-date: $BUILD_DATE"

# PUID/PGID: who the app runs as and owns /config. UMASK: for the files it
# writes. TZ: local time for backup names and transaction times.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MMW_VERSION=$VERSION \
    MMW_DATA_DIR=/config \
    PUID=1000 \
    PGID=1000 \
    UMASK=022 \
    TZ=Etc/UTC \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=0 \
    PATH="/opt/venv/bin:$PATH"

RUN apk add --no-cache su-exec tzdata

WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --no-cache

COPY docker/entrypoint.sh /usr/local/bin/mmw-entrypoint
COPY src ./

# Everything stateful (accounts, settings, databases, backups) lives in /config.
RUN mkdir /config
VOLUME /config
EXPOSE 5732

HEALTHCHECK --interval=1m --timeout=5s --start-period=20s \
    CMD wget -qO /dev/null http://127.0.0.1:5732/healthz || exit 1

# Starts as root to fix /config's owner, then drops to PUID:PGID.
ENTRYPOINT ["mmw-entrypoint"]
# One worker: the first-write backup guard and sqlite writes are per-process.
CMD ["gunicorn", "--bind", "0.0.0.0:5732", "--workers", "1", "--threads", "4", "--timeout", "120", "app:app"]
