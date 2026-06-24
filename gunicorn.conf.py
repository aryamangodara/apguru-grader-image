import os

bind = "0.0.0.0:8080"
workers = int(os.getenv("GUNICORN_WORKERS", "2"))
worker_class = "uvicorn.workers.UvicornWorker"
# Recycle each worker after ~200 requests (+ jitter so the two workers don't recycle
# together) to bound long-term memory growth / fragmentation from the render+OCR
# pipeline. Secondary to the container mem_limit (the hard cap) — this keeps a
# long-lived worker's RSS from creeping toward the 1 GiB cgroup ceiling.
max_requests = int(os.getenv("GUNICORN_MAX_REQUESTS", "200"))
max_requests_jitter = int(os.getenv("GUNICORN_MAX_REQUESTS_JITTER", "50"))
# A handwritten grade runs as a Starlette BackgroundTask (~150-160s OCR) that keeps its
# request in flight for the whole duration; allow a recycling/draining worker to finish
# one before SIGKILL so a max_requests recycle or redeploy never aborts an in-flight
# grade (which would orphan the job in `running`). Independent of the OOM/restart path.
graceful_timeout = 180
timeout = 120
keepalive = 5
accesslog = "-"
errorlog = "-"
loglevel = os.getenv("LOG_LEVEL", "info").lower()
