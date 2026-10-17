"""Инструменты Kubernetes: состояние подов, деплоев, событий (заглушки).

Реализация появится на следующих шагах development-цикла.
"""

from src.utils.logger import get_logger

logger = get_logger(__name__)


def get_pod_status(namespace: str = "default") -> list[dict]:
    """Вернуть статус подов в неймспейсе (заглушка)."""
    logger.debug("get_pod_status called (not implemented yet)")
    return []


def get_deployments(namespace: str = "default") -> list[dict]:
    """Вернуть список деплоев (заглушка)."""
    logger.debug("get_deployments called (not implemented yet)")
    return []


def get_recent_events(namespace: str = "default") -> list[dict]:
    """Вернуть последние события кластера (заглушка)."""
    logger.debug("get_recent_events called (not implemented yet)")
    return []
