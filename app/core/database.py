import time
from typing import Any

import structlog
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import declarative_base

from app.core.config import settings

log = structlog.get_logger(__name__)

Base = declarative_base()


def _attach_query_timing(engine: AsyncEngine) -> None:
    """Register event listeners on the sync engine to log query duration."""
    sync_engine = engine.sync_engine

    @event.listens_for(sync_engine, "before_cursor_execute")
    def _before_cursor_execute(
        conn, cursor, statement, parameters, context, executemany
    ):
        conn.info["_query_start"] = time.perf_counter()

    @event.listens_for(sync_engine, "after_cursor_execute")
    def _after_cursor_execute(
        conn, cursor, statement, parameters, context, executemany
    ):
        duration_ms = round((time.perf_counter() - conn.info["_query_start"]) * 1000, 2)
        log.debug("query_executed", sql=statement, duration_ms=duration_ms)


class Database:
    """Async singleton for managing MySQL database connections.

    Usage:
        db = Database.get_instance()           # default db from settings
        db = Database.get_instance("other_db") # connect to a different database

        rows = await db.query("SELECT * FROM users WHERE active = :active", {"active": 1})
        await db.write("INSERT INTO logs (msg) VALUES (:msg)", {"msg": "hello"})
        await db.write_many("INSERT INTO logs (msg) VALUES (:msg)", [{"msg": "a"}, {"msg": "b"}])

        async with db.transaction() as session:
            await session.execute(text("UPDATE users SET active = 0 WHERE id = :id"), {"id": 5})
            await session.execute(text("DELETE FROM tokens WHERE user_id = :id"), {"id": 5})
    """

    _instances: dict[str, "Database"] = {}

    def __init__(self, db_name: str | None = None, url: str | None = None) -> None:
        if url is not None:
            # Explicit URL (e.g. a named env profile) — bypass the
            # local/default resolution entirely.
            self._db_name = db_name or "<explicit-url>"
        else:
            _, _, _, _, effective_db = settings._effective_db_params(db_name)
            self._db_name = effective_db
        self._engine: AsyncEngine = create_async_engine(
            url or settings.get_async_database_url(db_name),
            pool_pre_ping=True,
            pool_recycle=3600,
            pool_size=10,
            max_overflow=5,
            pool_timeout=30,
        )
        self._session_factory = async_sessionmaker(
            self._engine, class_=AsyncSession, expire_on_commit=False
        )
        if settings.debug:
            _attach_query_timing(self._engine)

    # ------------------------------------------------------------------
    # Singleton access
    # ------------------------------------------------------------------
    @classmethod
    def get_instance(
        cls,
        db_name: str | None = None,
        url: str | None = None,
        instance_key: str | None = None,
    ) -> "Database":
        """Returns the singleton Database instance for a given database name.

        Synchronous — safe to call outside an event loop.  The async engine
        is created lazily and only used inside async methods.

        Pass ``url`` + ``instance_key`` to point an instance at an arbitrary
        host (used by cross-env ops tooling); ``instance_key`` keeps such
        instances from colliding with the default-host singletons.
        """
        if url is not None:
            key = instance_key or url
        else:
            _, _, _, _, key = settings._effective_db_params(db_name)
        if key not in cls._instances:
            cls._instances[key] = cls(db_name, url)
        return cls._instances[key]

    # ------------------------------------------------------------------
    # Connection helpers
    # ------------------------------------------------------------------
    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    def get_session(self) -> AsyncSession:
        return self._session_factory()

    async def connect(self) -> bool:
        """Lightweight connectivity check (no writes)."""
        try:
            async with self._engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------
    async def query(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Execute a SELECT and return a list of row-dicts."""
        async with self._engine.connect() as conn:
            result = await conn.execute(text(sql), params or {})
            columns = list(result.keys())
            rows = [dict(zip(columns, row)) for row in result.fetchall()]
            await conn.commit()
            return rows

    async def query_one(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Execute a SELECT and return the first row or None."""
        rows = await self.query(sql, params)
        return rows[0] if rows else None

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------
    async def write(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
    ) -> int:
        """Execute a single INSERT / UPDATE / DELETE. Returns affected row count."""
        async with self._engine.begin() as conn:
            result = await conn.execute(text(sql), params or {})
            return result.rowcount

    async def write_returning_id(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
    ) -> int:
        """Execute an INSERT and return the auto-generated primary key.

        Note: relies on cursor lastrowid — MySQL-specific.
        For PostgreSQL, use RETURNING in the SQL and scalar_one() instead.
        """
        async with self._engine.begin() as conn:
            result = await conn.execute(text(sql), params or {})
            return result.lastrowid

    async def write_many(
        self,
        sql: str,
        param_list: list[dict[str, Any]],
    ) -> int:
        """Execute a parameterised statement for each dict in param_list (batch)."""
        async with self._engine.begin() as conn:
            result = await conn.execute(text(sql), param_list)
            return result.rowcount

    # ------------------------------------------------------------------
    # Transaction context manager
    # ------------------------------------------------------------------
    class _TransactionCtx:
        """Async context manager that commits on success and rolls back on error."""

        def __init__(self, session_factory: async_sessionmaker) -> None:
            self._session_factory = session_factory
            self._session: AsyncSession | None = None

        async def __aenter__(self) -> AsyncSession:
            self._session = self._session_factory()
            return self._session

        async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
            if self._session is None:
                return
            if exc_type is not None:
                await self._session.rollback()
            else:
                await self._session.commit()
            await self._session.close()

    def transaction(self) -> _TransactionCtx:
        """Provides a transactional scope around a series of operations.

        Usage:
            async with db.transaction() as session:
                await session.execute(text("..."), {...})
                await session.execute(text("..."), {...})
        """
        return self._TransactionCtx(self._session_factory)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------
    async def dispose(self) -> None:
        """Dispose of the connection pool (call on app shutdown)."""
        await self._engine.dispose()

    @classmethod
    async def dispose_all(cls) -> None:
        """Dispose every registered instance's pool."""
        for instance in cls._instances.values():
            await instance.dispose()
        cls._instances.clear()
