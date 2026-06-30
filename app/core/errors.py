"""Typed domain errors + machine-readable error codes for the grader API.

Every client-facing failure raises a :class:`GraderError` subclass that carries a
stable ``error_code`` (for consumers to branch on) and an HTTP ``status_code``. A
single set of handlers (:func:`register_exception_handlers`) renders those — plus
FastAPI's own request-validation / HTTP / unhandled errors — into ONE consistent
envelope::

    {"error_code": "TEST_NOT_REGISTERED", "detail": "test_id 322 is not registered"}

``detail`` keeps its existing shape (a string, or FastAPI's field-error list for a 422
request-validation error), so consumers that already read ``detail`` are unaffected —
this only ADDS ``error_code``.
"""
from __future__ import annotations

from enum import StrEnum
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

log = structlog.get_logger(__name__)


class ErrorCode(StrEnum):
    """Stable, machine-readable error codes returned in every error response."""

    # Domain errors
    TEST_NOT_REGISTERED = "TEST_NOT_REGISTERED"
    RUBRIC_NOT_GENERATED = "RUBRIC_NOT_GENERATED"
    INVALID_TEST_ID = "INVALID_TEST_ID"
    UNKNOWN_COURSE = "UNKNOWN_COURSE"
    INVALID_SUBMISSION = "INVALID_SUBMISSION"
    INVALID_PDF_URL = "INVALID_PDF_URL"
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    MISSING_JOB_FILTER = "MISSING_JOB_FILTER"
    # Framework / generic
    VALIDATION_ERROR = "VALIDATION_ERROR"
    BAD_REQUEST = "BAD_REQUEST"
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ErrorResponse(BaseModel):
    """The error envelope — documents the shape + codes in OpenAPI (``/docs``)."""

    error_code: ErrorCode = Field(description="Stable machine-readable code to branch on.")
    detail: Any = Field(
        description="Human-readable message (a string), or a list of field errors for 422."
    )


class GraderError(Exception):
    """Base for client-facing grader errors; subclasses set status_code + error_code."""

    __test__ = False  # a subclass named Test* (e.g. TestNotRegisteredError) is not a pytest case

    status_code: int = 400
    error_code: ErrorCode = ErrorCode.INTERNAL_ERROR

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class TestNotRegisteredError(GraderError):
    status_code = 404
    error_code = ErrorCode.TEST_NOT_REGISTERED


class RubricNotGeneratedError(GraderError):
    status_code = 409  # the exam exists but isn't gradeable yet (refined from 404)
    error_code = ErrorCode.RUBRIC_NOT_GENERATED


class InvalidTestError(GraderError):
    status_code = 400
    error_code = ErrorCode.INVALID_TEST_ID


class UnknownCourseError(GraderError):
    status_code = 400
    error_code = ErrorCode.UNKNOWN_COURSE


class InvalidSubmissionError(GraderError):
    status_code = 400
    error_code = ErrorCode.INVALID_SUBMISSION


class InvalidPdfUrlError(GraderError):
    status_code = 400
    error_code = ErrorCode.INVALID_PDF_URL


class JobNotFoundError(GraderError):
    status_code = 404
    error_code = ErrorCode.JOB_NOT_FOUND


class MissingJobFilterError(GraderError):
    status_code = 400
    error_code = ErrorCode.MISSING_JOB_FILTER


# Generic code for a bare HTTPException (e.g. a framework 404/405), keyed by status.
_STATUS_TO_CODE: dict[int, ErrorCode] = {
    400: ErrorCode.BAD_REQUEST,
    404: ErrorCode.NOT_FOUND,
    405: ErrorCode.METHOD_NOT_ALLOWED,
    422: ErrorCode.VALIDATION_ERROR,
}


def _envelope(status_code: int, error_code: ErrorCode, detail: Any) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error_code": error_code.value, "detail": detail},
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Render every error as the :class:`ErrorResponse` envelope. Call once in create_app."""

    @app.exception_handler(GraderError)
    async def _grader_error(_request: Request, exc: GraderError) -> JSONResponse:
        return _envelope(exc.status_code, exc.error_code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # Preserve FastAPI's structured field-error list as `detail`; jsonable_encoder
        # makes it JSON-safe (an errors() entry's `ctx` can hold a raw exception object).
        return _envelope(422, ErrorCode.VALIDATION_ERROR, jsonable_encoder(exc.errors()))

    # Register for Starlette's HTTPException (the base) so this also catches the
    # framework-raised 404 (unknown path) / 405 (wrong method); fastapi.HTTPException
    # is a subclass and registering for it would miss those.
    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _STATUS_TO_CODE.get(exc.status_code, ErrorCode.INTERNAL_ERROR)
        return _envelope(exc.status_code, code, exc.detail)

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, exc: Exception) -> JSONResponse:
        # Log the real cause; never leak internals/stack traces to the client. NOTE: this
        # only runs when the app is built with debug=False (prod sets DEBUG=False in .env);
        # under debug=True Starlette renders its own traceback page instead (dev only).
        log.error("unhandled_exception", error=str(exc), exc_info=exc)
        return _envelope(500, ErrorCode.INTERNAL_ERROR, "Internal server error")
