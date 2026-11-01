"""Политики безопасности для рекомендаций агента (role-based guardrails).

Агент read-only и никогда не выполняет изменения сам; однако его
``suggested_fix`` может содержать потенциально опасные команды. Защита
двухуровневая (fail-safe: ложноположительный аппрув лучше ложноотрицательного):

1. **Уровень паттернов** (:data:`DANGEROUS_PATTERNS`) — регистронезависимые
   regex на деструктивные действия и их синонимы/перефразировки
   (``remove the pod``, ``terminate all instances``, ``set replicas to 0``,
   ``drop table``, ``chmod 777``, ...) — модель не может обойти проверку
   простым перефразированием команды.
2. **Уровень CLI** (:func:`find_unsafe_cli_mention`) — ЛЮБОЕ упоминание
   инструментов управления кластером ``kubectl``/``helm``/``oc`` (OpenShift),
   не являющееся read-only-командой (``get``/``describe``/``logs``), требует
   аппрува автоматически, даже если конкретный паттерн не сработал. Срабатывание
   логируется WARNING-сообщением :data:`CLI_DETECTED_WARNING`.

Любой положительный вердикт фиксируется в диагнозе полем ``requires_approval``
(Slack-уведомление см. ``src/agent/orchestrator.py``) и инкрементирует
Prometheus-счётчик ``policy_approval_required_total``.
"""

from __future__ import annotations

import logging
import re

from src.config import Settings, get_settings
from src.utils.metrics import inc_policy_approval_required

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Уровень 1: паттерны опасных действий.
# Каждая запись — (regex, пояснение опасности): комментарий объясняет, ПОЧЕМУ
# паттерн добавлен (обоснование для security-review и таблица в README).
# Все regex компилируются с re.IGNORECASE — поиск регистронезависимый:
# «DELETE pod», «Delete Pod» и «delete pod» детектируются одинаково.
# ---------------------------------------------------------------------------
_DANGEROUS_PATTERN_DOCS: tuple[tuple[str, str], ...] = (
    # --- Базовые паттерны (исходная версия guardrail) ---
    (
        r"delete",
        "Удаление ресурсов (``kubectl delete pod/pvc/namespace``) — "
        "безвозвратная потеря данных и downtime.",
    ),
    (
        r"drop",
        "``DROP TABLE/DATABASE`` — полное уничтожение данных БД; "
        "также английское 'drop the pod' (удалить под).",
    ),
    (
        r"rm\s+-rf",
        "Рекурсивное принудительное удаление каталогов (``rm -rf``) — "
        "данные на файловой системе не восстанавливаются.",
    ),
    (
        r"scale\s+0",
        "Масштабирование реплик в 0 — полный простой сервиса.",
    ),
    # --- Синонимы/перефразировки удаления (обход наивного substring-поиска) ---
    (
        r"remove\s+(the\s+)?pod",
        "Перефразировка удаления пода: 'remove the pod' / 'remove pod' / "
        "'remove pods' — тот же эффект, что ``kubectl delete pod``.",
    ),
    (
        r"terminate\s+(all\s+)?(instance|pod)",
        "Массовое уничтожение: 'terminate all instances' / 'terminate pod' — "
        "терминология AWS/EC2 и Kubernetes для удаления рабочих нагрузок.",
    ),
    (
        r"kill\s+(the\s+)?pod",
        "'kill the pod' — убийство процесса/пода, потеря in-memory состояния "
        "и незавершённых транзакций.",
    ),
    # --- Обнуление реплик (downtime сервиса) ---
    (
        r"set\s+replicas\s+to\s+(0|zero)",
        "Обнуление реплик естественным языком: 'set replicas to 0/zero' — "
        "эквивалент ``kubectl scale --replicas=0`` (включая словесную форму "
        "из контекста спеки — обход наивного поиска цифры).",
    ),
    (
        r"scale\s+down\s+to\s+(0|zero)",
        "'scale down to 0/zero' — снятие всех реплик, полный простой.",
    ),
    (
        r"replicas\s*[:=]\s*0",
        "Обнуление реплик в YAML-манифесте (``replicas: 0``) или во флаге "
        "kubectl (``--replicas=0``). Надмножество спеки ``replicas:\\s*0``: "
        "покрывает и манифесты, и CLI-флаги.",
    ),
    # --- Применение произвольных манифестов (потенциальный backdoor) ---
    (
        r"kubectl\s+apply\s+-f",
        "Применение произвольного манифеста: privileged-под, hostPath-монтирование, "
        "изменение RBAC/ClusterRoleBinding — классический вектор закрепления "
        "в кластере (prompt injection).",
    ),
    (
        r"kubectl\s+create\s+-f",
        "Создание объектов из произвольного манифеста — те же риски, что "
        "``apply -f`` (Secrets, RBAC, privileged-контейнеры).",
    ),
    # --- Деструктивные операции с базами данных ---
    (
        r"drop\s+table",
        "``DROP TABLE`` — безвозвратное удаление таблицы вместе со схемой и данными.",
    ),
    (
        r"truncate\s+table",
        "``TRUNCATE TABLE`` — мгновенная очистка всей таблицы без возможности "
        "отката в транзакции.",
    ),
    # --- Опасные изменения прав доступа ---
    (
        r"chmod\s+777",
        "Права rwx для всех пользователей/групп — любой процесс (включая "
        "скомпрометированный контейнер) может читать и изменять данные.",
    ),
    (
        r"chown\s+root",
        "Смена владельца на root — эскалация привилегий и поломка приложений, "
        "работающих под непривилегированным пользователем (runAsNonRoot).",
    ),
    # --- Инъекции секретов и системных файлов ---
    (
        r"export\s+.*=\s*.*secret",
        "Инъекция секретов в переменные окружения (``export TOKEN=...secret``) — "
        "утечка через /proc, дочерние процессы и логи.",
    ),
    (
        r"echo\s+.*>\s*/etc/passwd",
        "Перезапись системных файлов (``echo ... > /etc/passwd``) — компрометация "
        "узла и создание учётных записей с root-правами.",
    ),
)

#: Скомпилированные регистронезависимые regex (компиляция — один раз при импорте).
DANGEROUS_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE) for pattern, _ in _DANGEROUS_PATTERN_DOCS
)

# ---------------------------------------------------------------------------
# Уровень 2: упоминания CLI управления кластером (kubectl / helm / oc).
# Обоснование: модель может перефразировать опасное действие так, что regex
# уровня 1 не сработают (prompt injection, новые/редкие субкоманды), НО для
# реального изменения кластера ей в любом случае понадобится CLI-инструмент.
# Поэтому любое упоминание kubectl/helm/oc, не являющееся read-only-командой,
# автоматически требует аппрува человека.
# Read-only-субкоманды (get/describe/logs) исключены: они ничего не меняют
# в кластере (агент сам использует их для диагностики), и аппрув на них
# создал бы поток ложных срабатываний. Всё прочее — fail-safe: аппрув.
# ``oc`` матчится только как отдельное слово (\b), чтобы не задевать
# подстрок вроде "doc"/"container"; ``\S+`` после инструмента — субкоманда.
# ---------------------------------------------------------------------------
CLI_MENTION_PATTERN = re.compile(r"\b(kubectl|helm|oc)\b(?:\s+(\S+))?", re.IGNORECASE)

#: Read-only-субкоманды kubectl/oc/helm, не требующие аппрува.
READONLY_SUBCOMMANDS: frozenset[str] = frozenset({"get", "describe", "logs"})

#: Текст WARNING при срабатывании уровня 2 (зафиксирован спецификацией).
CLI_DETECTED_WARNING = "Policy guardrail: kubectl/helm command detected, requiring approval"

#: Разделители, которые отрезаются от токена субкоманды («get,» -> «get»).
_PUNCTUATION = ".,;:!?()[]"


def find_unsafe_cli_mention(text: str) -> str | None:
    """Найти упоминание kubectl/helm/oc, не являющееся read-only-командой.

    Упоминание без субкоманды (просто ``kubectl``/``helm``/``oc`` в тексте)
    трактуется как потенциально опасное (fail-safe): назначение инструмента
    в рекомендации неизвестно.

    Args:
        text: текст рекомендации (``suggested_fix``).

    Returns:
        Имя инструмента (``kubectl``/``helm``/``oc``) первого небезопасного
        упоминания либо ``None``, если упоминаний нет или все они — read-only
        команды (``get``/``describe``/``logs``).
    """
    for match in CLI_MENTION_PATTERN.finditer(text):
        subcommand = (match.group(2) or "").strip(_PUNCTUATION).lower()
        if subcommand not in READONLY_SUBCOMMANDS:
            return match.group(1).lower()
    return None


def check_policy(suggested_fix: str, settings: Settings | None = None) -> bool:
    """Проверить рекомендацию агента на опасные команды (два уровня защиты).

    Уровень 1 — regex :data:`DANGEROUS_PATTERNS` (опасные действия и их
    синонимы/перефразировки, регистронезависимо). Уровень 2 — любое упоминание
    ``kubectl``/``helm``/``oc``, не являющееся read-only-командой
    (:func:`find_unsafe_cli_mention`); срабатывание уровня 2 дополнительно
    логируется WARNING-сообщением :data:`CLI_DETECTED_WARNING`. Каждый
    положительный вердикт инкрементирует ``policy_approval_required_total``.

    Args:
        suggested_fix: текст рекомендации из RCA (поле ``suggested_fix``).
        settings: конфигурация; по умолчанию :func:`~src.config.get_settings`.

    Returns:
        True, если найден опасный паттерн ИЛИ небезопасное упоминание CLI при
        ``ENABLE_WRITE_ACTIONS=false`` (read-only режим) — требуется аппрув
        человека. При ``ENABLE_WRITE_ACTIONS=true`` политика не блокирует
        (write-путь вне спеки агента), возвращает False.
    """
    cfg = settings if settings is not None else get_settings()
    if cfg.enable_write_actions:
        return False
    text = suggested_fix or ""
    pattern_hit = any(pattern.search(text) for pattern in DANGEROUS_PATTERNS)
    cli_tool = find_unsafe_cli_mention(text)
    if cli_tool is not None:
        # Уровень 2: аппрув требуется, даже если конкретный паттерн не сработал.
        logger.warning(CLI_DETECTED_WARNING)
    if not pattern_hit and cli_tool is None:
        return False
    inc_policy_approval_required()
    return True


__all__ = [
    "CLI_DETECTED_WARNING",
    "CLI_MENTION_PATTERN",
    "DANGEROUS_PATTERNS",
    "READONLY_SUBCOMMANDS",
    "check_policy",
    "find_unsafe_cli_mention",
]
