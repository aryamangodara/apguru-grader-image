"""Regression tests for issue #73 — the app (and therefore the whole test suite)
must import without PyMuPDF (``fitz``) installed.

PyMuPDF ships no wheel for Python 3.14, so a module-level ``import fitz`` in
``app/services/grader/core.py`` made ``from app.main import create_app`` fail and
produced 163 pytest collection errors. The fix defers the ``fitz`` import into
``render_pdf_to_images`` (its only consumer); these tests lock that in by
simulating ``fitz`` being unavailable via ``sys.modules``.
"""
import importlib
import sys
from pathlib import Path

import pytest


def test_grader_core_imports_without_fitz(monkeypatch):
    """`app.services.grader.core` must import even when `import fitz` fails.

    This is the heart of #73: if any top-level statement in core.py imports
    fitz, this re-import raises and the whole app (hence 163 tests) fails to
    collect on a PyMuPDF-less interpreter.
    """
    # Simulate the missing 3.14 wheel: make `import fitz` raise ImportError.
    monkeypatch.setitem(sys.modules, "fitz", None)
    # Force a fresh execution of the module body with fitz unavailable.
    monkeypatch.delitem(sys.modules, "app.services.grader.core", raising=False)

    core = importlib.import_module("app.services.grader.core")

    assert hasattr(core, "render_pdf_to_images")


def test_render_pdf_to_images_errors_clearly_without_fitz(monkeypatch):
    """The renderer raises a clear, actionable error when PyMuPDF is absent."""
    monkeypatch.setitem(sys.modules, "fitz", None)

    from app.services.grader.core import render_pdf_to_images

    with pytest.raises(RuntimeError, match=r"(?i)pymupdf"):
        render_pdf_to_images(Path("nonexistent.pdf"))
