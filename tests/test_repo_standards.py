"""Регресс-тесты open-source стандартов репозитория."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_license_mit_2026() -> None:
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "MIT License" in license_text
    assert "Copyright (c) 2026 Mkft1" in license_text


def test_pull_request_template_checklist() -> None:
    tpl = (ROOT / ".github" / "pull_request_template.md").read_text(encoding="utf-8")
    for item in ("make test", "make lint", "документация", "тесты"):
        assert item in tpl
    assert tpl.count("- [ ]") >= 4


def test_bug_report_template_fields() -> None:
    tpl = (ROOT / ".github" / "ISSUE_TEMPLATE" / "bug_report.md").read_text(encoding="utf-8")
    for section in ("## Описание бага", "## Шаги для воспроизведения", "## Ожидаемое поведение"):
        assert section in tpl


def test_changelog_keep_a_changelog() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "Keep a Changelog" in changelog
    assert "## [1.0.0]" in changelog
    for feature in ("RAG", "tracing", "Guardrails", "Evaluation"):
        assert feature.lower() in changelog.lower()
