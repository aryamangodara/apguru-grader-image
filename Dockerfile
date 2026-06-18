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

# Boot the grader. `uat_apguru_new` is SHARED with the parent analytics app, whose
# migrations can push the common alembic_version ahead of THIS repo's chain (e.g. DB
# at 030 while this chain tops at 028). In ONLY that case `alembic upgrade head` prints
# "Can't locate revision …" and we start anyway — the grader's own tables already exist
# and the drift is benign for it. Any OTHER alembic failure (bad creds, DB unreachable,
# a broken grader migration, partial DDL) is real and MUST abort startup, so the deploy
# health check fails loudly instead of serving a grader that can't reach its tables
# behind a static /health. Re-tighten to a plain `&&` once the chain is resynced with
# prod — see docs/grader-ec2-deployment.md.
CMD ["sh", "-c", "out=$(alembic upgrade head 2>&1); rc=$?; printf '%s\\n' \"$out\"; if [ \"$rc\" -ne 0 ]; then printf '%s' \"$out\" | grep -qi 'locate revision' && echo 'WARN: shared DB ahead of this chain; starting app anyway' || { echo 'FATAL: alembic upgrade failed for an unrelated reason; aborting'; exit \"$rc\"; }; fi; exec gunicorn app.main:app -c gunicorn.conf.py"]
