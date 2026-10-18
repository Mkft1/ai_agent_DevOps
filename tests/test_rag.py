"""Unit-тесты RunbookRAG (src/agent/rag.py) на замоканных md-файлах.

Реальные docs/runbooks не читаются — используется tmp_path, чтобы тесты не
зависели от содержимого репозитория.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.agent.orchestrator import IncidentOrchestrator
from src.agent.rag import RunbookRAG

OOM_DOC = """# Runbook OOMKilled
Pod was OOMKilled with exit code 137. Memory limit exceeded.
Increase resources.limits.memory or fix the leak.
"""

PULL_DOC = """# Runbook ImagePullBackOff
Failed to pull image: manifest unknown, registry unauthorized.
Check image name, tag and imagePullSecrets credentials.
"""

CRASH_DOC = """# Runbook CrashLoopBackOff
Container exits repeatedly with traceback at startup.
Missing env configuration; check probes and rollback deployment.
"""


def make_runbooks(dir_: Path) -> None:
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / "oom_killed.md").write_text(OOM_DOC, encoding="utf-8")
    (dir_ / "image_pull_backoff.md").write_text(PULL_DOC, encoding="utf-8")
    (dir_ / "crash_loop.md").write_text(CRASH_DOC, encoding="utf-8")


@pytest.fixture()
def rag_dir(tmp_path: Path) -> Path:
    d = tmp_path / "runbooks"
    make_runbooks(d)
    return d


class TestLoadRunbooks:
    def test_loads_all_md_files(self, rag_dir: Path):
        rag = RunbookRAG(rag_dir)
        assert rag.document_count == 3

    def test_missing_dir_is_empty_not_crash(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        with caplog.at_level(logging.WARNING):
            rag = RunbookRAG(tmp_path / "nope")
        assert rag.document_count == 0
        assert rag.find_relevant("anything") == []
        assert "not found" in caplog.text

    def test_ignores_non_md_and_empty_files(self, tmp_path: Path):
        d = tmp_path / "rb"
        d.mkdir()
        (d / "good.md").write_text("OOMKilled memory limit", encoding="utf-8")
        (d / "empty.md").write_text("   \n", encoding="utf-8")
        (d / "notes.txt").write_text("OOMKilled memory limit", encoding="utf-8")
        rag = RunbookRAG(d)
        assert rag.document_count == 1

    def test_reload_picks_up_new_file(self, rag_dir: Path):
        rag = RunbookRAG(rag_dir)
        assert rag.document_count == 3
        (rag_dir / "new.md").write_text("# Node pressure eviction", encoding="utf-8")
        rag.load_runbooks()
        assert rag.document_count == 4


class TestFindRelevant:
    def test_oom_query_matches_oom_runbook_first(self, rag_dir: Path):
        rag = RunbookRAG(rag_dir)
        hits = rag.find_relevant(
            "Pod status OOMKilled exit code 137 memory limit exceeded", top_k=1
        )
        assert len(hits) == 1
        assert "OOMKilled" in hits[0]
        assert "ImagePullBackOff" not in hits[0]

    def test_pull_query_matches_pull_runbook(self, rag_dir: Path):
        rag = RunbookRAG(rag_dir)
        hits = rag.find_relevant(
            "Failed to pull image manifest unknown unauthorized registry", top_k=1
        )
        assert hits and "pull image" in hits[0]

    def test_top_k_limits_results(self, rag_dir: Path):
        rag = RunbookRAG(rag_dir)
        assert len(rag.find_relevant("pod", top_k=2)) <= 2

    def test_irrelevant_query_returns_empty(self, rag_dir: Path):
        rag = RunbookRAG(rag_dir)
        # Ни одного совпадения с лексиконом runbook'ов -> пустой результат.
        assert rag.find_relevant("banana smoothie unicorn") == []

    def test_empty_query_returns_empty(self, rag_dir: Path):
        rag = RunbookRAG(rag_dir)
        assert rag.find_relevant("") == []
        assert rag.find_relevant("   ") == []

    def test_normalize_query_strips_timestamps_and_hashes(self, rag_dir: Path):
        rag = RunbookRAG(rag_dir)
        noisy = (
            "2026-10-18T10:22:33Z pod web-7d4f9ab-x2k OOMKilled exit code 137 "
            "memory limit exceeded abcdef1234567890"
        )
        clean = "pod OOMKilled exit code 137 memory limit exceeded"
        assert rag.find_relevant(noisy, top_k=1) == rag.find_relevant(clean, top_k=1)


class TestOrchestratorRagIntegration:
    """RAG встроен в diagnose(): найденные runbook'и уходят в контекст LLM."""

    @staticmethod
    def _make_orch(rag: object) -> tuple[IncidentOrchestrator, MagicMock]:
        k8s = MagicMock()
        k8s.get_pods.return_value = [
            {"name": "api-1", "status": "OOMKilled", "restarts": 3, "node": "w1"}
        ]
        k8s.get_events.return_value = []
        prom = MagicMock()
        prom.get_pod_cpu_usage.return_value = 0.1
        prom.get_pod_memory_usage.return_value = 900_000_000
        logs_tools = MagicMock()
        logs_tools.get_pod_logs.return_value = "Out of memory! Killed process\n"
        llm = MagicMock()
        llm.generate.return_value = (
            '{"root_cause": "oom", "evidence": [], '
            '"suggested_fix": "raise limit", "severity": "high"}'
        )
        from src.config import Settings

        orch = IncidentOrchestrator(
            k8s_tools=k8s,
            prometheus_tools=prom,
            logs_tools=logs_tools,
            llm_client=llm,
            settings=Settings(allowed_namespaces=["default"], _env_file=None),
            rag=rag,  # type: ignore[arg-type]
        )
        return orch, llm

    def test_runbooks_added_to_llm_context(self, rag_dir: Path):
        rag = RunbookRAG(rag_dir)
        orch, llm = self._make_orch(rag)
        result = orch.diagnose(
            {
                "namespace": "default",
                "pod_name": "api-1",
                "description": "Pod OOMKilled memory limit exceeded exit 137",
            }
        )
        assert result["severity"] == "high"
        sent_context = llm.generate.call_args.kwargs["context"]
        assert "relevant_runbooks" in sent_context
        assert any("OOMKilled" in rb for rb in sent_context["relevant_runbooks"])

    def test_no_match_keeps_agent_behaviour(self, rag_dir: Path):
        """Если RAG ничего не нашёл — контекст без relevant_runbooks, как раньше."""
        rag = RunbookRAG(rag_dir, min_score=0.99)  # заведомо высокий порог
        orch, llm = self._make_orch(rag)
        result = orch.diagnose(
            {
                "namespace": "default",
                "pod_name": "api-1",
                "description": "some totally unrelated alert text",
            }
        )
        assert result["severity"] == "high"
        ctx = llm.generate.call_args.kwargs["context"]
        assert "relevant_runbooks" not in ctx

    def test_rag_failure_does_not_break_diagnosis(self, rag_dir: Path):
        """Исключение внутри RAG не роняет диагностику (graceful degradation)."""
        rag = MagicMock()
        rag.find_relevant.side_effect = RuntimeError("index corrupted")
        orch, llm = self._make_orch(rag)
        result = orch.diagnose(
            {
                "namespace": "default",
                "pod_name": "api-1",
                "description": "Pod OOMKilled",
            }
        )
        assert result["root_cause"] == "oom"
        assert "relevant_runbooks" not in result["raw_data"]


class TestExpandedRunbookLibrary:
    """Новые runbook'и (DNS/PVC/Node) должны индексироваться и находится."""

    def test_new_runbooks_are_retrievable(self):
        rag = RunbookRAG()  # реальный docs/runbooks из репозитория
        assert rag.document_count >= 6

        dns = rag.find_relevant(
            "lookup my-service.default.svc.cluster.local: no such host SERVFAIL",
            top_k=2,
        )
        assert any("CoreDNS" in doc for doc in dns)

        pvc = rag.find_relevant(
            "persistentvolumeclaim stuck Pending ProvisioningFailed storageclass not found",
            top_k=2,
        )
        assert any("StorageClass" in doc for doc in pvc)

        node = rag.find_relevant(
            "node NotReady Kubelet stopped posting node status DiskPressure",
            top_k=2,
        )
        assert any("kubelet" in doc.lower() for doc in node)

    def test_config_error_and_eviction_runbooks_retrievable(self):
        """Новые runbook'и (CreateContainerConfigError, Evicted) находятся RAG'ом."""
        rag = RunbookRAG()  # реальный docs/runbooks из репозитория
        assert rag.document_count >= 8

        cfg = rag.find_relevant(
            'container waiting reason: CreateContainerConfigError, Error: secret "db-creds" not found',
            top_k=2,
        )
        assert any("CreateContainerConfigError" in doc for doc in cfg)

        evicted = rag.find_relevant(
            "Pod status Failed reason Evicted: node was low on resource memory, MemoryPressure",
            top_k=2,
        )
        assert any("Evicted" in doc for doc in evicted)


# ----------------------------------------------------------------------
# Hybrid RAG (векторная ветка): SentenceTransformer мокается детерминированным
# «эмбеддером»: синонимичные категории -> ортогональные one-hot векторы,
# чтобы тесты не скачивали модель и оставались воспроизводимыми.
# ----------------------------------------------------------------------
_DIM = 9

# Маркеры подобраны так, чтобы категории были уникальны и для фикстур,
# и для реальной базы docs/runbooks (проверено grep'ом по корпусу).
_CATEGORIES = (
    (("evicted", "low on resource"), 0),
    (("oomkilled", "ram shortage", "working set"), 1),
    (("imagepullbackoff", "errimagepull"), 2),
    (("crashloopbackoff",), 3),
    (("no such host", "coredns"), 4),
    (("storageclass",), 5),
    (("notready",), 6),
    (("createcontainerconfigerror",), 7),
)


def _category(text: str) -> int:
    low = text.lower()
    for keywords, idx in _CATEGORIES:
        if any(k in low for k in keywords):
            return idx
    return 8  # «прочее» — ортогонально всем категориям


def _one_hot(category: int) -> list[float]:
    vec = [0.0] * _DIM
    vec[category] = 1.0
    return vec


class FakeSentenceTransformer:
    """Замена SentenceTransformer: embedding = one-hot категории симптомов."""

    def __init__(self, model_name: str = "fake-model") -> None:
        self.model_name = model_name

    def encode(self, texts, normalize_embeddings: bool = False):  # noqa: ARG002
        return [_one_hot(_category(t)) for t in texts]


@pytest.fixture(autouse=True)
def fake_embedder(monkeypatch: pytest.MonkeyPatch):
    """Все тесты модуля работают с фейковым эмбеддером (без скачивания модели)."""
    import src.agent.rag as rag_module

    monkeypatch.setattr(rag_module, "SentenceTransformer", FakeSentenceTransformer)


class TestHybridRag:
    def test_config_defaults(self) -> None:
        from src.config import Settings

        s = Settings(_env_file=None)
        assert s.enable_hybrid_rag is True
        assert s.embedding_model == "all-MiniLM-L6-v2"

    def test_hybrid_enabled_with_model(self, rag_dir: Path) -> None:
        rag = RunbookRAG(rag_dir)
        assert rag.hybrid_enabled is True

    def test_vector_branch_finds_synonym_missed_by_tfidf(self, rag_dir: Path) -> None:
        """«RAM shortage» нет в словаре TF-IDF runbook'а OOM — выручают эмбеддинги."""
        rag = RunbookRAG(rag_dir)
        query = "process terminated due to RAM shortage"
        assert rag.find_relevant(query, top_k=1)  # гибрид находит
        tfidf_only = RunbookRAG(rag_dir, enable_hybrid=False)
        assert tfidf_only.hybrid_enabled is False
        assert tfidf_only.find_relevant(query, top_k=1) == []  # чистый TF-IDF мимо

    def test_hybrid_score_combines_tfidf_and_cosine(self, rag_dir: Path) -> None:
        """Итоговый скор = 0.5*TF-IDF + 0.5*cosine: оба сигнала влияют на ранжирование."""
        rag = RunbookRAG(rag_dir)
        # Запрос и по терминам TF-IDF, и по смыслу бьёт в OOM-ранбук.
        hits = rag.find_relevant("OOMKilled memory limit exceeded exit code 137", top_k=1)
        assert hits and "OOMKilled" in hits[0]

    def test_disable_hybrid_flag(self, rag_dir: Path) -> None:
        rag = RunbookRAG(rag_dir, enable_hybrid=False)
        assert rag.hybrid_enabled is False
        # TF-IDF продолжает работать.
        assert rag.find_relevant("OOMKilled memory limit", top_k=1)

    def test_model_download_failure_falls_back_to_tfidf(self, rag_dir: Path, monkeypatch):
        """Модель не скачалась (OSError) — graceful fallback на чистый TF-IDF."""
        import src.agent.rag as rag_module

        def boom(model_name: str):
            raise OSError(f"network unreachable while downloading {model_name}")

        monkeypatch.setattr(rag_module, "SentenceTransformer", boom)
        rag = RunbookRAG(rag_dir)
        assert rag.hybrid_enabled is False
        assert rag.find_relevant("OOMKilled memory limit", top_k=1)  # TF-IDF живой

    def test_package_missing_falls_back_to_tfidf(self, rag_dir: Path, monkeypatch):
        """sentence-transformers не установлен (None) — приложение не падает."""
        import src.agent.rag as rag_module

        monkeypatch.setattr(rag_module, "SentenceTransformer", None)
        rag = RunbookRAG(rag_dir)
        assert rag.hybrid_enabled is False
        assert rag.find_relevant("Failed to pull image manifest unknown", top_k=1)

    def test_encode_failure_at_query_time_uses_tfidf(self, rag_dir: Path) -> None:
        """Сбой encode запроса в рантайме -> ответ только по TF-IDF, без падения."""
        rag = RunbookRAG(rag_dir)

        def broken_encode(texts, normalize_embeddings: bool = False):
            raise RuntimeError("embedder exploded")

        rag._embedder.encode = broken_encode
        assert rag.find_relevant("OOMKilled memory limit", top_k=1)

    def test_empty_corpus_does_not_load_model(self, tmp_path: Path, monkeypatch) -> None:
        """Пустая директория runbook'ов: модель не грузим (быстрый оффлайн-старт)."""
        import src.agent.rag as rag_module

        calls = []

        class Spy(FakeSentenceTransformer):
            def __init__(self, model_name: str = "fake-model") -> None:
                calls.append(model_name)
                super().__init__(model_name)

        monkeypatch.setattr(rag_module, "SentenceTransformer", Spy)
        empty = tmp_path / "no-runbooks"
        empty.mkdir()
        rag = RunbookRAG(empty)
        assert calls == []
        assert rag.hybrid_enabled is False
        assert rag.find_relevant("anything") == []

    def test_real_runbooks_still_retrievable_with_hybrid(self) -> None:
        """Гибрид на реальной базе docs/runbooks: OOM и ImagePullBackOff находятся."""
        rag = RunbookRAG()  # реальные docs/runbooks репозитория
        assert rag.hybrid_enabled is True
        oom = rag.find_relevant("OOMKilled exit code 137", top_k=1)
        assert oom and "OOMKilled" in oom[0]
        pull = rag.find_relevant("ErrImagePull: manifest unknown", top_k=1)
        assert pull and "ImagePullBackOff" in pull[0]
