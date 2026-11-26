"""Tests for the local OCR result parser."""

from __future__ import annotations

import pytest

from zero_agent.utils import ocr_utils


def _detections():
    return [
        [[[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]], "AB12", 0.9],
    ]


def test_ocr_image_accepts_rapidocr_tuple_result(monkeypatch) -> None:
    image_module = pytest.importorskip("PIL.Image")
    monkeypatch.setattr(
        ocr_utils, "_get_ocr", lambda: lambda image: (_detections(), [0.01]),
    )

    result = ocr_utils.ocr_image(image_module.new("RGB", (10, 10)))

    assert result["text"] == "AB12"
    assert result["lines"] == ["AB12"]
    assert result["details"][0]["conf"] == 0.9


def test_ocr_image_accepts_legacy_list_result(monkeypatch) -> None:
    image_module = pytest.importorskip("PIL.Image")
    monkeypatch.setattr(ocr_utils, "_get_ocr", lambda: lambda image: _detections())

    result = ocr_utils.ocr_image(image_module.new("RGB", (10, 10)))

    assert result["text"] == "AB12"
