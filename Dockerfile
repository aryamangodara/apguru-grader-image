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

CMD ["sh", "-c", "alembic upgrade head && gunicorn app.main:app -c gunicorn.conf.py"]
