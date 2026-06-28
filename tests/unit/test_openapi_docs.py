"""Enforce API-documentation completeness on the generated OpenAPI schema.

This is the "lint" for API docs: ruff is a static linter and can't check that
every parameter and response field is *documented*, so we assert it against the
schema FastAPI generates. It runs in the same CI gate as the rest of the suite,
so a new endpoint or model field that ships without a description (or a route
without a response_model / summary) turns the build red.

What it guards (mirrors the team's API-doc conventions):
  * every endpoint parameter (path/query/header) has a description
  * every request/response model property has a description
  * every operation has a summary AND a description
  * every operation declares a typed 2xx response (i.e. response_model=)
  * every public grader operation documents the {error_code, detail} envelope
  * the primary request bodies carry a copy-pasteable example
"""
from __future__ import annotations

from collections.abc import Iterator

import pytest

from app.core.errors import ErrorResponse
from app.main import create_app

# Health endpoints are liveness probes — they never raise a domain error, so the
# error-envelope assertion is scoped to the public grader surface.
GRADER_PREFIX = "/api/v1/grader/"
REQUEST_MODELS_NEEDING_EXAMPLES = ("RegisterExamRequest", "CreateSubmissionRequest")
_HTTP_METHODS = {"get", "post", "put", "patch", "delete"}


@pytest.fixture(scope="module")
def spec() -> dict:
    """The generated OpenAPI document (built once; no DB or network needed)."""
    return create_app().openapi()


def _operations(spec: dict) -> Iterator[tuple[str, str, dict]]:
    """Yield (METHOD, path, operation) for every real HTTP operation."""
    for path, methods in spec.get("paths", {}).items():
        for method, op in methods.items():
            if method in _HTTP_METHODS and isinstance(op, dict):
                yield method.upper(), path, op


def _fail(label: str, items: list[str]) -> str:
    return f"{label}:\n  " + "\n  ".join(items)


def test_every_schema_property_has_a_description(spec: dict) -> None:
    missing: list[str] = []
    for name, sch in spec.get("components", {}).get("schemas", {}).items():
        for field, meta in (sch.get("properties") or {}).items():
            # A property documents itself via `description`, or defers to the
            # model it $refs (which is asserted on its own).
            if meta.get("description") or "$ref" in meta or "allOf" in meta:
                continue
            missing.append(f"{name}.{field}")
    assert not missing, _fail("Schema properties missing a Field(description=...)", missing)


def test_every_endpoint_parameter_has_a_description(spec: dict) -> None:
    missing = [
        f"{method} {path} -> {p.get('in')} '{p.get('name')}'"
        for method, path, op in _operations(spec)
        for p in op.get("parameters", [])
        if not p.get("description")
    ]
    assert not missing, _fail("Params missing a description (use Query/Path(description=...))", missing)


def test_every_operation_has_summary_and_description(spec: dict) -> None:
    missing = [
        f"{method} {path}"
        for method, path, op in _operations(spec)
        if not op.get("summary") or not op.get("description")
    ]
    assert not missing, _fail("Operations missing a summary= or a route-docstring description", missing)


def test_every_operation_declares_a_typed_success_response(spec: dict) -> None:
    # response_model= is what makes FastAPI attach a schema to the 2xx response.
    missing: list[str] = []
    for method, path, op in _operations(spec):
        success = (r for code, r in op.get("responses", {}).items() if code.startswith("2"))
        has_schema = any(
            (r.get("content") or {}).get("application/json", {}).get("schema") for r in success
        )
        if not has_schema:
            missing.append(f"{method} {path}")
    assert not missing, _fail("Operations without a typed 2xx response (add response_model=)", missing)


def test_grader_operations_document_the_error_envelope(spec: dict) -> None:
    ref = ErrorResponse.__name__
    missing: list[str] = []
    for method, path, op in _operations(spec):
        if not path.startswith(GRADER_PREFIX):
            continue
        documents_envelope = any(
            code.startswith("4")
            and ref in str((r.get("content") or {}).get("application/json", {}).get("schema", {}))
            for code, r in op.get("responses", {}).items()
        )
        if not documents_envelope:
            missing.append(f"{method} {path}")
    assert not missing, _fail(f"Grader operations not documenting the {ref} 4xx envelope", missing)


def test_primary_request_models_have_examples(spec: dict) -> None:
    schemas = spec.get("components", {}).get("schemas", {})
    missing = [
        name
        for name in REQUEST_MODELS_NEEDING_EXAMPLES
        if not (schemas.get(name, {}).get("examples") or schemas.get(name, {}).get("example"))
    ]
    assert not missing, _fail("Request models without an OpenAPI example", missing)
