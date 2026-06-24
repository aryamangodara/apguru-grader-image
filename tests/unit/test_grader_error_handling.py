"""Tests for the central error envelope + machine-readable error codes.

Builds a minimal app with ``register_exception_handlers`` and routes that raise each
error kind, then asserts the rendered ``{error_code, detail}`` envelope, the refined
409 status, the 422 validation shape, and that an unexpected exception becomes a
500 ``INTERNAL_ERROR`` with no internals leaked.
"""
from __future__ import annotations

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.errors import (
    InvalidTestError,
    RubricNotGeneratedError,
    TestNotRegisteredError,
    register_exception_handlers,
)


def _app() -> FastAPI:
    app = FastAPI(debug=False)  # prod config: the catch-all 500 handler only runs when debug is off
    register_exception_handlers(app)

    @app.get("/not-registered")
    async def _nr() -> dict:
        raise TestNotRegisteredError("test_id 322 is not registered")

    @app.get("/rubric")
    async def _rb() -> dict:
        raise RubricNotGeneratedError("rubric not generated yet")

    @app.get("/boom")
    async def _boom() -> dict:
        raise RuntimeError("super secret internal detail")

    @app.get("/validate")
    async def _v(n: int) -> dict:  # ?n=abc -> 422
        return {"n": n}

    return app


def _client(app: FastAPI) -> AsyncClient:
    # raise_app_exceptions=False so the 500 handler's response is returned, not re-raised.
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    )


async def test_typed_error_renders_envelope():
    async with _client(_app()) as c:
        resp = await c.get("/not-registered")
    assert resp.status_code == 404
    assert resp.json() == {
        "error_code": "TEST_NOT_REGISTERED",
        "detail": "test_id 322 is not registered",
    }


async def test_rubric_not_generated_is_409():
    async with _client(_app()) as c:
        resp = await c.get("/rubric")
    assert resp.status_code == 409  # refined from 404 — exists but isn't ready
    assert resp.json()["error_code"] == "RUBRIC_NOT_GENERATED"


async def test_request_validation_is_422_with_code_and_list_detail():
    async with _client(_app()) as c:
        resp = await c.get("/validate", params={"n": "abc"})
    assert resp.status_code == 422
    body = resp.json()
    assert body["error_code"] == "VALIDATION_ERROR"
    assert isinstance(body["detail"], list)  # FastAPI's field-error list is preserved


async def test_unexpected_exception_is_500_and_hides_internals():
    async with _client(_app()) as c:
        resp = await c.get("/boom")
    assert resp.status_code == 500
    body = resp.json()
    assert body["error_code"] == "INTERNAL_ERROR"
    assert "secret" not in str(body).lower()  # internals are not leaked to the client


async def test_framework_404_unknown_path_gets_envelope():
    async with _client(_app()) as c:
        resp = await c.get("/no-such-path")
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "NOT_FOUND"  # framework HTTPException, not a GraderError


async def test_framework_405_wrong_method_gets_envelope():
    async with _client(_app()) as c:
        resp = await c.post("/not-registered")  # a GET-only route
    assert resp.status_code == 405
    assert resp.json()["error_code"] == "METHOD_NOT_ALLOWED"


def test_typed_errors_carry_status_and_code():
    assert TestNotRegisteredError("x").status_code == 404
    assert TestNotRegisteredError("x").error_code.value == "TEST_NOT_REGISTERED"
    assert RubricNotGeneratedError("x").status_code == 409
    assert InvalidTestError("x").status_code == 400
