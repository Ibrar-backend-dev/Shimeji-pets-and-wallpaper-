# ---------- builder ----------
# Wheels are built here so the runtime image carries no compilers.
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY requirements/ requirements/
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r requirements/prod.txt

# ---------- runtime ----------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    DJANGO_SETTINGS_MODULE=config.settings.prod

# Pillow and psycopg ship manylinux wheels that bundle their own libs, so no
# apt build-deps are needed. curl is here only for the container healthcheck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

# Never run as root.
RUN useradd --create-home --uid 10001 appuser
WORKDIR /app
COPY --chown=appuser:appuser . .

# Collect static at build time so the container starts without writing to disk.
# A throwaway key is used because collectstatic imports settings, and prod.py
# refuses to load without one — the real key arrives via the environment.
RUN DJANGO_SECRET_KEY=build-time-only-not-a-secret \
    DJANGO_ALLOWED_HOSTS=localhost \
    DATABASE_URL=sqlite:///tmp/build.sqlite3 \
    CRON_SECRET=build-time-only-not-a-secret-value \
    python manage.py collectstatic --noinput --clear \
    && rm -f /tmp/build.sqlite3

USER appuser
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/healthz || exit 1

# 2 workers x 4 threads fits a 512 MB instance. max-requests recycles workers to
# cap gradual memory growth; the jitter stops them all recycling in lockstep.
CMD ["sh", "-c", "python manage.py migrate --noinput && exec gunicorn config.wsgi:application \
    --bind 0.0.0.0:${PORT:-8000} \
    --workers ${WEB_CONCURRENCY:-2} \
    --threads ${WEB_THREADS:-4} \
    --worker-class gthread \
    --timeout 30 \
    --graceful-timeout 20 \
    --max-requests 1000 \
    --max-requests-jitter 100 \
    --access-logfile - \
    --error-logfile - \
    --log-level info"]
