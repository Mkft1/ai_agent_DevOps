"""Политики безопасности для рекомендаций агента (role-based guardrails).

Агент read-only и никогда не выполняет изменения сам; однако его
``suggested_fix`` может содержать потенциально опасные команды
(``kubectl delete ...``, ``DROP ...``, ``rm -rf ...``, ``scale 0 ...``).
Такие рекомендации до применения человеком должны пройти явный аппрув
(Slack-уведомление, см. ``src/agent/orchestrator.py``), что фиксируется
в диагнозе полем ``requires_approval``.
"""

from __future__ import annotations

from src.config import Settings, get_settings

# Подстроки опасных команд (регистронезависимо). Расширяйте список аккуратно:
# ложноположительный аппрув лучше ложноотрицательного (fail-safe).
DANGEROUS_PATTERNS: tuple[str, ...] = (
    "delete",
    "drop",
    "rm -rf",
    "scale 0",
)


def check_policy(suggested_fix: str, settings: Settings | None = None) -> bool:
    """Проверить рекомендацию агента на опасные команды.

    Args:
        suggested_fix: текст рекомендации из RCA (поле ``suggested_fix``).
        settings: конфигурация; по умолчанию :func:`~src.config.get_settings`.

    Returns:
        True, если в тексте найдена опасная команда при
        ``ENABLE_WRITE_ACTIONS=false`` (read-only режим) — требуется аппрув
        человека. При ``ENABLE_WRITE_ACTIONS=true`` политика не блокирует
        (write-путь вне спеки агента), возвращает False.
    """
    cfg = settings if settings is not None else get_settings()
    if cfg.enable_write_actions:
        return False
    text = (suggested_fix or "").lower()
    return any(pattern in text for pattern in DANGEROUS_PATTERNS)


__all__ = ["DANGEROUS_PATTERNS", "check_policy"]
