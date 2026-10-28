#!/usr/bin/env python3
"""Guard дрейфа метрик тестов/покрытия в README (docs vs реальность).

Зачем: числа «N tests / M% coverage» в README исторически указывались руками и
отставали от факта (78 -> 111 -> 118 -> ... -> 258). Этот скрипт — автоматическая
сверка единого источника правды:

    pytest --collect-only -q tests/   -> фактическое число тестов
    coverage.xml (line-rate)          -> фактический процент покрытия
    README.md / README.ru.md          -> задекларированные числа

Exit-коды: 0 — все числа сходятся; 1 — дрейф (CI падает); 2 — нет coverage.xml
(сначала выполните ``pytest --cov=src --cov-report=xml tests/``).

Использование: make docs-check   (в CI — шаг "Docs metrics drift guard").
"""

from __future__ import annotations

import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
READMES = (ROOT / "README.md", ROOT / "README.ru.md")

# Число тестов сверяем строго; покрытие — с допуском: line-rate слегка плавает
# между окружениями (версия Python/pytest), а в README оно округлённое («~93%»).
COVERAGE_TOLERANCE_POINTS = 1

# Паттерны задекларированного числа тестов (badge, features, структура, RU-строки)
TEST_PATTERNS = (
    r"tests-(\d+)%20passed",          # RU badge
    r"(\d+) unit/integration tests",  # EN features row
    r"(\d+) unit/интеграционных",      # RU bullet
    r"\*\*(\d+) passed\*\*",          # RU «Текущий набор»
    r"# (\d+) tests, fully mocked",   # EN structure comment
)
# Паттерны задекларированного покрытия
COV_PATTERNS = (
    r"coverage-(\d+)%25",       # EN badge
    r"\((\d+)% coverage",       # EN features row
    r"\*\*~(\d+)%\*\*",         # EN Testing section
    r"покрытие ~(\d+)%",        # RU bullet
    r"покрытие \*\*~(\d+)%\*\*",  # RU «Текущий набор»
)


def collect_test_count() -> int:
    """Фактическое число тестов: pytest --collect-only (без запуска)."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "tests/"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    match = re.search(r"(\d+) tests? collected", proc.stdout)
    if not match:
        raise RuntimeError(f"cannot parse collect-only output: {proc.stdout[-300:]}")
    return int(match.group(1))


def read_coverage_percent(path: Path | None = None) -> int:
    """Фактический coverage из coverage.xml (line-rate), округлённый вверх по арифметике."""
    cov = path or (ROOT / "coverage.xml")
    if not cov.exists():
        raise FileNotFoundError(cov)
    rate = float(ET.parse(cov).getroot().attrib["line-rate"])
    return int(rate * 100 + 0.5)


def extract_stated(text: str, patterns: tuple[str, ...]) -> list[tuple[str, int]]:
    """Все задекларированные числа из текста README с указанием паттерна."""
    found: list[tuple[str, int]] = []
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            found.append((pattern, int(match.group(1))))
    return found


def find_drift(
    stated_tests: list[tuple[str, int]],
    stated_cov: list[tuple[str, int]],
    actual_tests: int,
    actual_cov: int,
) -> list[str]:
    """Сравнить заявленные в README числа с фактом; вернуть список расхождений."""
    drift: list[str] = []
    for source, value in stated_tests:
        if value != actual_tests:
            drift.append(f"{source}: tests stated {value}, actual {actual_tests}")
    for source, value in stated_cov:
        if abs(value - actual_cov) > COVERAGE_TOLERANCE_POINTS:
            drift.append(
                f"{source}: coverage stated {value}%, actual {actual_cov}% "
                f"(tolerance ±{COVERAGE_TOLERANCE_POINTS})"
            )
    return drift


def main() -> int:
    try:
        actual_tests = collect_test_count()
    except (subprocess.CalledProcessError, RuntimeError) as exc:
        print(f"[docs-check] ERROR: cannot collect tests: {exc}", file=sys.stderr)
        return 2
    try:
        actual_cov = read_coverage_percent()
    except FileNotFoundError as exc:
        print(
            f"[docs-check] ERROR: {exc} not found — run "
            "`pytest --cov=src --cov-report=xml tests/` first",
            file=sys.stderr,
        )
        return 2

    stated_tests: list[tuple[str, int]] = []
    stated_cov: list[tuple[str, int]] = []
    for readme in READMES:
        text = readme.read_text(encoding="utf-8")
        stated_tests += [(readme.name, v) for _, v in extract_stated(text, TEST_PATTERNS)]
        stated_cov += [(readme.name, v) for _, v in extract_stated(text, COV_PATTERNS)]

    drift = find_drift(stated_tests, stated_cov, actual_tests, actual_cov)

    print(
        f"[docs-check] actual: {actual_tests} tests, {actual_cov}% coverage | "
        f"stated: {len(stated_tests)} test-numbers, {len(stated_cov)} coverage-numbers"
    )
    if not stated_tests or not stated_cov:
        print("[docs-check] ERROR: README lost its metric mentions", file=sys.stderr)
        return 1
    if drift:
        print("[docs-check] DRIFT DETECTED:", file=sys.stderr)
        for line in drift:
            print(f"  - {line}", file=sys.stderr)
        print(
            "[docs-check] обновите числа в README.md/README.ru.md "
            "(badge, features, Testing, структура)",
            file=sys.stderr,
        )
        return 1
    print("[docs-check] OK: README metrics match pytest reality")
    return 0


if __name__ == "__main__":
    sys.exit(main())
