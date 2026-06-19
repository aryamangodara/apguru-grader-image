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
# Ship the endpoint smoke test (stdlib-only) so the deploy can run it against the
# freshly-built container as a release/deploy gate. See scripts/smoke_test_api.py.
COPY scripts/smoke_test_api.py scripts/
COPY gunicorn.conf.py .

EXPOSE 8080

# Boot the grader. Database migrations are NOT run here — they live in the central
# repo `apguru-centralized-alembic`, whose CI/CD applies `alembic upgrade head` to the
# shared prod DB on merge. The app never migrates on boot (that boot migration used to
# crash-loop the container when the shared DB drifted ahead of this repo's chain).
CMD ["gunicorn", "app.main:app", "-c", "gunicorn.conf.py"]
