"""Клиент LLM API (Qwen / любой OpenAI-совместимый endpoint).

Реализует ``POST {base_url}/v1/chat/completions`` через ``requests``:
таймаут 30 с, ретраи tenacity на 429/5xx (до 3 попыток, экспоненциальная
задержка). Ключ и адрес берутся только из настроек (:mod:`src.config`),
то есть из ``.env`` — ничего не хардкодится.

Метод :meth:`LLMClient.generate` сохраняет обратную совместимость со
старым интерфейсом заглушки ``generate(prompt, context)``: если передан
единый ``prompt``, он используется как user-сообщение.
"""

from __future__ import annotations

import json
from typing import Any

import logging

import requests
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from src.config import get_settings
from src.utils.logger import get_logger

logger = get_logger(__name__)

# Таймаут запроса к LLM API, секунд (по ТЗ).
DEFAULT_TIMEOUT = 30.0


class LLMError(RuntimeError):
    """LLM API недоступен или вернул некорректный ответ после ретраев."""


def _is_retryable(exc: BaseException) -> bool:
    """Ретраим только сетевые ошибки и HTTP 429/5xx; 4xx (кроме 429) — нет."""
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return True
    if isinstance(exc, requests.HTTPError):
        status = getattr(exc.response, "status_code", None)
        return status == 429 or (status is not None and 500 <= status < 600)
    return False


class LLMClient:
    """Клиент OpenAI-совместимого chat API (по умолчанию — Qwen/DashScope).

    Args:
        base_url: базовый адрес API без ``/v1`` (например
            ``https://dashscope.aliyuncs.com/compatible-mode``). По умолчанию
            из ``Settings.llm_base_url`` / env ``LLM_BASE_URL``.
        api_key: секретный ключ — только из env (``QWEN_API_KEY`` /
            ``OPENAI_API_KEY``), в коде не хранится.
        model: имя модели (``qwen-max``, ``gpt-4o-mini`` и т.п.).
        timeout: таймаут HTTP-запроса, сек.
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.llm_base_url).rstrip("/")
        # Qwen предпочтительнее; fallback — OpenAI-совместимый ключ.
        self.api_key = api_key or settings.llm_api_key
        self.model = model or settings.llm_model
        self.timeout = timeout
        if not self.api_key:
            raise LLMError(
                "API-ключ LLM не задан: укажите QWEN_API_KEY или OPENAI_API_KEY в .env"
            )

    # ------------------------------------------------------------------
    # Публичный интерфейс
    # ------------------------------------------------------------------
    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(3),  # максимум 3 попытки
        wait=wait_exponential(multiplier=1, min=1, max=8),  # экспоненциальная задержка
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    def chat(self, system_prompt: str, user_prompt: str) -> str:
        """Отправить сообщение в ``/v1/chat/completions`` и вернуть текст ответа.

        Ретраи (tenacity): до 3 попыток с экспоненциальной задержкой при
        HTTP 429/5xx, ConnectionError и Timeout. Прочие ошибки (401/400/...)
        не ретраятся.

        Args:
            system_prompt: системная роль (установка для модели).
            user_prompt: пользовательское сообщение (контекст инцидента).

        Returns:
            Текст ``choices[0].message.content`` ответа модели.

        Raises:
            LLMError: ошибка не ретраибельна либо API вернул неожиданный формат.
        """
        url = f"{self.base_url}/v1/chat/completions"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        # DEBUG-логирование промтов (без секретов: ключи в заголовках не логируются).
        logger.debug("LLM request: model=%s, system=%.300s", self.model, system_prompt)
        logger.debug("LLM request user_prompt (first 2000 chars): %.2000s", user_prompt)

        try:
            response = requests.post(url, json=payload, headers=headers, timeout=self.timeout)
            response.raise_for_status()
        except requests.HTTPError as exc:
            status = getattr(exc.response, "status_code", "?")
            if not _is_retryable(exc):
                raise LLMError(f"LLM API HTTP {status}: {exc}") from exc
            logger.warning("LLM API HTTP %s, retrying...", status)
            raise
        except (requests.ConnectionError, requests.Timeout) as exc:
            logger.warning("LLM API network error (%s), retrying...", type(exc).__name__)
            raise

        data = response.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"Unexpected LLM response format: {data!r:.300}") from exc

        logger.debug("LLM response (first 2000 chars): %.2000s", content)
        return content

    def generate(
        self,
        prompt: str | None = None,
        context: dict[str, Any] | None = None,
        *,
        system_prompt: str | None = None,
        user_prompt: str | None = None,
    ) -> str:
        """Совместимый со старой заглушкой метод генерации.

        Принимает либо пару ``(system_prompt, user_prompt)``, либо старый
        формат ``(prompt, context)`` — тогда ``prompt`` становится user-сообщением,
        а отдельный ``system_prompt`` подставляется вызывающей стороной.

        Args:
            prompt: единый промт (старый интерфейс заглушки).
            context: структурированный контекст (для DEBUG-лога).
            system_prompt: системная роль (новый интерфейс).
            user_prompt: пользовательское сообщение (новый интерфейс).

        Returns:
            Текст ответа модели.
        """
        sys_msg = system_prompt or ""
        usr_msg = user_prompt if user_prompt is not None else (prompt or "")
        if context is not None:
            logger.debug(
                "LLMClient.generate context keys: %s",
                list(context.keys()),
            )
        return self.chat(system_prompt=sys_msg, user_prompt=usr_msg)

    @staticmethod
    def parse_json_response(raw: str) -> dict:
        """Безопасно извлечь JSON-объект из текста ответа LLM.

        Толерантно к markdown-обёрткам ```json ... ``` и поясняющему тексту
        вокруг объекта.

        Args:
            raw: исходный текст ответа модели.

        Returns:
            Разпарсенный словарь; пустой dict, если JSON не найден/невалиден.
        """
        if not raw:
            return {}
        text = raw.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            logger.warning("LLM response has no JSON object: %.200s", raw)
            return {}
        try:
            data = json.loads(text[start : end + 1])
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError as exc:
            logger.warning("Failed to parse LLM JSON: %s", exc)
            return {}
