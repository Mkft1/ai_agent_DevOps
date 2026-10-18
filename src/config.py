"""Загрузка конфигурации приложения из переменных окружения (.env)."""

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Допустимые значения LOG_LEVEL (fail-fast при старте, а не в рантайме).
VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
# Формат логов: text — человекочитаемый, json — structured logging (Loki/CloudWatch).
VALID_LOG_FORMATS = ("text", "json")


class Settings(BaseSettings):
    """Настройки приложения, читаются из .env / окружения."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Kubernetes
    kubeconfig_path: str = "~/.kube/config"

    # Prometheus
    prometheus_url: str = "http://localhost:9090"

    # Логирование
    log_level: str = "INFO"
    # Формат логов: text (человекочитаемый) | json (structured logging для Loki/CloudWatch)
    log_format: str = "text"
    # Демо/оффлайн-режим: при отсутствии LLM-ключа использовать LLMClientStub
    # вместо ошибки 503 на /diagnose (для демо, e2e-стендов и оффлайн-разработки).
    llm_offline: bool = False

    # Журнал аудита диагностик (append-only JSON Lines); см. src/utils/audit.py.
    audit_log_path: str = "audit.jsonl"

    # OpenTelemetry: endpoint OTLP-экспортера трейсов (gRPC, напр. http://localhost:4317
    # для локального Jaeger). None (по умолчанию) — экспорт отключён: спаны
    # собираются in-process, но никуда не отправляются (нуль внешних эффектов).
    otel_exporter_otlp_endpoint: str | None = None
    # Имя сервиса в трейсах (service.name в Jaeger/Tempo).
    otel_service_name: str = "ai-agent-devops"

    # Slack incoming webhook для аппрува опасных рекомендаций агента.
    # None (по умолчанию) — уведомление не отправляется, в логах WARNING.
    slack_webhook_url: str | None = None

    # RAG: гибридный поиск (TF-IDF + векторные эмбеддинги sentence-transformers).
    # При недоступности модели агент молча деградирует до чистого TF-IDF.
    enable_hybrid_rag: bool = True
    embedding_model: str = "all-MiniLM-L6-v2"

    # LLM API-ключи (используется один из провайдеров)
    openai_api_key: str | None = None
    qwen_api_key: str | None = None

    # LLM API (OpenAI-совместимый: Qwen/DashScope, OpenAI, vLLM, Ollama...)
    llm_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode"
    llm_model: str = "qwen-max"

    # Guardrails (ограничения безопасности агента)
    allowed_namespaces: list[str] = ["default"]
    max_log_lines: int = 200
    enable_write_actions: bool = False

    # CORS (список разрешённых origins; по умолчанию "*" для dev)
    cors_origins: list[str] = ["*"]

    # Ограничение размера контекста (в символах), передаваемого в LLM
    max_context_chars: int = 16000

    # ------------------------------------------------------------------
    # Валидация конфигурации (fail-fast при старте приложения)
    # ------------------------------------------------------------------
    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        """LOG_LEVEL: только уровня из VALID_LOG_LEVELS (регистронезависимо)."""
        normalized = value.strip().upper()
        if normalized not in VALID_LOG_LEVELS:
            raise ValueError(f"log_level must be one of {VALID_LOG_LEVELS}, got {value!r}")
        return normalized

    @field_validator("log_format")
    @classmethod
    def _validate_log_format(cls, value: str) -> str:
        """LOG_FORMAT: только text | json."""
        normalized = value.strip().lower()
        if normalized not in VALID_LOG_FORMATS:
            raise ValueError(
                f"log_format must be one of {VALID_LOG_FORMATS}, got {value!r}"
            )
        return normalized

    @field_validator("max_log_lines")
    @classmethod
    def _validate_max_log_lines(cls, value: int) -> int:
        """MAX_LOG_LINES: положительное целое (guardrail не может быть <= 0)."""
        if value < 1:
            raise ValueError(f"max_log_lines must be >= 1, got {value}")
        return value

    @field_validator("max_context_chars")
    @classmethod
    def _validate_max_context_chars(cls, value: int) -> int:
        """MAX_CONTEXT_CHARS: не меньше 1000 символов (иначе контекст бесполезен)."""
        if value < 1000:
            raise ValueError(f"max_context_chars must be >= 1000, got {value}")
        return value

    @property
    def llm_api_key(self) -> str | None:
        """Ключ активного LLM-провайдера (Qwen приоритетнее, затем OpenAI)."""
        return self.qwen_api_key or self.openai_api_key


@lru_cache
def get_settings() -> Settings:
    """Возвращает кэшированный экземпляр настроек."""
    return Settings()
