# ai_agent_DevOps

AI-агент для диагностики инцидентов в Kubernetes.

## Что делает агент

- Читает **метрики** из Prometheus (загрузка CPU/RAM, ошибки, задержки).
- Собирает и анализирует **логи** контейнеров и компонентов кластера.
- Проверяет **состояние подов**, деплоев и других ресурсов Kubernetes.
- Формирует гипотезу о причине инцидента и предлагает **RCA** (Root Cause Analysis)
  с рекомендациями для on-call инженера.

## Структура проекта

```
ai_agent_DevOps/
├── README.md
├── requirements.txt
├── .gitignore
├── .env.example
├── src/
│   ├── main.py            # Точка входа FastAPI-приложения
│   ├── config.py          # Загрузка конфигурации из .env
│   ├── agent/
│   │   ├── orchestrator.py  # Оркестрация шагов диагностики (заглушка)
│   │   └── tools/
│   │       ├── k8s_tools.py         # Инструменты Kubernetes (заглушка)
│   │       ├── prometheus_tools.py  # Инструменты Prometheus (заглушка)
│   │       └── logs_tools.py        # Инструменты логов (заглушка)
│   └── utils/
│       └── logger.py      # Базовое логирование
└── tests/
```

## Быстрый старт

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # заполните переменные окружения
uvicorn src.main:app --reload
```

Проверка работоспособности:

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

> Текущая версия — каркас проекта без бизнес-логики агента.
