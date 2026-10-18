"""Тесты evaluation-фреймворка (eval/evaluate.py) без реального кластера и LLM."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.agent.llm_client import LLMError
from src.config import Settings

EVAL_PATH = Path(__file__).resolve().parents[1] / "eval" / "evaluate.py"


def _load_eval():
    spec = importlib.util.spec_from_file_location("eval_evaluate", EVAL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ev = _load_eval()


@pytest.fixture()
def dataset() -> list[dict]:
    return ev.load_dataset(ev.DEFAULT_DATASET)


def _mock_llm(judge_answer: str = "5", fix: str = "Increase the memory limit to 1Gi") -> MagicMock:
    llm = MagicMock()
    llm.generate.return_value = json.dumps(
        {
            "root_cause": "Container OOMKilled: memory limit exceeded",
            "evidence": ["restarts=5"],
            "suggested_fix": fix,
            "severity": "high",
        }
    )
    llm.chat.return_value = judge_answer
    return llm


class TestDataset:
    def test_golden_dataset_has_three_cases(self, dataset) -> None:
        assert len(dataset) == 3

    def test_case_schema(self, dataset) -> None:
        for case in dataset:
            assert set(case["incident"]) >= {"namespace", "pod_name", "description"}
            assert case["expected_root_cause"]
            runbook = Path("docs/runbooks") / case["expected_runbook"]
            assert runbook.exists(), f"expected runbook missing: {runbook}"

    def test_scenarios_cover_all_cases(self, dataset) -> None:
        for case in dataset:
            assert case["expected_runbook"] in ev.SCENARIOS

    def test_load_dataset_validates_schema(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps([{"incident": {"namespace": "x"}}]), encoding="utf-8")
        with pytest.raises(ValueError):
            ev.load_dataset(bad)


class TestJudge:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("5", 5), ("3\n", 3), ("Оценка: 4", 4), ("1", 1), ("abc", None), ("", None)],
    )
    def test_parse_judge_score(self, raw: str, expected: int | None) -> None:
        assert ev.parse_judge_score(raw) == expected

    def test_judge_prompt_follows_spec(self) -> None:
        llm = _mock_llm(judge_answer="4")
        score = ev.judge_score(llm, expected="OOMKilled due to memory limit", actual="memory limit caused OOMKill")
        assert score == 4
        user_prompt = llm.chat.call_args.kwargs["user_prompt"]
        assert "Сравни expected_root_cause: OOMKilled due to memory limit" in user_prompt
        assert "actual_root_cause: memory limit caused OOMKill" in user_prompt
        assert "Верни только цифру" in user_prompt


class TestRunCase:
    @pytest.fixture()
    def offline_settings(self) -> Settings:
        # Гибрид off: тесты не скачивают embedding-модель (CI остаётся быстрым).
        return Settings(_env_file=None, enable_hybrid_rag=False)

    def test_run_case_finds_runbook_and_scores(self, dataset, offline_settings) -> None:
        result = ev.run_case(dataset[0], _mock_llm(), offline_settings)
        assert result["runbook"] == "oom_killed.md"
        assert result["runbook_found"] is True  # RAG подтянул oom_killed.md
        assert result["score"] == 5
        assert result["actual_root_cause"]

    def test_run_case_all_cases_retrieve_expected_runbooks(self, dataset, offline_settings) -> None:
        for case in dataset:
            result = ev.run_case(case, _mock_llm(), offline_settings)
            assert result["runbook_found"] is True, case["expected_runbook"]


class TestSummaryAndReport:
    def test_summarize_metrics(self) -> None:
        results = [
            {"score": 5, "runbook_found": True},
            {"score": 4, "runbook_found": True},
            {"score": 3, "runbook_found": False},
        ]
        summary = ev.summarize(results)
        assert summary["cases"] == 3
        assert summary["avg_score"] == pytest.approx(4.0)
        assert summary["runbook_hit_rate"] == pytest.approx(66.666, abs=0.1)

    def test_report_pass_and_fail(self, capsys) -> None:
        good = [{"runbook": "oom_killed.md", "pod": "p", "actual_root_cause": "oom",
                 "score": 5, "runbook_found": True, "warnings": 0}]
        report = ev.print_report(good, ev.summarize(good), min_score=4.0)
        assert "✅ PASS" in report
        bad = [dict(good[0], score=2, runbook_found=False)]
        report = ev.print_report(bad, ev.summarize(bad), min_score=4.0)
        assert "❌ FAIL" in report
        assert capsys.readouterr().out  # отчёт печатается в консоль


class TestMainExitCodes:
    def test_missing_llm_key_exits_2(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*args, **kwargs):
            raise LLMError("API-ключ LLM не задан")

        monkeypatch.setattr(ev, "LLMClient", boom)
        assert ev.main([]) == 2

    def test_bad_dataset_exits_2(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        missing = tmp_path / "nope.json"
        assert ev.main(["--dataset", str(missing)]) == 2

    def test_full_run_passes_with_perfect_judge(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ev, "LLMClient", lambda: _mock_llm(judge_answer="5"))
        monkeypatch.setattr(
            ev, "get_settings", lambda: Settings(_env_file=None, enable_hybrid_rag=False)
        )
        assert ev.main(["--min-score", "4"]) == 0

    def test_low_score_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ev, "LLMClient", lambda: _mock_llm(judge_answer="2"))
        monkeypatch.setattr(
            ev, "get_settings", lambda: Settings(_env_file=None, enable_hybrid_rag=False)
        )
        assert ev.main(["--min-score", "4"]) == 1
