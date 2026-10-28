"""Тесты guard'а дрейфа метрик README (scripts/check_docs_metrics.py)."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_docs_metrics.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_docs_metrics", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


chk = _load_checker()


class TestParsers:
    def test_extract_stated_tests(self) -> None:
        text = "![Tests](https://img.shields.io/badge/tests-258%20passed) | 258 unit/integration tests"
        values = [v for _, v in chk.extract_stated(text, chk.TEST_PATTERNS)]
        assert values == [258, 258]

    def test_extract_stated_coverage(self) -> None:
        text = "coverage-93%25 ... (93% coverage) ... **~93%** ... покрытие ~93%"
        values = [v for _, v in chk.extract_stated(text, chk.COV_PATTERNS)]
        assert values == [93, 93, 93, 93]

    def test_coverage_rounding(self, tmp_path: Path) -> None:
        cov = tmp_path / "coverage.xml"
        cov.write_text(
            '<?xml version="1.0"?><coverage line-rate="0.9251" branches-rate="0"></coverage>',
            encoding="utf-8",
        )
        assert chk.read_coverage_percent(cov) == 93

    def test_missing_coverage_raises(self, tmp_path: Path) -> None:
        import pytest as _pytest

        with _pytest.raises(FileNotFoundError):
            chk.read_coverage_percent(tmp_path / "nope.xml")


class TestDriftTolerance:
    def test_coverage_tolerance_one_point(self) -> None:
        drift = chk.find_drift([("README.md", 270)], [("README.md", 93)], 270, 92)
        assert drift == []  # ±1 пункт покрытия — не дрейф

    def test_coverage_beyond_tolerance_is_drift(self) -> None:
        drift = chk.find_drift([("README.md", 270)], [("README.md", 93)], 270, 90)
        assert drift and "coverage" in drift[0]

    def test_test_count_must_match_exactly(self) -> None:
        drift = chk.find_drift([("README.md", 270)], [("README.md", 93)], 271, 93)
        assert drift and "tests" in drift[0]


class TestReadmeConsistency:
    def test_en_ru_numbers_match(self) -> None:
        en = (ROOT / "README.md").read_text(encoding="utf-8")
        ru = (ROOT / "README.ru.md").read_text(encoding="utf-8")
        en_tests = {v for _, v in chk.extract_stated(en, chk.TEST_PATTERNS)}
        ru_tests = {v for _, v in chk.extract_stated(ru, chk.TEST_PATTERNS)}
        en_cov = {v for _, v in chk.extract_stated(en, chk.COV_PATTERNS)}
        ru_cov = {v for _, v in chk.extract_stated(ru, chk.COV_PATTERNS)}
        assert en_tests and en_tests == ru_tests
        assert en_cov and en_cov == ru_cov


class TestWiring:
    def test_script_exists_and_ci_wired(self) -> None:
        assert SCRIPT.exists()
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        assert "check_docs_metrics.py" in ci
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        assert "docs-check" in makefile

    def test_collect_count_matches_suite(self) -> None:
        """collect-only парсится и совпадает с числом, задекларированным в README."""
        actual = chk.collect_test_count()
        en = (ROOT / "README.md").read_text(encoding="utf-8")
        assert actual in {v for _, v in chk.extract_stated(en, chk.TEST_PATTERNS)}
        assert re.match(r"\d{3}", str(actual))
