#!/usr/bin/env python3
"""Streamlit-дашборд оператора: журнал аудита диагностики + аппрувы.

Запуск (отдельно от FastAPI-сервиса):
    make dashboard            # или: streamlit run ui/dashboard.py

Что показывает:
    * таблицу инцидентов из журнала аудита (``AUDIT_LOG_PATH``, по умолчанию
      ``audit.jsonl``): Timestamp / Namespace / Severity / Root Cause;
    * карточки инцидентов с ``requires_approval=True``: текст ``suggested_fix``
      и кнопка "Approve & Apply" (демо-аппрув: реальных изменений в кластере
      НЕ производит — агент read-only, apply выполняет человек вне дашборда).

Скрипт не импортируется FastAPI-приложением и не влияет на него: только чтение
журнала аудита.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import get_settings  # noqa: E402

# События аудита, которые считаются диагнозами (строки таблицы).
DIAGNOSIS_EVENTS = {"diagnose", "alertmanager_diagnose"}

TABLE_COLUMNS = ["Timestamp", "Namespace", "Severity", "Root Cause"]


def load_audit_events(path: Path) -> list[dict]:
    """Прочитать JSON Lines журнала аудита; битые строки пропускаются.

    Returns:
        Список событий-диагнозов (``diagnose`` / ``alertmanager_diagnose``),
        новые в конце файла остаются новыми (сортировка на уровне таблицы).
        Пустой список, если файла нет или в нём нет пригодных строк.
    """
    path = Path(path)
    if not path.exists():
        return []
    events: list[dict] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue  # полубитая строка при ротации не роняет дашборд
            if record.get("event_type") in DIAGNOSIS_EVENTS:
                events.append(record)
    return events


def build_dataframe(events: list[dict]) -> pd.DataFrame:
    """Таблица инцидентов: Timestamp / Namespace / Severity / Root Cause.

    Свежие инциденты сверху (on-call смотрит начало смены, а не её историю).
    """
    rows = [
        {
            "Timestamp": str(e.get("ts", "")),
            "Namespace": str((e.get("incident") or {}).get("namespace", "n/a")),
            "Severity": str((e.get("result") or {}).get("severity", "unknown")),
            "Root Cause": str((e.get("result") or {}).get("root_cause", "")),
        }
        for e in events
    ]
    df = pd.DataFrame(rows, columns=TABLE_COLUMNS)
    return df.sort_values("Timestamp", ascending=False).reset_index(drop=True)


def approvals_pending(events: list[dict]) -> list[dict]:
    """Инциденты, чьи рекомендации помечены политикой как требующие аппрува."""
    return [
        e
        for e in events
        if bool((e.get("result") or {}).get("requires_approval", False))
    ]


def render_approval_card(index: int, event: dict) -> None:
    """Карточка опасной рекомендации: контекст, suggested_fix, кнопка аппрува."""
    incident = event.get("incident") or {}
    result = event.get("result") or {}
    with st.container(border=True):
        st.markdown(
            f"**🔔 Аппрув требуется** — `{incident.get('namespace', 'n/a')}`"
            + (f" / pod `{incident.get('pod_name')}`" if incident.get("pod_name") else "")
            + f" — {event.get('ts', '')}"
        )
        st.markdown(f"**Root cause:** {result.get('root_cause', '—')}")
        st.markdown("**Suggested fix (опасное действие по политике):**")
        st.code(result.get("suggested_fix", "—"), language="bash")
        warnings = result.get("warnings") or []
        if warnings:
            st.caption("Warnings сбора данных: " + "; ".join(warnings))
        if st.button("Approve & Apply", key=f"approve-{index}-{event.get('ts', '')}"):
            st.session_state[f"approved-{index}-{event.get('ts', '')}"] = True
        if st.session_state.get(f"approved-{index}-{event.get('ts', '')}"):
            st.success(
                "Approval recorded (demo): оператор подтвердил рекомендацию. "
                "Реальных изменений в кластере дашборд не выполняет — apply "
                "делается человеком через kubectl/GitOps после dry-run."
            )


def main() -> None:
    st.set_page_config(page_title="AI DevOps Agent - Incident Dashboard", layout="wide")
    st.title("AI DevOps Agent - Incident Dashboard")

    settings = get_settings()
    audit_path = Path(settings.audit_log_path)

    st.sidebar.markdown(f"**Audit journal:** `{audit_path}`")
    if st.sidebar.button("Refresh"):
        st.cache_data.clear()

    events = load_audit_events(audit_path)
    if not events:
        if not audit_path.exists():
            st.warning(
                f"Audit journal not found at `{audit_path}`. Запустите агент и "
                "выполните несколько диагностик (`POST /diagnose` или Alertmanager "
                "webhook) — записи появятся здесь. Путь настраивается через "
                "`AUDIT_LOG_PATH`."
            )
        else:
            st.info("Audit journal exists but contains no diagnosis events yet.")
        st.stop()

    df = build_dataframe(events)
    st.subheader("Incidents")
    st.dataframe(df, use_container_width=True, hide_index=True)

    severity_counts = df["Severity"].value_counts()
    cols = st.columns(len(severity_counts) or 1)
    for col, (severity, count) in zip(cols, severity_counts.items()):
        col.metric(severity, int(count))

    pending = approvals_pending(events)
    st.subheader(f"Approvals required ({len(pending)})")
    if not pending:
        st.caption("Нет рекомендаций, помеченных политикой как опасные.")
    for i, event in enumerate(pending):
        render_approval_card(i, event)

    with st.expander("Raw audit events"):
        st.json(events)


if __name__ == "__main__":
    main()
