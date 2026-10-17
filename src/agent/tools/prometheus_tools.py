"""Инструменты Prometheus: instant/range запросы и метрики подов.

Использует HTTP API Prometheus (``/api/v1/query``, ``/api/v1/query_range``)
через библиотеку ``requests``. URL сервера берётся из настроек приложения
(``Settings.prometheus_url``, переменная окружения ``PROMETHEUS_URL``).

Все методы никогда не роняют приложение: при HTTP-ошибках, таймаутах или
некорректном ответе ошибка логируется и возвращается ``None`` / ``{}``.
"""

from __future__ import annotations

import requests

from src.config import get_settings
from src.utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_TIMEOUT_SECONDS = 10.0


class PrometheusTools:
    """Клиент Prometheus HTTP API для диагностики инцидентов.

    Example:
        >>> tools = PrometheusTools()
        >>> data = tools.query("up")
        >>> cpu = tools.get_pod_cpu_usage(namespace="default", pod_name="web-1")
    """

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        """Инициализирует клиент.

        Args:
            base_url: адрес Prometheus (например ``http://prom:9090``);
                если не задан — берётся из ``get_settings().prometheus_url``.
            timeout: таймаут HTTP-запросов в секундах.
        """
        url = (base_url or get_settings().prometheus_url).rstrip("/")
        self.base_url = url
        self.timeout = timeout
        self._session = requests.Session()
        logger.debug("PrometheusTools инициализирован (base_url: %s)", url)

    # ------------------------------------------------------------------
    # Низкоуровневые запросы
    # ------------------------------------------------------------------
    def _get(self, path: str, params: dict) -> dict | None:
        """Выполняет GET к Prometheus API; возвращает JSON ``data`` или None.

        Логирует и проглатывает сетевые ошибки, таймауты, HTTP-статусы != 200
        и статус ``status != "success"`` в теле ответа.
        """
        url = f"{self.base_url}{path}"
        try:
            response = self._session.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
            payload = response.json()
        except requests.exceptions.RequestException as exc:
            logger.error("Ошибка запроса к Prometheus (%s): %s", url, exc)
            return None
        except ValueError as exc:  # невалидный JSON
            logger.error("Некорректный JSON-ответ от Prometheus (%s): %s", url, exc)
            return None

        if payload.get("status") != "success":
            logger.error(
                "Prometheus вернул ошибку: %s", payload.get("error", payload)
            )
            return None
        return payload.get("data")

    def query(self, query: str) -> dict:
        """Выполняет instant-запрос (``/api/v1/query``).

        Args:
            query: PromQL-выражение.

        Returns:
            Словарь вида ``{"resultType": "vector", "result": [{...}]}``,
            где каждый элемент ``result`` — ``{"metric": {...}, "value": [ts, "v"]}``.
            Пустой словарь при ошибке.
        """
        data = self._get("/api/v1/query", {"query": query})
        return data if data is not None else {}

    def query_range(self, query: str, start: str, end: str, step: str = "60s") -> dict:
        """Выполняет range-запрос (``/api/v1/query_range``).

        Args:
            query: PromQL-выражение.
            start: начало диапазона (RFC3339 или unix-время).
            end: конец диапазона.
            step: шаг разрешения (по умолчанию ``"60s"``).

        Returns:
            Словарь вида ``{"resultType": "matrix", "result": [{...}]}``;
            пустой словарь при ошибке.
        """
        data = self._get(
            "/api/v1/query_range",
            {"query": query, "start": start, "end": end, "step": step},
        )
        return data if data is not None else {}

    # ------------------------------------------------------------------
    # Высокоуровневые метрики подов
    # ------------------------------------------------------------------
    def _instant_scalar(self, promql: str) -> float | None:
        """Выполняет instant-запрос и возвращает первое числовое значение."""
        data = self.query(promql)
        result = data.get("result") or []
        if not result:
            logger.warning("Пустой результат запроса: %s", promql)
            return None
        try:
            # value имеет вид [timestamp, "1.23"]
            return float(result[0]["value"][1])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            logger.error("Не удалось распарсить ответ Prometheus (%s): %s", promql, exc)
            return None

    def get_pod_cpu_usage(self, namespace: str, pod_name: str) -> float | None:
        """Текущее потребление CPU подом в ядрах.

        Основано на ``rate(container_cpu_usage_seconds_total[5m])`` —
        производной counter-метрики по времени (секунды CPU в секунду = ядра).

        Args:
            namespace: Kubernetes-неймспейс.
            pod_name: имя пода.

        Returns:
            Потребление CPU в ядрах либо ``None`` при ошибке/отсутствии данных.
        """
        promql = (
            "sum(rate(container_cpu_usage_seconds_total"
            f'{{namespace="{namespace}",pod="{pod_name}",container!=""}}[5m]))'
        )
        return self._instant_scalar(promql)

    def get_pod_memory_usage(self, namespace: str, pod_name: str) -> float | None:
        """Текущее потребление памяти подом в байтах.

        Основано на ``container_memory_working_set_bytes`` (сумма по контейнерам пода).

        Args:
            namespace: Kubernetes-неймспейс.
            pod_name: имя пода.

        Returns:
            Потребление памяти в байтах либо ``None`` при ошибке/отсутствии данных.
        """
        promql = (
            "sum(container_memory_working_set_bytes"
            f'{{namespace="{namespace}",pod="{pod_name}",container!=""}})'
        )
        return self._instant_scalar(promql)
