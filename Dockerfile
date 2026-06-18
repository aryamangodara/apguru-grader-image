FROM python:3.13-slim

WORKDIR /app

# Install system dependencies for mysqlclient/cryptography
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/
COPY alembic/ alembic/
COPY alembic.ini .
COPY gunicorn.conf.py .

EXPOSE 8080

# Boot migration is best-effort, not boot-gating. `uat_apguru_new` is SHARED with
# the parent analytics app, whose migrations can advance the common alembic_version
# ahead of this repo's chain (e.g. DB at 030 while this chain tops at 028). In that
# case `alembic upgrade head` exits non-zero ("Can't locate revision …") and would
# otherwise crash-loop the container even though the grader's own tables are fine.
# `|| echo …` keeps the upgrade attempt (so genuine grader migrations still apply on
# a healthy chain) but never blocks startup. Re-tighten to `&&` once the chain is
# resynced with prod — see docs/grader-ec2-deployment.md.
CMD ["sh", "-c", "alembic upgrade head || echo 'WARN: alembic upgrade head failed (shared DB ahead of this chain?); starting app anyway'; gunicorn app.main:app -c gunicorn.conf.py"]
