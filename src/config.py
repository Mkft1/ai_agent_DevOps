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

    # LLM API (OpenAI-совместимый: Qwen/DashScope, OpenAI, vLLM, Ollama...)
    llm_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode"
    llm_model: str = "qwen-max"

    @property
    def llm_api_key(self) -> str | None:
        """Ключ активного LLM-провайдера (Qwen приоритетнее, затем OpenAI)."""
        return self.qwen_api_key or self.openai_api_key


@lru_cache
def get_settings() -> Settings:
    """Возвращает кэшированный экземпляр настроек."""
    return Settings()
