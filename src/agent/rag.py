"""RAG по локальным runbook'ам (docs/runbooks/*.md).

Простой retrieval без внешних сервисов: все markdown-файла читаются в память,
релевантность запросу ранжируется TF-IDF (sklearn.feature_extraction.text).
Используется инкрементально: если runbook'и не найдены (или директория
отсутствует / sklearn недоступен) — агент работает как раньше, без контекста.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# Директория runbook'ов по умолчанию: <repo>/docs/runbooks
DEFAULT_RUNBOOKS_DIR = Path(__file__).resolve().parents[2] / "docs" / "runbooks"

# Минимальный порог косинусной близости TF-IDF, ниже которого runbook
# считается нерелевантным (шумовой совпадений отдельных слов).
DEFAULT_MIN_SCORE = 0.05


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
    ) -> None:
        self.runbooks_dir = Path(runbooks_dir) if runbooks_dir else DEFAULT_RUNBOOKS_DIR
        self.min_score = min_score
        self._documents: list[tuple[str, str]] = []  # (filename, text)
        self._vectorizer = None
        self._matrix = None
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

        ranked = sorted(scores, key=lambda s: s[1], reverse=True)
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
