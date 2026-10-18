"""Инструменты Kubernetes: поды, события, деплойменты, узлы.

Использует официальный python-клиент ``kubernetes``. Все методы возвращают
списки plain-dict (удобно для передачи LLM/в JSON) и никогда не роняют
приложение: при ``ApiException`` логируется ошибка и возвращается пустой список.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import kubernetes
from kubernetes.client.exceptions import ApiException

from src.config import get_settings
from src.utils.logger import get_logger
from src.utils.metrics import inc_k8s_api_error

logger = get_logger(__name__)


def _age_seconds(started_at: datetime | None) -> float | None:
    """Возвращает возраст объекта в секундах от момента запуска."""
    if started_at is None:
        return None
    now = datetime.now(timezone.utc)
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    return max(0.0, (now - started_at).total_seconds())


class K8sTools:
    """Клиент Kubernetes для диагностики инцидентов.

    Инициализируется путём к kubeconfig из настроек приложения
    (``Settings.kubeconfig_path``, переменная окружения ``KUBECONFIG_PATH``).

    Example:
        >>> tools = K8sTools()
        >>> pods = tools.get_pods(namespace="default")
    """

    def __init__(self, kubeconfig_path: str | None = None) -> None:
        """Загружает kubeconfig и создаёт CoreV1Api / AppsV1Api клиенты.

        Graceful degradation: если kubeconfig отсутствует/повреждён — клиенты
        остаются ``None`` (``available is False``), а публичные методы
        возвращают пустые результаты с ошибкой в логах, не роняя приложение.

        Args:
            kubeconfig_path: явный путь к kubeconfig; если не задан —
                берётся из ``get_settings().kubeconfig_path``.
        """
        path = kubeconfig_path or get_settings().kubeconfig_path
        path = os.path.expanduser(path)
        self._core: kubernetes.client.CoreV1Api | None = None
        self._apps: kubernetes.client.AppsV1Api | None = None
        try:
            kubernetes.config.load_kube_config(config_file=path)
        except Exception as exc:  # ConfigException и пр. ошибки чтения файла
            # В кластере kubeconfig-файла нет: пробуем in-cluster config
            # (ServiceAccount + projected token), прежде чем деградировать.
            try:
                kubernetes.config.load_incluster_config()
                logger.info("K8sTools: using in-cluster config (ServiceAccount)")
            except Exception:
                logger.error(
                    "Не удалось загрузить kubeconfig '%s' (%s) и in-cluster "
                    "config — K8s-инструменты переходят в degraded-режим "
                    "(пустые результаты)",
                    path,
                    exc,
                )
                return
        self._core = kubernetes.client.CoreV1Api()
        self._apps = kubernetes.client.AppsV1Api()
        logger.debug("K8sTools инициализирован (kubeconfig: %s)", path)

    @property
    def available(self) -> bool:
        """True, если kubeconfig загружен и клиенты Kubernetes созданы."""
        return self._core is not None

    def _degraded(self, method: str) -> None:
        """Логирует и учитывает метрикой обращение в degraded-режиме."""
        inc_k8s_api_error(method)
        logger.error(
            "K8s client not initialized (no kubeconfig) — %s returns empty result",
            method,
        )

    # ------------------------------------------------------------------
    # Публичные методы
    # ------------------------------------------------------------------
    def get_pods(self, namespace: str = "default") -> list[dict]:
        """Возвращает список подов неймспейса.

        Args:
            namespace: имя Kubernetes-неймспейса.

        Returns:
            Список словарей с полями: name, status, restarts, node, age, labels.
            Пустой список при ошибке API или недоступном kubeconfig.
        """
        if self._core is None:
            self._degraded("get_pods")
            return []
        try:
            pods = self._core.list_namespaced_pod(namespace=namespace).items
        except ApiException as exc:
            inc_k8s_api_error("get_pods")
            logger.error(
                "Ошибка list_namespaced_pod(namespace=%s): status=%s reason=%s",
                namespace,
                exc.status,
                exc.reason,
            )
            return []

        result: list[dict] = []
        for pod in pods:
            status_name = self._pod_phase(pod)
            restarts = sum(
                cs.restart_count or 0 for cs in (pod.status.container_statuses or [])
            )
            result.append(
                {
                    "name": pod.metadata.name,
                    "status": status_name,
                    "restarts": restarts,
                    "node": pod.spec.node_name if pod.spec else None,
                    "age": _age_seconds(pod.status.start_time),
                    "labels": dict(pod.metadata.labels or {}),
                }
            )
        return result

    def get_events(self, namespace: str = "default", limit: int = 50) -> list[dict]:
        """Возвращает последние события в неймспейсе (Warning и Normal).

        Args:
            namespace: имя Kubernetes-неймспейса.
            limit: максимальное количество событий.

        Returns:
            Список словарей с полями: type, reason, message, involved_object,
            count, last_timestamp. Пустой список при ошибке API или
            недоступном kubeconfig.
        """
        if self._core is None:
            self._degraded("get_events")
            return []
        try:
            events = self._core.list_namespaced_event(namespace=namespace).items
        except ApiException as exc:
            inc_k8s_api_error("get_events")
            logger.error(
                "Ошибка list_namespaced_event(namespace=%s): status=%s reason=%s",
                namespace,
                exc.status,
                exc.reason,
            )
            return []

        # Сортируем по времени (последние первыми); None-метки уходят в конец.
        events = sorted(
            events,
            key=lambda e: e.last_timestamp or e.event_time or e.first_timestamp or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )

        result: list[dict] = []
        for event in events[:limit]:
            involved = event.involved_object
            result.append(
                {
                    "type": event.type,
                    "reason": event.reason,
                    "message": event.message,
                    "involved_object": (
                        f"{involved.kind}/{involved.name}" if involved else None
                    ),
                    "count": event.count or 1,
                    "last_timestamp": (
                        event.last_timestamp.isoformat() if event.last_timestamp else None
                    ),
                }
            )
        return result

    def get_deployments(self, namespace: str = "default") -> list[dict]:
        """Возвращает деплойменты неймспейса.

        Args:
            namespace: имя Kubernetes-неймспейса.

        Returns:
            Список словарей с полями: name, replicas, ready_replicas,
            updated_replicas, available_replicas, image.
            Пустой список при ошибке API или недоступном kubeconfig.
        """
        if self._apps is None:
            self._degraded("get_deployments")
            return []
        try:
            deployments = self._apps.list_namespaced_deployment(namespace=namespace).items
        except ApiException as exc:
            inc_k8s_api_error("get_deployments")
            logger.error(
                "Ошибка list_namespaced_deployment(namespace=%s): status=%s reason=%s",
                namespace,
                exc.status,
                exc.reason,
            )
            return []

        result: list[dict] = []
        for dep in deployments:
            containers = dep.spec.template.spec.containers if dep.spec and dep.spec.template else []
            images = [c.image for c in containers if c.image]
            result.append(
                {
                    "name": dep.metadata.name,
                    "replicas": dep.status.replicas if dep.status else None,
                    "ready_replicas": dep.status.ready_replicas if dep.status else None,
                    "updated_replicas": dep.status.updated_replicas if dep.status else None,
                    "available_replicas": dep.status.available_replicas if dep.status else None,
                    "image": images[0] if images else None,
                }
            )
        return result

    def get_nodes(self) -> list[dict]:
        """Возвращает состояние узлов кластера.

        Returns:
            Список словарей с полями: name, status, roles, cpu_capacity,
            memory_capacity, conditions. Пустой список при ошибке API или
            недоступном kubeconfig.
        """
        if self._core is None:
            self._degraded("get_nodes")
            return []
        try:
            nodes = self._core.list_node().items
        except ApiException as exc:
            inc_k8s_api_error("get_nodes")
            logger.error(
                "Ошибка list_node: status=%s reason=%s", exc.status, exc.reason
            )
            return []

        result: list[dict] = []
        for node in nodes:
            conditions = [
                {"type": c.type, "status": c.status}
                for c in (node.status.conditions or [])
            ]
            allocatable = node.status.allocatable or {}
            capacity = node.status.capacity or {}
            result.append(
                {
                    "name": node.metadata.name,
                    "status": self._node_status(conditions),
                    "roles": sorted(
                        k.removeprefix("node-role.kubernetes.io/")
                        for k in (node.metadata.labels or {})
                        if k.startswith("node-role.kubernetes.io/")
                    ),
                    "cpu_capacity": capacity.get("cpu") or allocatable.get("cpu"),
                    "memory_capacity": capacity.get("memory") or allocatable.get("memory"),
                    "conditions": conditions,
                }
            )
        return result

    # ------------------------------------------------------------------
    # Внутренние хелперы
    # ------------------------------------------------------------------
    @staticmethod
    def _pod_phase(pod) -> str:
        """Определяет фазу пода с уточнением по состоянию контейнеров."""
        phase = pod.status.phase or "Unknown"
        if phase == "Running":
            for cs in pod.status.container_statuses or []:
                if cs.state and cs.state.waiting and cs.state.waiting.reason:
                    return cs.state.waiting.reason
                if cs.state and cs.state.terminated and cs.state.terminated.reason:
                    return cs.state.terminated.reason
        return phase

    @staticmethod
    def _node_status(conditions: list[dict]) -> str:
        """Статус узла: Ready, если условие Ready == 'True', иначе NotReady."""
        for cond in conditions:
            if cond["type"] == "Ready":
                return "Ready" if cond["status"] == "True" else "NotReady"
        return "Unknown"
