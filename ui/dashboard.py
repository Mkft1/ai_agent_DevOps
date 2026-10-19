#!/usr/bin/env python3
"""Streamlit-дашборд оператора: журнал аудита диагностики + аппрувы.

Запуск (отдельно от FastAPI-сервиса):
    make dashboard            # или: streamlit run ui/dashboard.py

Экраны:
    * три большие метрики: Total / Critical / Pending Approvals;
    * таблица инцидентов Time | Namespace | Pod | Severity | Root Cause
      с цветовым кодированием severity (🔴/🟠/🟡/🟢) и читаемым временем;
    * карточки аппрува для инцидентов с ``requires_approval=True``:
      root cause, suggested_fix и кнопка "✅ Approve & Apply"
      (демо-подтверждение: кластер дашборд НЕ изменяет);
    * раскрывающийся блок с сырым журналом аудита.

Данные читаются построчно через ``json`` (JSON Lines); ``pd.read_json`` не
используется намеренно — он не предназначен для JSONL. Отсутствие/пустота
файла и битые строки обрабатываются gracefully.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import get_settings  # noqa: E402

# События аудита, которые считаются диагнозами (строки таблицы).
DIAGNOSIS_EVENTS = {"diagnose", "alertmanager_diagnose"}

TABLE_COLUMNS = ["Time", "Namespace", "Pod", "Severity", "Root Cause"]

# Цветовое кодирование severity (on-call считывает статус периферией).
SEVERITY_EMOJI = {
    "critical": "🔴",
    "high": "🟠",
    "medium": "🟡",
    "low": "🟢",
    "info": "ℹ️",
    "unknown": "❓",
}

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def format_timestamp(ts: str) -> str:
    """ISO-8601 -> читаемый вид для дежурного: ``19 Oct, 10:00``.

    Нераспарсиваемые значения возвращаются как есть (журнал не идеален).
    """
    raw = str(ts or "")
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    return f"{dt.day} {_MONTHS[dt.month - 1]}, {dt.hour:02d}:{dt.minute:02d}"


def load_audit_events(path: Path) -> list[dict]:
    """Построчно (модуль ``json``) прочитать JSON Lines журнала аудита.

    Returns:
        Список событий-диагнозов (``diagnose`` / ``alertmanager_diagnose``).
        Пустой список, если файла нет, он пуст или содержит только битые/
        нерелевантные строки — дашборд покажет предупреждение, а не упадёт.
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
                continue  # оборванная строка (ротация/дозапись) не роняет UI
            if not isinstance(record, dict):
                continue
            if record.get("event_type") in DIAGNOSIS_EVENTS:
                events.append(record)
    return events


def build_dataframe(events: list[dict]) -> pd.DataFrame:
    """Таблица инцидентов: Time | Namespace | Pod | Severity | Root Cause.

    Severity подсвечена эмодзи, время в человекочитаемом формате, свежие
    инциденты сверху.
    """
    rows = []
    for e in events:
        incident = e.get("incident") or {}
        result = e.get("result") or {}
        severity = str(result.get("severity", "unknown"))
        rows.append(
            {
                "Time": format_timestamp(e.get("ts")),
                "Namespace": str(incident.get("namespace", "n/a")),
                "Pod": str(incident.get("pod_name") or "—"),
                "Severity": f"{SEVERITY_EMOJI.get(severity, '❓')} {severity}",
                "Root Cause": str(result.get("root_cause", "")),
            }
        )
    df = pd.DataFrame(rows, columns=TABLE_COLUMNS)
    if df.empty:
        return df
    # Сортировка по исходному времени: сырой ts держим во вспомогательной серии.
    order = pd.to_datetime(
        [str(e.get("ts")) for e in events], utc=True, errors="coerce"
    )
    df = df.assign(_sort=order).sort_values("_sort", ascending=False).drop(columns="_sort")
    return df.reset_index(drop=True)


def summarize(events: list[dict]) -> dict:
    """Верхние метрики: всего инцидентов, критических, ожидающих аппрува."""
    return {
        "total": len(events),
        "critical": sum(
            1 for e in events if str((e.get("result") or {}).get("severity")) == "critical"
        ),
        "pending": sum(
            1 for e in events if bool((e.get("result") or {}).get("requires_approval"))
        ),
    }


def approvals_pending(events: list[dict]) -> list[dict]:
    """Инциденты, чьи рекомендации помечены политикой как требующие аппрува."""
    return [
        e
        for e in events
        if bool((e.get("result") or {}).get("requires_approval", False))
    ]


def render_approval_card(index: int, event: dict) -> None:
    """Карточка опасной рекомендации: root cause, suggested fix, кнопка аппрува."""
    incident = event.get("incident") or {}
    result = event.get("result") or {}
    key = f"approve-{index}-{event.get('ts', '')}"
    with st.container(border=True):
        st.markdown(
            f"**🔔 Approval required** — `{incident.get('namespace', 'n/a')}`"
            + (f" / pod `{incident.get('pod_name')}`" if incident.get("pod_name") else "")
            + f" — {format_timestamp(event.get('ts'))}"
        )
        st.markdown(f"**Root cause:** {result.get('root_cause', '—')}")
        st.markdown("**Suggested fix (dangerous action policy):**")
        st.code(result.get("suggested_fix", "—"), language="bash")
        warnings = result.get("warnings") or []
        if warnings:
            st.caption("Data-collection warnings: " + "; ".join(warnings))
        if st.button("✅ Approve & Apply", key=key):
            # Демо-подтверждение: дашборд не изменяет кластер — apply делает
            # человек вне UI (kubectl/GitOps после dry-run).
            st.success("Action approved and applied!")
            st.balloons()


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

    # --- верхние метрики -------------------------------------------------
    summary = summarize(events)
    m1, m2, m3 = st.columns(3)
    m1.metric("Total Incidents", summary["total"])
    m2.metric("Critical Incidents", summary["critical"])
    m3.metric("Pending Approvals", summary["pending"])

    st.sidebar.caption(
        f"events: {summary['total']} | critical: {summary['critical']} | "
        f"pending approvals: {summary['pending']}"
    )

    # --- таблица инцидентов ----------------------------------------------
    st.subheader("Incidents")
    df = build_dataframe(events)
    st.dataframe(
        df,
        width="stretch",
        hide_index=True,
        column_config={
            "Time": st.column_config.TextColumn("Time", width="medium"),
            "Namespace": st.column_config.TextColumn("Namespace", width="small"),
            "Pod": st.column_config.TextColumn("Pod", width="medium"),
            "Severity": st.column_config.TextColumn("Severity", width="small"),
            "Root Cause": st.column_config.TextColumn("Root Cause", width="large"),
        },
    )

    # --- аппрувы ----------------------------------------------------------
    pending = approvals_pending(events)
    st.subheader(f"Approvals required ({len(pending)})")
    if not pending:
        st.caption("Нет рекомендаций, помеченных политикой как опасные.")
    for i, event in enumerate(pending):
        render_approval_card(i, event)

    # --- сырой журнал ------------------------------------------------------
    with st.expander("🔍 View Raw Audit Log", expanded=False):
        st.json(events)


if __name__ == "__main__":
    main()
