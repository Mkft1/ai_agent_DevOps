"""RAG по локальным runbook'ам (docs/runbooks/*.md): гибридный поиск.

Retrieval без внешних сервисов (ничего не деплоим, индекс в процессе):
    * TF-IDF (sklearn) — точное совпадение терминов;
    * векторный поиск (sentence-transformers, ``all-MiniLM-L6-v2``) — поиск
      по смыслу (синонимы, перефразирования);
    * гибридный скор = 0.5 * TF-IDF + 0.5 * cosine_similarity(эмбеддингов).

Graceful degradation: если sentence-transformers не установлен или модель не
скачалась (нет сети/диска) — ``RunbookRAG`` молча падает обратно на чистый
TF-IDF (``hybrid_enabled is False``); если нет и sklearn / runbook'ов —
поиск возвращает [] и агент работает без контекста, как раньше.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

try:  # тяжёлая опциональная зависимость: без неё работаем на чистом TF-IDF
    from sentence_transformers import SentenceTransformer
except ImportError:  # pragma: no cover - зависит от окружения деплоя
    SentenceTransformer = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# Директория runbook'ов по умолчанию: <repo>/docs/runbooks
DEFAULT_RUNBOOKS_DIR = Path(__file__).resolve().parents[2] / "docs" / "runbooks"

# Минимальный порог косинусной близости TF-IDF, ниже которого runbook
# считается нерелевантным (шумовой совпадений отдельных слов).
DEFAULT_MIN_SCORE = 0.05

# Веса гибридного ранжирования (TF-IDF + векторный cosine similarity).
HYBRID_TFIDF_WEIGHT = 0.5
HYBRID_VECTOR_WEIGHT = 0.5


class RunbookRAG:
    """Retrieval-Augmented генерация поверх локальных markdown-runbook'ов.

    Attributes:
        runbooks_dir: путь к директории с ``*.md`` файлами.
        min_score: минимальный скор TF-IDF для выдачи runbook'а.
    """

    def __init__(
        self,
        runbooks_dir: str | Path | None = None,
        min_score: float = DEFAULT_MIN_SCORE,
        enable_hybrid: bool | None = None,
        embedding_model: str | None = None,
    ) -> None:
        """Args:
            runbooks_dir: директория с ``*.md``; по умолчанию ``docs/runbooks``.
            min_score: порог итогового (гибридного) скора релевантности.
            enable_hybrid: включить векторную ветку; по умолчанию флаг
                ``ENABLE_HYBRID_RAG`` из конфигурации.
            embedding_model: имя модели sentence-transformers; по умолчанию
                ``EMBEDDING_MODEL`` из конфигурации (``all-MiniLM-L6-v2``).
        """
        from src.config import get_settings  # локально: избежать циклов импорта

        settings = get_settings()
        self.runbooks_dir = Path(runbooks_dir) if runbooks_dir else DEFAULT_RUNBOOKS_DIR
        self.min_score = min_score
        self.enable_hybrid = settings.enable_hybrid_rag if enable_hybrid is None else enable_hybrid
        self.embedding_model = embedding_model or settings.embedding_model
        self._documents: list[tuple[str, str]] = []  # (filename, text)
        self._vectorizer = None
        self._matrix = None
        self._embedder = None
        self._embed_matrix = None  # numpy array (n_docs, dim), нормированный
        self.load_runbooks()

    # ------------------------------------------------------------------
    def load_runbooks(self) -> list[tuple[str, str]]:
        """Прочитать все ``*.md`` из ``runbooks_dir`` и построить TF-IDF индекс.

        Повторный вызов перезагружает индекс. Если директория отсутствует или
        пуста — индекс остаётся пустым, поиск вернёт [] (агент деградирует
        gracefully). Ошибки чтения отдельного файла не роняют загрузку.

        Returns:
            Список ``(имя_файла, текст)`` загруженных документов.
        """
        documents: list[tuple[str, str]] = []
        if not self.runbooks_dir.is_dir():
            logger.warning("Runbooks dir not found: %s", self.runbooks_dir)
            self._documents = []
            self._reset_index()
            return documents

        for path in sorted(self.runbooks_dir.glob("*.md")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                logger.error("Failed to read runbook %s: %s", path, exc)
                continue
            if text.strip():
                documents.append((path.name, text))

        self._documents = documents
        self._build_index(documents)
        self._build_embedding_index(documents)
        logger.debug("Loaded %d runbook(s) from %s", len(documents), self.runbooks_dir)
        return documents

    # ------------------------------------------------------------------
    def find_relevant(self, query: str, top_k: int = 2) -> list[str]:
        """Найти наиболее релевантные runbook'и по тексту запроса.

        Args:
            query: поисковый запрос (обычно статус пода + последние строки
                логов/событий).
            top_k: максимальное число возвращаемых документов.

        Returns:
            Список полных текстов runbook'ов, отсортированных по убыванию
            релевантности. Пустой список — если индекс пуст, запрос пуст или
            ничего не набрал минимум порога близости.
        """
        if not query or not query.strip() or self._matrix is None or top_k <= 0:
            return []
        try:
            scores = self._cosine_scores(query)
        except ValueError as exc:  # например, OOV-only запрос
            logger.debug("RAG query produced no features: %s", exc)
            return []

        vector_scores = self._vector_scores(query)
        if vector_scores is not None:
            # Hybrid search: 0.5 * TF-IDF + 0.5 * cosine similarity эмбеддингов.
            scores = [
                (name, text, HYBRID_TFIDF_WEIGHT * tf + HYBRID_VECTOR_WEIGHT * vs)
                for (name, text, tf), vs in zip(scores, vector_scores)
            ]
            logger.debug("RAG hybrid scoring active (model=%s)", self.embedding_model)

        # Ранжируем по итоговому скору (элемент кортежа #2), НЕ по тексту:
        # сортировка по s[1] (тексту) — исторический баг, из-за которого топ
        # выдачи определялся алфавитом, а не релевантностью.
        ranked = sorted(scores, key=lambda s: s[2], reverse=True)
        results = [text for name, text, score in ranked if score >= self.min_score][:top_k]
        if results:
            logger.debug(
                "RAG matched %d runbook(s): %s",
                len(results),
                [n for n, _, s in ranked[: len(results)] if s >= self.min_score],
            )
        return results

    # ------------------------------------------------------------------
    @property
    def document_count(self) -> int:
        """Число загруженных runbook'ов."""
        return len(self._documents)

    @property
    def hybrid_enabled(self) -> bool:
        """True, если векторная ветка активна (модель загружена и индекс построен)."""
        return self._embedder is not None and self._embed_matrix is not None

    # ------------------------------------------------------------------
    # Векторная (embedding) ветка гибридного поиска
    # ------------------------------------------------------------------
    def _load_embedder(self):
        """Загрузить SentenceTransformer; при любой ошибке — None (fallback TF-IDF)."""
        if not self.enable_hybrid:
            logger.debug("Hybrid RAG disabled by config (ENABLE_HYBRID_RAG=false)")
            return None
        if SentenceTransformer is None:
            logger.error(
                "sentence-transformers is not installed — RAG falls back to pure TF-IDF"
            )
            return None
        try:
            return SentenceTransformer(self.embedding_model)
        except Exception as exc:  # noqa: BLE001 - сеть/диск/кэш модели недоступны
            logger.error(
                "Embedding model %s unavailable (%s) — RAG falls back to pure TF-IDF",
                self.embedding_model,
                exc,
            )
            return None

    def _build_embedding_index(self, documents: list[tuple[str, str]]) -> None:
        """Эмбеддинги всех runbook'ов (нормированный numpy array (n_docs, dim))."""
        self._embedder = None
        self._embed_matrix = None
        if not documents:
            return  # пустой корпус: модель не грузим вовсе (быстрый старт оффлайн)
        embedder = self._load_embedder()
        if embedder is None:
            return
        try:
            import numpy as np

            matrix = np.asarray(
                embedder.encode(
                    [text for _, text in documents], normalize_embeddings=True
                ),
                dtype=float,
            )
        except Exception as exc:  # noqa: BLE001 - сбой encode не роняет агент
            logger.error("Embedding index build failed (%s) — fallback to TF-IDF", exc)
            return
        self._embedder = embedder
        self._embed_matrix = matrix
        logger.info(
            "Hybrid RAG enabled: %d runbook(s) embedded with %s",
            len(documents),
            self.embedding_model,
        )

    def _vector_scores(self, query: str) -> list[float] | None:
        """Косинусные близости запроса к runbook'ам по эмбеддингам (или None)."""
        if self._embedder is None or self._embed_matrix is None:
            return None
        try:
            q_vec = self._embedder.encode([query], normalize_embeddings=True)[0]
            sims = self._embed_matrix @ q_vec  # эмбеддинги нормированы -> cosine
            return [float(x) for x in sims]
        except Exception as exc:  # noqa: BLE001 - сбой encode запроса -> только TF-IDF
            logger.error("Query embedding failed (%s) — using TF-IDF only", exc)
            return None

    def _build_index(self, documents: list[tuple[str, str]]) -> None:
        """Построить TF-IDF матрицу; при недоступности sklearn — оставить пустой."""
        self._reset_index()
        if not documents:
            return
        try:
            from sklearn.feature_extraction.text import TfidfVectorizer
        except ImportError:  # pragma: no cover - зависимость в requirements.txt
            logger.error("scikit-learn is not installed - RAG disabled")
            return
        vectorizer = TfidfVectorizer(
            lowercase=True,
            stop_words="english",
            token_pattern=r"[a-zA-Z0-9_]+",
            sublinear_tf=True,
        )
        try:
            matrix = vectorizer.fit_transform([text for _, text in documents])
        except ValueError as exc:  # пустой/невалидный корпус
            logger.error("TF-IDF index build failed: %s", exc)
            return
        self._vectorizer = vectorizer
        self._matrix = matrix

    def _reset_index(self) -> None:
        self._vectorizer = None
        self._matrix = None

    def _cosine_scores(self, query: str) -> list[tuple[str, str, float]]:
        """Косинусные близости запроса к каждому документу корпуса."""
        assert self._vectorizer is not None and self._matrix is not None
        cleaned = self._normalize_query(query)
        q_vec = self._vectorizer.transform([cleaned])
        sims = (self._matrix @ q_vec.T).toarray().ravel()
        return [
            (name, text, float(sim))
            for (name, text), sim in zip(self._documents, sims)
        ]

    @staticmethod
    def _normalize_query(query: str) -> str:
        """Смягчить «шум» поиска: убрать таймстемпы/хешы Pod'ов.

        Таймстемпы и суффиксы имён подов (``web-7d4f9-x2kqz``) редкие токены,
        которые разбавляют TF-IDF-вектор запроса; заменяем их на стабильные
        маркеры, чтобы матчинился смысл ошибки, а не уникальные идентификаторы.
        """
        text = re.sub(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[^\s]*", " ", query)
        text = re.sub(r"\b[a-f0-9]{8,}\b", " ", text, flags=re.IGNORECASE)
        return text


__all__ = ["RunbookRAG", "DEFAULT_RUNBOOKS_DIR"]
