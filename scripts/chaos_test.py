#!/usr/bin/env python3
"""Chaos-эксперимент: проверка, что агент диагностирует искусственный сбой.

Сценарий (простой, без Chaos Mesh / Litmus):
    1. Подключается к Kubernetes (kubeconfig из KUBECONFIG_PATH / config.py).
    2. Создаёт pod с несуществующим образом -> ImagePullBackOff в namespace "default".
    3. Ждёт 10 секунд, пока kubelet попробует стянуть образ и событие появится.
    4. Отправляет POST http://localhost:8000/diagnose с описанием инцидента.
    5. Проверяет, что diagnosis.root_cause содержит "image" или "pull" (case-insensitive).
    6. Удаляет созданный pod (idempotent cleanup — даже при ошибке диагностики).

Идемпотентность:
    - Имя pod'а фиксированное (chaos-imagepull-test), перед созданием старый pod
      с таким же именем удаляется (ignore-not-found), поэтому повторный запуск
      не падает с "already exists".
    - Cleanup выполняется в finally, ошибки удаления логируются, но не валят скрипт.

Запуск:
    python scripts/chaos_test.py [--agent-url http://localhost:8000] [--wait 10]

Внимание: это write-операция над кластером (создание/удаление pod'а), но выполняет
её ЧЕЛОВЕК через этот скрипт, а не агент. Сам агент остаётся read-only
(ENABLE_WRITE_ACTIONS=false) и лишь диагностирует последствия.
"""

from __future__ import annotations

import argparse
import sys
import time

import requests
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

# --- Параметры эксперимента ---------------------------------------------------

POD_NAME = "chaos-imagepull-test"
NAMESPACE = "default"
# Заведомо несуществующий образ -> ImagePullBackOff
BAD_IMAGE = "ghcr.io/mkft1/this-image-does-not-exist-404:latest"
LABELS = {"app": POD_NAME, "managed-by": "chaos-test-script"}

DEFAULT_AGENT_URL = "http://localhost:8000"
DEFAULT_WAIT_SECONDS = 10


def load_kube_config() -> None:
    """Загружает kubeconfig: сначала путь из config.py (.env), затем стандартный."""
    try:
        # импортируем локально, чтобы скрипт можно было запускать и без пакета src
        from src.config import get_settings

        kubeconfig_path = get_settings().kubeconfig_path
        config.load_kube_config(config_file=kubeconfig_path)
        print(f"[chaos] kubeconfig загружен: {kubeconfig_path}")
    except Exception as exc:  # noqa: BLE001
        print(f"[chaos] не удалось прочитать config.py ({exc}), пробую стандартный kubeconfig")
        config.load_kube_config()


def delete_pod(core: client.CoreV1Api, name: str, namespace: str) -> None:
    """Удаляет pod, игнорируя 'not found' (идемпотентный cleanup)."""
    try:
        core.delete_namespaced_pod(
            name=name,
            namespace=namespace,
            grace_period_seconds=0,
        )
        print(f"[chaos] pod '{name}' удалён из namespace '{namespace}'")
    except ApiException as exc:
        if exc.status == 404:
            print(f"[chaos] pod '{name}' уже отсутствует (ok)")
        else:
            print(f"[chaos] WARNING: не удалось удалить pod '{name}': {exc.reason}")


def create_chaos_pod(core: client.CoreV1Api, namespace: str) -> None:
    """Создаёт pod с несуществующим образом (предварительно чистит старый)."""
    # Идемпотентность: если pod с таким именем остался от прошлого прогона — удаляем
    delete_pod(core, POD_NAME, namespace)
    _wait_pod_gone(core, POD_NAME, namespace, timeout=30)

    body = client.V1Pod(
        metadata=client.V1ObjectMeta(name=POD_NAME, namespace=namespace, labels=LABELS),
        spec=client.V1PodSpec(
            restart_policy="Never",
            containers=[
                client.V1Container(
                    name="chaos",
                    image=BAD_IMAGE,
                    command=["sleep", "3600"],
                )
            ],
        ),
    )
    core.create_namespaced_pod(namespace=namespace, body=body)
    print(f"[chaos] pod '{POD_NAME}' создан в namespace '{namespace}' с образом '{BAD_IMAGE}'")


def _wait_pod_gone(core: client.CoreV1Api, name: str, namespace: str, timeout: int) -> None:
    """Ждёт фактического исчезновения pod'а (иначе create упрётся в already exists)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            core.read_namespaced_pod(name=name, namespace=namespace)
        except ApiException as exc:
            if exc.status == 404:
                return
            raise
        time.sleep(1)
    print(f"[chaos] WARNING: pod '{name}' всё ещё существует после {timeout}s, пробуем создавать")


def run_diagnosis(agent_url: str, namespace: str, pod_name: str) -> dict:
    """Отправляет POST /diagnose и возвращает JSON-ответ."""
    payload = {
        "namespace": namespace,
        "pod_name": pod_name,
        "description": "Pod in ImagePullBackOff",
    }
    url = f"{agent_url.rstrip('/')}/diagnose"
    print(f"[chaos] POST {url} payload={payload}")
    resp = requests.post(url, json=payload, timeout=120)
    resp.raise_for_status()
    return resp.json()


def check_root_cause(diagnosis: dict) -> bool:
    """root_cause должен содержать 'image' или 'pull' (case-insensitive)."""
    root_cause = str(diagnosis.get("root_cause", "")).lower()
    ok = ("image" in root_cause) or ("pull" in root_cause)
    print(f"[chaos] root_cause: {root_cause!r}")
    print(f"[chaos] проверка keywords ('image'/'pull'): {'PASS' if ok else 'FAIL'}")
    return ok


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Chaos-тест: ImagePullBackOff -> /diagnose")
    parser.add_argument("--agent-url", default=DEFAULT_AGENT_URL, help="URL агента (по умолчанию http://localhost:8000)")
    parser.add_argument("--wait", type=int, default=DEFAULT_WAIT_SECONDS, help="Секунд ожидания после создания pod (по умолчанию 10)")
    parser.add_argument("--namespace", default=NAMESPACE, help="Namespace для эксперимента (по умолчанию default)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    load_kube_config()
    core = client.CoreV1Api()

    created = False
    exit_code = 1
    try:
        create_chaos_pod(core, args.namespace)
        created = True

        print(f"[chaos] жду {args.wait}s, пока kubelet зафиксирует ImagePullBackOff...")
        time.sleep(args.wait)

        # Небольшая страховка: печатаем статус пода для отладки
        try:
            pod = core.read_namespaced_pod(name=POD_NAME, namespace=args.namespace)
            phase = pod.status.phase if pod.status else "Unknown"
            waits = [
                s.state.waiting.reason
                for s in (pod.status.container_statuses or [])
                if s.state and s.state.waiting
            ] if pod.status else []
            print(f"[chaos] состояние пода: phase={phase}, waiting_reasons={waits}")
        except ApiException as exc:
            print(f"[chaos] WARNING: не удалось прочитать статус пода: {exc.reason}")

        result = run_diagnosis(args.agent_url, args.namespace, POD_NAME)
        diagnosis = result.get("diagnosis", {})
        report = result.get("report", "")
        print("[chaos] === отчёт агента ===")
        print(report)
        print("[chaos] ====================")

        if check_root_cause(diagnosis):
            print("[chaos] SUCCESS: агент корректно диагностировал ImagePullBackOff")
            exit_code = 0
        else:
            print("[chaos] FAILURE: root_cause не упоминает проблему с образом")
            exit_code = 1
    except requests.RequestException as exc:
        print(f"[chaos] ERROR: агент недоступен ({exc}). Запущен ли uvicorn src.main:app?")
        exit_code = 1
    except ApiException as exc:
        print(f"[chaos] ERROR: обращение к Kubernetes API: {exc.status} {exc.reason}")
        exit_code = 1
    finally:
        if created:
            delete_pod(core, POD_NAME, args.namespace)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
