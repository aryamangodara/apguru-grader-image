"""Application settings (pydantic-settings).

Grader-only service: required fields use ``Field(...)`` so the app fails fast at
startup if a value is missing. Per-subject grading/OCR guidance lives in the
``course_configs`` table (resolved at grade time), NOT here — these are
infrastructure + operational knobs only.
"""
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    env: str = Field(default="STAG")
    app_name: str = "APGuru Grader API"
    debug: bool = True
    api_prefix: str = "/api/v1"
    # ASGI mount prefix when this service is served behind a PATH-based reverse
    # proxy that strips the prefix (e.g. "/grader" when the host Apache proxies
    # https://…/grader/ → this container). Sets FastAPI's root_path so Swagger UI
    # (/docs) and the OpenAPI schema reference the spec under the prefix
    # (…/grader/openapi.json) instead of an absolute /openapi.json that 404s
    # through the proxy. Empty = served at the domain root (local/dev/direct).
    root_path: str = Field(
        default="",
        description="ASGI mount prefix when behind a path-based reverse proxy (e.g. '/grader'); empty at root.",
    )
    if debug:
        log_level: str = Field(default="DEBUG")
    else:
        log_level: str = Field(default="INFO")

    # Database Configuration
    db_host: str = Field(...)
    db_port: int = Field(default=3306)
    db_user: str = Field(...)
    db_password: str = Field(...)
    db_name: str = Field(default="apguru")

    # Local Database Override
    # Set use_local_db=true in .env to route all DB traffic to local MySQL.
    use_local_db: bool = Field(default=True)
    local_db_host: str = Field(default="127.0.0.1")
    local_db_port: int = Field(default=3306)
    local_db_user: str = Field(default="root")
    local_db_password: str = Field(default="")
    local_db_name: str = Field(default="apguru")

    # Gemini / Vertex AI (grader LLM)
    # The grader's Gemini client reads GEMINI_API_KEY from the environment, or
    # uses Vertex AI when a service account is configured (see grader_use_vertex).
    gemini_api_key: str | None = Field(default=None)
    google_cloud_project: str = Field(default="")
    google_cloud_location: str = Field(default="")

    # Langfuse Observability — MANDATORY.
    # Every grader LLM call must be traced (product decision: no Langfuse, no LLM
    # call). Both keys are required so the app fails fast at startup if tracing
    # isn't configured, rather than silently grading untraced. A failed auth check
    # at startup only warns (won't block boot) — see observability.configure_langfuse.
    langfuse_public_key: str = Field(...)
    langfuse_secret_key: str = Field(...)
    langfuse_host: str = Field(default="https://cloud.langfuse.com")

    # AP FRQ Auto-Grader (grader feature)
    # Per-subject grading/OCR guidance lives in `course_configs`
    # (grading_addendum / ocr_addendum), resolved dynamically at grade time —
    # NOT here. These are operational knobs only.
    grader_ocr_model: str = Field(default="gemini-3.1-pro-preview")
    grader_rubric_model: str = Field(default="gemini-3.5-flash")
    grader_grading_model: str = Field(default="gemini-3.5-flash")
    grader_typed_label_model: str = Field(default="gemini-3.5-flash")
    grader_ocr_dpi: int = Field(default=300)
    grader_ocr_thinking_level: str = Field(default="low")
    grader_grading_max_workers: int = Field(default=8)
    grader_low_confidence_threshold: float = Field(default=0.75)
    # Answer/question PDFs are fetched from durable URLs in the request.
    grader_pdf_fetch_timeout_seconds: float = Field(default=60.0)
    grader_pdf_fetch_auth_header: str | None = Field(default=None)
    # Caps simultaneous in-flight grading jobs so a burst doesn't exhaust the
    # Gemini quota / CPU; excess jobs wait in the `queued` state.
    grader_max_concurrent_jobs: int = Field(default=2)
    # A startup reaper fails jobs stuck `running` longer than this (a process
    # restart drops in-flight BackgroundTasks).
    grader_job_reaper_stale_minutes: int = Field(default=30)
    # Route the grader's Gemini calls through Vertex AI (global endpoint) even
    # when GEMINI_API_KEY is set. The handwriting-OCR call routinely runs ~150s,
    # which exceeds AI Studio's server-side request deadline (504
    # DEADLINE_EXCEEDED) but is fine on Vertex. Honoured only when a Vertex
    # service account is actually configured (GOOGLE_APPLICATION_CREDENTIALS +
    # GOOGLE_CLOUD_PROJECT); otherwise the grader falls back to the API key.
    grader_use_vertex: bool = Field(default=True)
    # Post-grading audience summaries (issue #14): one extra structured Gemini call
    # turns the scorecard into short student/teacher/parent summaries. On by default;
    # set false to skip the call (and its per-grade cost/latency).
    grader_enable_summaries: bool = Field(default=True)
    grader_summaries_model: str = Field(default="gemini-3.5-flash")

    def _effective_db_params(
        self, specific_db: str | None = None
    ) -> tuple[str, str, str, int, str]:
        """Returns (user, password, host, port, db_name) based on use_local_db flag."""
        if self.use_local_db:
            return (
                self.local_db_user,
                self.local_db_password,
                self.local_db_host,
                self.local_db_port,
                specific_db if specific_db is not None else self.local_db_name,
            )
        return (
            self.db_user,
            self.db_password,
            self.db_host,
            self.db_port,
            specific_db if specific_db is not None else self.db_name,
        )

    def get_async_database_url(self, specific_db: str | None = None) -> str:
        """Constructs the async database URL (used by the application)."""
        user, password, host, port, db = self._effective_db_params(specific_db)
        return f"mysql+aiomysql://{user}:{password}@{host}:{port}/{db}"

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )


settings = Settings()
