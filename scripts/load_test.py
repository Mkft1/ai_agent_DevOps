"""Locust-сценарии нагрузочного тестирования rate limiting (slowapi).

Проверяют, что ограничения частоты запросов из ``src/main.py`` действительно
работают: ``POST /diagnose`` — 5 запросов/минуту, ``GET /health`` — без лимита.

Запуск (агент должен уже работать на http://localhost:8000):

    make run              # терминал 1: агент (нужен LLM-ключ или LLM_OFFLINE=true)
    make load-test        # терминал 2: locust --headless -u 10 -t 10s

Сценарии:
    test_diagnose_rate_limit
        10 быстрых POST /diagnose с валидным payload: первые 5 обязаны пройти
        лимит (HTTP 200; 422 тоже допустим — ошибка валидации инцидента, но
        НЕ 500), следующие 5 — строго 429 (Too Many Requests). Сразу после
        burst'а выполняется GET /health → 200: приложение не упало и limiter
        не блокирует health checks.
    test_health_unlimited
        20 быстрых GET /health — все обязаны вернуть 200 OK (rate limiting
        к liveness-probe не применяется).

Особенности конструкции:
    * Счётчик slowapi keyed по IP клиента (``key_func=get_remote_address``),
      поэтому при локальном прогоне все пользователи Locust делят ОДНО окно
      лимита (127.0.0.1). Из-за этого ``test_diagnose_rate_limit`` выполняется
      ровно один раз на весь прогон — его забирает первый же заспавненный
      пользователь (через lock); остальные всё это время грузят /health,
      параллельно доказывая, что health check не «залипает» под перегрузкой
      /diagnose.
    * Ожидаемые 429 фиксируются отдельной успешной строкой статистики
      ``POST /diagnose [429 expected]``: в итоговой таблице наглядно видно,
      что 5 из 10 (~50%) запросов /diagnose throttled, при этом exit code
      locust остаётся 0. Любое НЕожидаемое поведение (500; 429 в первой
      пятёрке; не-429 после превышения; не-200 на /health) — настоящий
      failure и ненулевой exit code.
    * Вердикты сценариев дублируются строками ``SCENARIO test_*`` в таблице
      статистики (0 failures = PASS) и записью в лог с разбором кодов.
    * Окно лимита — 60 секунд: повторный запуск не чаще раза в минуту (или
      перезапустите агент), иначе сценарий сообщит об уже исчерпанном окне.
    * Для /diagnose нужен LLM: ключ ``QWEN_API_KEY``/``OPENAI_API_KEY`` или
      ``LLM_OFFLINE=true`` в ``.env`` (stub-режим, быстрые ответы). Без них
      эндпоинт честно возвращает 503 ещё до срабатывания limiter'а
      (FastAPI резолвит зависимость ``get_orchestrator`` раньше тела
      эндпоинта) — сценарий упадёт с подсказкой. С реальным (медленным) LLM
      увеличьте длительность прогона, например ``-t 120s``.
"""

import logging
import threading
import time

from locust import HttpUser, between, task

logger = logging.getLogger("locust.load_test")

# Лимит из src/main.py: @limiter.limit("5/minute") для POST /diagnose.
DIAGNOSE_LIMIT = 5
# Burst: LIMIT «разрешённых» запросов + LIMIT сверх лимита → ровно ~50% 429.
DIAGNOSE_BURST = 10
# Число быстрых GET /health в сценарии проверки отсутствия лимита.
HEALTH_BURST = 20
# Первый /diagnose может быть медленным: холодный старт оркестратора/RAG,
# а с реальным ключом — ещё и ответ LLM.
DIAGNOSE_TIMEOUT = 120

# Валидный payload POST /diagnose (схема — DiagnoseRequest из src/main.py).
VALID_DIAGNOSE_PAYLOAD = {
    "namespace": "default",
    "pod_name": "payments-api-7d9f4b-x2kq",
    "description": "Pod payments-api restarts repeatedly, alert: ContainerOOMKilling",
}

# Одноразовый «claim» сценария rate limit: счётчик лимита общий на всех
# пользователей с одного IP (см. docstring модуля).
_claim_lock = threading.Lock()
_rate_limit_claimed = False


def _claim_rate_limit_scenario() -> bool:
    """Выдать разрешение на выполнение rate-limit-сценария ровно одному пользователю."""
    global _rate_limit_claimed
    with _claim_lock:
        if _rate_limit_claimed:
            return False
        _rate_limit_claimed = True
        return True


def _fire_verdict(user: HttpUser, scenario: str, passed: bool, detail: str, elapsed_ms: float) -> None:
    """Зафиксировать вердикт сценария строкой ``SCENARIO <name>`` в статистике locust."""
    logger.info("%s: %s — %s", scenario, "PASS" if passed else "FAIL", detail)
    user.environment.events.request.fire(
        request_type="SCENARIO",
        name=scenario,
        response_time=elapsed_ms,
        response_length=0,
        exception=None if passed else AssertionError(detail),
        context={"scenario": scenario},
    )


class RateLimitUser(HttpUser):
    """Пользователь Locust: фоновая нагрузка на /health + одноразовый burst на /diagnose."""

    wait_time = between(0.1, 0.5)

    def on_start(self) -> None:
        # Забираем rate-limit-сценарий сразу после spawn'а: burst из 10 запросов
        # должен уложиться в короткое окно -t 10s и в «чистое» 60s-окно лимита.
        self.test_diagnose_rate_limit()

    @task
    def test_diagnose_rate_limit(self) -> None:
        """10 быстрых POST /diagnose: первые 5 → 200/422 (не 500), следующие 5 → строго 429."""
        if not _claim_rate_limit_scenario():
            return  # сценарий одноразовый: burst уже выполнен первым пользователем

        started = time.perf_counter()
        codes: list[int] = []
        for i in range(DIAGNOSE_BURST):
            within_limit = i < DIAGNOSE_LIMIT
            with self.client.post(
                "/diagnose",
                json=VALID_DIAGNOSE_PAYLOAD,
                timeout=DIAGNOSE_TIMEOUT,
                catch_response=True,
            ) as resp:
                code = resp.status_code
                codes.append(code)
                if within_limit and code in (200, 422):
                    # 200 — диагностика построена; 422 — ошибка валидации
                    # инцидента (приложение живо, лимит учитывается).
                    resp.success()
                elif not within_limit and code == 429:
                    # Ожидаемый 429 — в отдельную успешную строку статистики:
                    # в таблице видно ~50% throttled-запросов /diagnose,
                    # а счётчик failures остаётся нулевым.
                    resp.request_meta["name"] = "/diagnose [429 expected]"
                    resp.success()
                elif code == 0:
                    resp.failure(
                        f"запрос #{i + 1}: ошибка соединения — агент запущен на {self.host}?"
                    )
                elif code == 500:
                    resp.failure(f"запрос #{i + 1}: HTTP 500 — приложение упало под нагрузкой")
                elif code == 503:
                    resp.failure(
                        f"запрос #{i + 1}: HTTP 503 — LLM не настроен: запустите агент с "
                        "QWEN_API_KEY/OPENAI_API_KEY или LLM_OFFLINE=true в .env"
                    )
                elif within_limit and code == 429:
                    resp.failure(
                        f"запрос #{i + 1}: HTTP 429 в пределах первых {DIAGNOSE_LIMIT} — "
                        "окно лимита уже исчерпано: подождите 60 с после прошлого "
                        "запуска (или перезапустите агент)"
                    )
                else:
                    expected = "200/422" if within_limit else "429"
                    resp.failure(f"запрос #{i + 1}: HTTP {code} вместо ожидаемого {expected}")

        # После перегрузки приложение обязано остаться живым: /health не
        # блокируется limiter'ом (контекст задачи: «не падает и не блокирует /health»).
        with self.client.get(
            "/health", name="/health (after overload)", catch_response=True
        ) as resp:
            health_ok = resp.status_code == 200
            if health_ok:
                resp.success()
            else:
                resp.failure(
                    f"/health после перегрузки /diagnose: HTTP {resp.status_code} вместо 200"
                )

        elapsed_ms = (time.perf_counter() - started) * 1000
        passed = (
            all(c in (200, 422) for c in codes[:DIAGNOSE_LIMIT])
            and all(c == 429 for c in codes[DIAGNOSE_LIMIT:])
            and health_ok
        )
        n429 = codes.count(429)
        _fire_verdict(
            self,
            "test_diagnose_rate_limit",
            passed,
            f"codes={codes}; 429: {n429}/{len(codes)} "
            f"(ожидалось ровно {DIAGNOSE_BURST - DIAGNOSE_LIMIT}); "
            f"/health после перегрузки: {'200' if health_ok else 'НЕ 200'}",
            elapsed_ms,
        )

    @task(5)
    def test_health_unlimited(self) -> None:
        """20 быстрых GET /health — все 200: rate limiting не применяется к health checks."""
        started = time.perf_counter()
        bad: list[int] = []
        for i in range(HEALTH_BURST):
            with self.client.get("/health", catch_response=True) as resp:
                if resp.status_code == 200:
                    resp.success()
                else:
                    bad.append(resp.status_code)
                    resp.failure(
                        f"/health запрос #{i + 1}: HTTP {resp.status_code} — health check "
                        "обязан всегда отвечать 200 (без лимита)"
                    )
        elapsed_ms = (time.perf_counter() - started) * 1000
        _fire_verdict(
            self,
            "test_health_unlimited",
            not bad,
            f"{HEALTH_BURST - len(bad)}/{HEALTH_BURST} → 200"
            + (f", неожиданные коды: {bad}" if bad else ""),
            elapsed_ms,
        )
