"""Инструменты логов: сбор и анализ логов контейнеров (заглушки).

Реализация появится на следующих шагах development-цикла.
"""

from src.utils.logger import get_logger

logger = get_logger(__name__)


def get_pod_logs(pod_name: str, namespace: str = "default", tail_lines: int = 100) -> str:
    """Вернуть логи пода (заглушка)."""
    logger.debug("get_pod_logs called (not implemented yet): %s/%s", namespace, pod_name)
    return ""


def search_errors(logs: str) -> list[str]:
    """Найти ошибки в логах (заглушка)."""
    logger.debug("search_errors called (not implemented yet)")
    return []
