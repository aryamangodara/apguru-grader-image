from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from dotenv import load_dotenv

load_dotenv()

from app.core.config import settings
from app.core.database import Database
from app.core.logging import setup_logging
from app.core.observability import configure_langfuse, shutdown_langfuse
from app.api.router import api_router
from app.middleware.request_logging import RequestLoggingMiddleware

log = structlog.stdlib.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- Startup ---
    configure_langfuse()
    _, _, db_host, db_port, db_name = settings._effective_db_params()
    db = Database.get_instance()
    if await db.connect():
        log.info(
            "database_connected", db_name=db_name, db_host=db_host,
            db_port=db_port, local=settings.use_local_db,
        )
    else:
        log.warning(
            "database_unreachable", db_name=db_name, db_host=db_host,
            db_port=db_port, local=settings.use_local_db,
        )
    # Fail any grader jobs orphaned by a previous restart — in-process
    # BackgroundTasks don't survive a process exit.
    try:
        from app.services.grader_job_service import reap_stale_jobs

        reaped = await reap_stale_jobs()
        if reaped:
            log.info("grader_jobs_reaped_on_startup", count=reaped)
    except Exception as exc:
        log.warning("grader_reaper_startup_failed", error=str(exc))
    yield
    # --- Shutdown ---
    await shutdown_langfuse()
    await Database.dispose_all()
    log.info("database_connections_disposed")


def create_app() -> FastAPI:
    setup_logging()

    app = FastAPI(
        title=settings.app_name,
        version="1.2.0",
        description="Auto-grades AP® Free-Response Questions (FRQ) from handwritten or typed student submissions.",
        debug=settings.debug,
        lifespan=lifespan,
    )

    # Configure CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Request logging (added after CORS so it wraps the full request)
    app.add_middleware(RequestLoggingMiddleware)

    # Register root router
    app.include_router(api_router, prefix=settings.api_prefix)

    return app


app = create_app()
