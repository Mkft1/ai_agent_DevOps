"""Загрузка конфигурации приложения из переменных окружения (.env)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # LLM API-ключи (используется один из провайдеров)
    openai_api_key: str | None = None
    qwen_api_key: str | None = None


@lru_cache
def get_settings() -> Settings:
    """Возвращает кэшированный экземпляр настроек."""
    return Settings()
