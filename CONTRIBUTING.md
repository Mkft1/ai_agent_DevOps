# Вклад в проект (Contributing)

Спасибо за интерес к развитию AI-агента диагностики инцидентов в Kubernetes!
Ниже — краткие правила, которых достаточно для первого контрибьюшена.

## Окружение разработки

```bash
python -m venv .venv && source .venv/bin/activate   # требуется Python 3.12+
pip install -r requirements-dev.txt                 # тянет и requirements.txt, плюс pytest/ruff
cp .env.example .env                                 # заполните ключи LLM и пути
```

Реальный кластер для разработки **не обязателен**: все инструменты (K8s, Prometheus,
LLM) в тестах подменяются моками. Для ручных проверок удобно использовать kind/minikube.

## Как запускать тесты и линтер

```bash
ruff check src tests scripts                        # линтер (job "lint" в CI)
python -m pytest --cov=src --cov-report=xml tests/  # тесты с покрытием (job "test")
python -m pytest tests/test_api.py                  # только HTTP-слой
python -m pytest -k "guardrails"                    # точечный прогон по имени
```

Перед отправкой PR убедитесь, что **линтер чистый** и **все тесты зелёные** —
то же самое проверяет GitHub Actions (`.github/workflows/ci.yml`: lint → test → build,
а на push в `main` ещё и integration-test на kind-кластере), который запускается
на каждый push/PR в `main`. Приложение при этом должно стартовать
(`uvicorn src.main:app`, затем `curl localhost:8000/health`).

## Процесс контрибьюшена

1. Форкните репозиторий и создайте ветку от актуального `main`:
   `git checkout -b feat/<короткое-имя>` (или `fix/...`, `chore/...`).
2. Делайте небольшие атомарные коммиты с понятным сообщением
   (`feat:`, `fix:`, `docs:`, `test:`, `chore:`).
3. Добавляйте unit-тесты на новую логику (минимум: успешный путь + путь с ошибкой).
4. Не добавляйте новых зависимостей без обсуждения; секреты — только через `.env`
   (`.env` и `.venv` в `.gitignore`).
5. Запушьте ветку и откройте Pull Request в `main`; опишите *что* и *зачем*,
   приложите вывод тестов.
6. Дождитесь ревью; правки — отдельными коммитами в ту же ветку.

## Правила кода

- Type hints и docstrings на всех публичных функциях/классах.
- Обработка ошибок: внешний сбой инструмента не должен ронять сервис —
  логируйте и деградируйте gracefully (см. `src/agent/tools/*`).
- Никаких write-операций над кластером из кода агента (read-only guardrail,
  см. раздел «Безопасность» в README).
- Конфигурация только через `src/config.py` (pydantic-settings), не хардкод.

## Chaos-эксперименты

`scripts/chaos_test.py` запускается только против тестового кластера
(kind/minikube) и идемпотентен; после работы всегда удаляет созданные ресурсы.
