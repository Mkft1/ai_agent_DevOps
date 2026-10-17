"""Инструменты Prometheus: запросы метрик (заглушки).

Реализация появится на следующих шагах development-цикла.
"""

from src.utils.logger import get_logger

logger = get_logger(__name__)


def query_instant(promql: str) -> list[dict]:
    """Выполнить мгновенный PromQL-запрос (заглушка)."""
    logger.debug("query_instant called (not implemented yet): %s", promql)
    return []


def query_range(promql: str, start: str, end: str, step: str = "60s") -> list[dict]:
    """Выполнить диапазонный PromQL-запрос (заглушка)."""
    logger.debug("query_range called (not implemented yet): %s", promql)
    return []
