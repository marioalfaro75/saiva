# Pinned to bookworm: the `python:3.11-slim` tag now tracks Debian trixie, and the
# PostgreSQL APT repo/codename must match the base OS (see the RUN step below).
FROM python:3.11-slim-bookworm AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app

ARG SAIVA_VERSION=dev
ENV SAIVA_VERSION=$SAIVA_VERSION

# postgresql-client-16 provides pg_dump for the pre-migration safety backup; it
# must match the postgres:16 server, so we pull it from the PGDG apt repo. The
# repo codename is derived from the base OS (VERSION_CODENAME) so it always
# matches — a mismatched codename pulls an incompatible libpq5 and fails.
RUN . /etc/os-release \
 && apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && install -d /usr/share/postgresql-common/pgdg \
 && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
      -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
 && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] http://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client-16 \
 && apt-get purge -y curl && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY app ./app
COPY alembic.ini ./
COPY migrations ./migrations
RUN pip install --upgrade pip && pip install .

# Least privilege: run as a non-root user. /backups holds pre-migration dumps and
# is created here so a named volume mounted there inherits appuser ownership.
RUN useradd --create-home --uid 10001 appuser \
 && mkdir -p /backups \
 && chown -R appuser /app /backups
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health').status==200 else 1)"

# Apply DB migrations (Alembic), then serve.
CMD ["sh", "-c", "python -m app.dbinit && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
