#!/usr/bin/env python3
"""Evaluation framework: качество ответов агента (LLM-as-a-judge).

Зачем: промпты, RAG-база и модели меняются — без измерителя качества эти
изменения слепы. Фреймворк прогоняет золотой датасет инцидентов
(``eval/golden_dataset.json``) через реальный пайплайн оркестратора
(RAG + LLM), а затем отдельный LLM-выступaет судьёй: сравнивает ожидаемый
root cause с фактическим по шкале 1–5 (semantic match, не совпадение строк).

Метрики:
    * средний балл судьи (1–5);
    * runbook hit-rate — доля кейсов, где RAG подтянул ожидаемый runbook
      (проверяется по содержимому ``relevant_runbooks`` в raw_data диагноза).

Инструменты K8s/Prometheus/логов ЗАМОКАНЫ сценарными симптомами (SMPTOMS по
имени ожидаемого runbook'а): оценка измеряет логику RAG+LLM, а не доступность
кластера. Реальный кластер не требуется.

Запуск:
    make eval                     # или: python eval/evaluate.py
    python eval/evaluate.py --min-score 4.0 --dataset eval/golden_dataset.json

Exit-коды: 0 — оценка выше порога и все runbook'и найдены; 1 — ниже порога;
2 — ошибка окружения (нет LLM-ключа, битый датасет).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agent.llm_client import LLMClient, LLMError  # noqa: E402
from src.agent.orchestrator import IncidentOrchestrator  # noqa: E402
from src.agent.rag import DEFAULT_RUNBOOKS_DIR  # noqa: E402
from src.config import get_settings  # noqa: E402

EVAL_DIR = Path(__file__).resolve().parent
DEFAULT_DATASET = EVAL_DIR / "golden_dataset.json"

# Промпт судьи (по спеке): сравнение по смыслу, ответ — только цифра 1–5.
JUDGE_SYSTEM_PROMPT = (
    "Ты — строгий eval-судья. Сравниваешь две формулировки root cause "
    "инцидента по СМЫСЛУ (не по букве) и отвечаешь одной цифрой от 1 до 5."
)
JUDGE_USER_PROMPT = (
    "Сравни expected_root_cause: {expected} и actual_root_cause: {actual}. "
    "Оцени по шкале от 1 до 5, где 5 - полное совпадение по смыслу. "
    "Верни только цифру."
)

# Сценарные симптомы для моков инструментов: ключ — имя ожидаемого runbook'а.
# Тексты насыщены терминами соответствующих runbook'ов, чтобы RAG-поиск
# (TF-IDF/гибрид) находил их по симптомам, как в реальном инциденте.
SCENARIOS: dict[str, dict] = {
    "oom_killed.md": {
        "pods": [
            {
                "name": "payments-api-7d9f4b-x2kq",
                "status": "OOMKilled",
                "restarts": 5,
                "node": "node-a",
                "age": 3600,
                "labels": {"app": "payments-api"},
            }
        ],
        "events": [
            {
                "type": "Warning",
                "reason": "OOMKilling",
                "message": "Memory cgroup out of memory: Killed process 1234 (payments-api)",
                "involved_object": "Pod/payments-api-7d9f4b-x2kq",
                "count": 3,
                "last_timestamp": "2026-10-19T09:40:55Z",
            }
        ],
        "logs": (
            "2026-10-19T09:40:50Z allocating batch buffer 512Mi\n"
            "2026-10-19T09:40:55Z fatal error: runtime: out of memory\n"
            "2026-10-19T09:40:55Z container memory working set bytes reached limit\n"
        ),
        "cpu": 0.42,
        "mem": 511_705_088,
    },
    "image_pull_backoff.md": {
        "pods": [
            {
                "name": "bad-pod",
                "status": "ImagePullBackOff",
                "restarts": 0,
                "node": "node-b",
                "age": 120,
                "labels": {"run": "bad-pod"},
            }
        ],
        "events": [
            {
                "type": "Warning",
                "reason": "Failed",
                "message": "Failed to pull image: manifest for tag latest not found",
                "involved_object": "Pod/bad-pod",
                "count": 6,
                "last_timestamp": "2026-10-19T10:00:00Z",
            },
            {
                "type": "Warning",
                "reason": "Failed",
                "message": "Error: ErrImagePull, Back-off pulling image",
                "involved_object": "Pod/bad-pod",
                "count": 6,
                "last_timestamp": "2026-10-19T10:00:10Z",
            },
        ],
        "logs": "",
        "cpu": None,
        "mem": None,
    },
    "crash_loop.md": {
        "pods": [
            {
                "name": "web-1",
                "status": "CrashLoopBackOff",
                "restarts": 7,
                "node": "node-a",
                "age": 900,
                "labels": {"app": "web"},
            }
        ],
        "events": [
            {
                "type": "Warning",
                "reason": "BackOff",
                "message": "Back-off restarting failed container web of pod web-1",
                "involved_object": "Pod/web-1",
                "count": 7,
                "last_timestamp": "2026-10-19T11:00:00Z",
            }
        ],
        "logs": (
            "Traceback (most recent call last):\n"
            '  File "/app/main.py", line 12, in <module>\n'
            "RuntimeError: Missing env configuration: DATABASE_URL\n"
            "container exits repeatedly at startup, CrashLoopBackOff\n"
        ),
        "cpu": 0.01,
        "mem": 40_000_000,
    },
}


def load_dataset(path: Path = DEFAULT_DATASET) -> list[dict]:
    """Загрузить золотой датасет инцидентов и проверить обязательные поля."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    for i, case in enumerate(data):
        for key in ("incident", "expected_root_cause", "expected_runbook"):
            if key not in case:
                raise ValueError(f"case #{i} missing required key {key!r}")
        for key in ("namespace", "pod_name", "description"):
            if key not in case["incident"]:
                raise ValueError(f"case #{i} incident missing key {key!r}")
    return data


def build_mock_tools(scenario: dict):
    """Моки K8sTools/PrometheusTools/LogsTools с симптомами сценария.

    Реальный кластер не нужен: оцениваем логику RAG+LLM, а не доступность API.
    """
    k8s = MagicMock()
    k8s.get_pods.return_value = scenario["pods"]
    k8s.get_events.return_value = scenario["events"]
    prom = MagicMock()
    prom.get_pod_cpu_usage.return_value = scenario["cpu"]
    prom.get_pod_memory_usage.return_value = scenario["mem"]
    logs = MagicMock()
    logs.get_pod_logs.return_value = scenario["logs"]
    return k8s, prom, logs


def parse_judge_score(raw: str) -> int | None:
    """Извлечь оценку 1–5 из ответа судьи (толерантно к пояснениям модели)."""
    match = re.search(r"[1-5]", raw or "")
    return int(match.group()) if match else None


def judge_score(llm: LLMClient, expected: str, actual: str) -> int | None:
    """LLM-as-a-judge: семантическая оценка совпадения root cause (1–5)."""
    raw = llm.chat(
        system_prompt=JUDGE_SYSTEM_PROMPT,
        user_prompt=JUDGE_USER_PROMPT.format(expected=expected, actual=actual),
    )
    return parse_judge_score(raw)


def run_case(case: dict, llm: LLMClient, settings=None) -> dict:
    """Прогнать один кейс датасета через оркестратор и судью.

    Args:
        case: элемент золотого датасета.
        llm: реальный :class:`LLMClient` (диагностика + судья).
        settings: опциональный :class:`~src.config.Settings` (тесты передают
            ``enable_hybrid_rag=False``, чтобы не скачивать модель в CI).
    """
    runbook = case["expected_runbook"]
    scenario = SCENARIOS[runbook]
    k8s, prom, logs = build_mock_tools(scenario)
    orchestrator = IncidentOrchestrator(
        k8s_tools=k8s,
        prometheus_tools=prom,
        logs_tools=logs,
        llm_client=llm,
        settings=settings if settings is not None else get_settings(),
    )
    diagnosis = orchestrator.diagnose(case["incident"])
    actual = diagnosis.get("root_cause", "")

    relevant = diagnosis.get("raw_data", {}).get("relevant_runbooks", []) or []
    expected_text = (DEFAULT_RUNBOOKS_DIR / runbook).read_text(encoding="utf-8")
    runbook_found = any(expected_text == text for text in relevant)

    score = judge_score(llm, case["expected_root_cause"], actual)
    return {
        "runbook": runbook,
        "pod": case["incident"]["pod_name"],
        "actual_root_cause": actual,
        "score": score,
        "runbook_found": runbook_found,
        "warnings": len(diagnosis.get("warnings", [])),
    }


def run_evaluation(dataset: list[dict], llm: LLMClient, settings=None) -> list[dict]:
    """Прогнать весь датасет и вернуть результаты по каждому кейсу."""
    return [run_case(case, llm, settings) for case in dataset]


def summarize(results: list[dict]) -> dict:
    """Агрегаты: средний балл судьи и runbook hit-rate."""
    scores = [r["score"] for r in results if r["score"] is not None]
    hits = sum(1 for r in results if r["runbook_found"])
    return {
        "cases": len(results),
        "avg_score": (sum(scores) / len(scores)) if scores else 0.0,
        "judged": len(scores),
        "runbook_hits": hits,
        "runbook_hit_rate": (hits / len(results) * 100) if results else 0.0,
    }


def print_report(results: list[dict], summary: dict, min_score: float) -> str:
    """Красивый консольный отчёт; возвращает итоговый вердикт PASS/FAIL."""
    lines = [
        "",
        "=" * 78,
        " EVALUATION REPORT — LLM-as-a-judge (golden dataset)".center(78),
        "=" * 78,
        f" {'CASE (runbook)':<26}{'JUDGE 1-5':>10}  {'RUNBOOK':<8}{'ACTUAL ROOT CAUSE':<32}",
        "-" * 78,
    ]
    for r in results:
        score = f"{r['score']}/5" if r["score"] is not None else "n/a"
        hit = "✅" if r["runbook_found"] else "❌"
        actual = (r["actual_root_cause"] or "—")[:30] + (
            "…" if len(r["actual_root_cause"] or "") > 30 else ""
        )
        lines.append(f" {r['runbook']:<26}{score:>10}  {hit:<8}{actual:<32}")
    passed = summary["avg_score"] >= min_score and summary["runbook_hit_rate"] == 100
    lines += [
        "-" * 78,
        f" Average judge score : {summary['avg_score']:.2f} / 5"
        f"  (порог: {min_score:.1f})",
        f" Runbook hit-rate    : {summary['runbook_hit_rate']:.0f}%"
        f"  ({summary['runbook_hits']}/{summary['cases']})",
        f" Verdict             : {'✅ PASS' if passed else '❌ FAIL'}",
        "=" * 78,
        "",
    ]
    report = "\n".join(lines)
    print(report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate agent quality (LLM-as-a-judge)")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument(
        "--min-score", type=float, default=4.0, help="порог среднего балла судьи (0-5)"
    )
    args = parser.parse_args(argv)

    try:
        dataset = load_dataset(args.dataset)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[eval] ERROR: cannot load dataset {args.dataset}: {exc}", file=sys.stderr)
        return 2

    try:
        llm = LLMClient()
    except LLMError as exc:
        print(
            f"[eval] ERROR: {exc}\n"
            "[eval] Evaluation требует реальный LLM (судья и диагностика). "
            "Задайте QWEN_API_KEY или OPENAI_API_KEY в .env/окружении.",
            file=sys.stderr,
        )
        return 2

    results = run_evaluation(dataset, llm, get_settings())
    summary = summarize(results)
    print_report(results, summary, args.min_score)
    passed = summary["avg_score"] >= args.min_score and summary["runbook_hit_rate"] == 100
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
