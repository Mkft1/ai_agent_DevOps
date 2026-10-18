"""Инструменты логов: чтение логов контейнеров через Kubernetes API.

Использует официальный python-клиент ``kubernetes`` (метод
``CoreV1Api.read_namespaced_pod_log``). Путь к kubeconfig берётся из
настроек приложения (``Settings.kubeconfig_path``), как и в ``k8s_tools``.

Все методы никогда не роняют приложение: при ошибках API логируется
ошибка и возвращается пустая строка.
"""

from __future__ import annotations

import os

import kubernetes
from kubernetes.client.exceptions import ApiException

from src.config import get_settings
from src.utils.logger import get_logger

logger = get_logger(__name__)


class LogsTools:
    """Клиент для чтения логов подов через Kubernetes API.

    Example:
        >>> tools = LogsTools()
        >>> text = tools.get_pod_logs(namespace="default", pod_name="web-1")
    """

    def __init__(self, kubeconfig_path: str | None = None) -> None:
        """Загружает kubeconfig и создаёт CoreV1Api клиент.

        Graceful degradation: если kubeconfig отсутствует/повреждён — клиент
        остаётся ``None`` (``available is False``), а публичные методы
        возвращают пустую строку с ошибкой в логах, не роняя приложение.

        Args:
            kubeconfig_path: явный путь к kubeconfig; если не задан —
                берётся из ``get_settings().kubeconfig_path``.
        """
        path = kubeconfig_path or get_settings().kubeconfig_path
        path = os.path.expanduser(path)
        self._core: kubernetes.client.CoreV1Api | None = None
        try:
            kubernetes.config.load_kube_config(config_file=path)
        except Exception as exc:  # ConfigException и пр. ошибки чтения файла
            try:
                kubernetes.config.load_incluster_config()
                logger.info("LogsTools: using in-cluster config (ServiceAccount)")
            except Exception:
                logger.error(
                    "Не удалось загрузить kubeconfig '%s' (%s) и in-cluster "
                    "config — логи подов недоступны (degraded-режим, пустые строки)",
                    path,
                    exc,
                )
                return
        self._core = kubernetes.client.CoreV1Api()
        logger.debug("LogsTools инициализирован (kubeconfig: %s)", path)

    @property
    def available(self) -> bool:
        """True, если kubeconfig загружен и клиент Kubernetes создан."""
        return self._core is not None

    # ------------------------------------------------------------------
    # Публичные методы
    # ------------------------------------------------------------------
    def get_pod_logs(
        self,
        namespace: str,
        pod_name: str,
        tail_lines: int = 100,
        previous: bool = False,
    ) -> str:
        """Возвращает последние ``tail_lines`` строк лога пода.

        Args:
            namespace: Kubernetes-неймспейс.
            pod_name: имя пода.
            tail_lines: количество последних строк (<= 0 означает без ограничения).
            previous: читать лог предыдущего инкарнации контейнера
                (полезно после рестарта/OOMKilled).

        Returns:
            Текст лога; пустая строка при ошибке API, отсутствии логов
            или недоступном kubeconfig.
        """
        if self._core is None:
            logger.error(
                "K8s client not initialized (no kubeconfig) — get_pod_logs "
                "returns empty string (%s/%s)",
                namespace,
                pod_name,
            )
            return ""
        kwargs: dict = {
            "name": pod_name,
            "namespace": namespace,
            "timestamps": True,
        }
        if tail_lines > 0:
            kwargs["tail_lines"] = tail_lines
        if previous:
            kwargs["previous"] = True

        try:
            logs = self._core.read_namespaced_pod_log(**kwargs)
        except ApiException as exc:
            logger.error(
                "Ошибка read_namespaced_pod_log(%s/%s): status=%s reason=%s",
                namespace,
                pod_name,
                exc.status,
                exc.reason,
            )
            return ""
        except Exception as exc:  # сетевые/декодирование ошибок клиента
            logger.error(
                "Непредвиденная ошибка чтения логов пода %s/%s: %s",
                namespace,
                pod_name,
                exc,
            )
            return ""
        return logs or ""

    def get_container_logs(
        self,
        namespace: str,
        pod_name: str,
        container: str,
        tail_lines: int = 100,
        previous: bool = False,
    ) -> str:
        """Возвращает последние ``tail_lines`` строк лога конкретного контейнера.

        Args:
            namespace: Kubernetes-неймспейс.
            pod_name: имя пода.
            container: имя контейнера внутри пода.
            tail_lines: количество последних строк.
            previous: читать лог предыдущей инкарнации контейнера.

        Returns:
            Текст лога; пустая строка при ошибке API или недоступном
            kubeconfig.
        """
        if self._core is None:
            logger.error(
                "K8s client not initialized (no kubeconfig) — get_container_logs "
                "returns empty string (%s/%s:%s)",
                namespace,
                pod_name,
                container,
            )
            return ""
        kwargs: dict = {
            "name": pod_name,
            "namespace": namespace,
            "container": container,
            "timestamps": True,
        }
        if tail_lines > 0:
            kwargs["tail_lines"] = tail_lines
        if previous:
            kwargs["previous"] = True

        try:
            logs = self._core.read_namespaced_pod_log(**kwargs)
        except ApiException as exc:
            logger.error(
                "Ошибка чтения логов контейнера %s/%s:%s: status=%s reason=%s",
                namespace,
                pod_name,
                container,
                exc.status,
                exc.reason,
            )
            return ""
        except Exception as exc:
            logger.error(
                "Непредвиденная ошибка чтения логов контейнера %s/%s:%s: %s",
                namespace,
                pod_name,
                container,
                exc,
            )
            return ""
        return logs or ""

    @staticmethod
    def search_errors(logs: str) -> list[str]:
        """Фильтрует строки лога, похожие на ошибки (эвристика для LLM-контекста).

        Args:
            logs: текст лога.

        Returns:
            Список строк, содержащих error/fatal/exception/oom/traceback
            (без учёта регистра). Пустой список при пустом входе.
        """
        if not logs:
            return []
        markers = ("error", "fatal", "exception", "oom", "traceback", "panic")
        return [line for line in logs.splitlines() if any(m in line.lower() for m in markers)]
