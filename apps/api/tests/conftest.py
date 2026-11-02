"""Synthetic reviews remain ephemeral unless a test explicitly enables persistence."""
import pytest

from app.services.ocr_review_registry import ocr_review_registry


@pytest.fixture(autouse=True)
def ephemeral_ocr_registry(monkeypatch):
    ocr_review_registry.clear()
    monkeypatch.setattr(ocr_review_registry, "_repository_factory", None)
    yield
    ocr_review_registry.clear()
