# Pinned to bookworm: the `python:3.11-slim` tag now tracks Debian trixie, and the
# PostgreSQL APT repo/codename must match the base OS (see the RUN step below).
FROM python:3.11-slim-bookworm AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app
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

# Dependencies come from the hash-pinned lockfile, not from the ranges in
# pyproject.toml: `--require-hashes` means the build gets exactly the artefacts
# recorded in requirements.lock or it fails outright, so a compromised release
# published inside one of those ranges cannot walk into the image on a rebuild.
# Copied on its own line so the layer caches until the lockfile itself changes.
#
# The packaging toolchain then goes, in this same RUN. Every fixable CVE the first
# image scan reported lived in it and not one lived in requirements.lock: pip vendors
# its own pinned msgpack, urllib3 and setuptools (pip/_vendor/vendor.txt), and that
# setuptools in turn vendors wheel and jaraco.context. The `--upgrade pip` above
# cannot clear any of them, because the pip doing the upgrading is the pip that pins
# them. Nothing left in the image needs it: the uvicorn console script the CMD runs
# is a plain `from uvicorn.main import main` stub rather than a pkg_resources entry
# point, the HEALTHCHECK below uses stdlib urllib rather than urllib3, and no module
# under app/ or migrations/ imports pkg_resources, setuptools or distutils.
#
# Three things about the spelling, each of which cost something to establish:
#   * same RUN as the install. A later `RUN rm` only hides the files from the
#     flattened filesystem a scanner reads — the layer underneath still carries them,
#     and that layer is still pushed, still pulled, still there for `docker save`.
#   * `pip uninstall`, never `rm -rf`, because setuptools also owns pkg_resources/,
#     _distutils_hack/ and distutils-precedence.pth. Orphan that .pth and every
#     interpreter start, the HEALTHCHECK's included, prints a traceback to stderr and
#     still exits 0: a container that looks broken for years and is not.
#   * pip last in the argv, so it is still there to remove the other two.
# `wheel` carries no finding today — the image's copy is 0.46.3 and clean — and is
# named because it is the same build-time toolchain, not because it is vulnerable. If
# a future base image drops it, pip skips a name that is not installed with a warning
# and still exits 0, so naming it stays correct either way. Removing all three leaves
# the image with no package manager; to chase a dependency problem, run a throwaway
# python:3.11-slim-bookworm container alongside this one rather than putting pip back.
COPY requirements.lock ./
RUN pip install --upgrade pip \
 && pip install --require-hashes --no-deps -r requirements.lock \
 && pip uninstall -y setuptools wheel pip

COPY pyproject.toml ./
COPY app ./app
COPY alembic.ini ./
COPY migrations ./migrations

# Least privilege: run as a non-root user. /backups holds pre-migration dumps and
# is created here so a named volume mounted there inherits appuser ownership.
RUN useradd --create-home --uid 10001 appuser \
 && mkdir -p /backups \
 && chown -R appuser /app /backups
USER appuser

# The only point at which either architecture's image is executed before it reaches
# somebody's box. The `images` job in security-scan.yml builds amd64 and only reads
# its filesystem; the release build publishes arm64 under QEMU and nothing runs that
# at all. So this RUN is the whole of the evidence that the uninstall above did not
# take something the app imports, and the only such evidence that covers arm64. The
# first command is the non-vacuous half: without it, a strip that quietly stopped
# removing anything would still go green here. Run after USER, as the user that will
# actually import these modules at boot.
RUN python -c "import importlib.util as u; assert not [m for m in ('pip','setuptools','pkg_resources','wheel') if u.find_spec(m)], 'the packaging toolchain survived the uninstall above'" \
 && python -c "import app.main, app.dbinit"

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health').status==200 else 1)"

# Apply DB migrations (Alembic), then serve.
CMD ["sh", "-c", "python -m app.dbinit && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
