"""Unit-тесты для scripts/chaos_test.py (без реального кластера и агента).

Проверяем чистую логику скрипта с моками:
  - создание пода с "плохим" образом и идемпотентная предварительная очистка;
  - delete_pod игнорирует 404 (повторный запуск не падает);
  - run_diagnosis шлёт корректный POST /diagnose;
  - check_root_cause — case-insensitive поиск 'image'/'pull';
  - main() выполняет cleanup пода даже при ошибке диагностики.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client.exceptions import ApiException

# Импортируем скрипт из scripts/ напрямую (в pytest его каталог не в sys.path)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location("chaos_test", ROOT / "scripts" / "chaos_test.py")
chaos_test = importlib.util.module_from_spec(spec)
spec.loader.exec_module(chaos_test)
sys.modules["chaos_test"] = chaos_test  # чтобы @patch("chaos_test....") находил модуль


def make_api_exception(status: int) -> ApiException:
    exc = ApiException(status=status, reason="error")
    return exc


class TestDeletePod:
    def test_delete_existing_pod(self):
        core = MagicMock()
        chaos_test.delete_pod(core, "p1", "default")
        core.delete_namespaced_pod.assert_called_once_with(
            name="p1", namespace="default", grace_period_seconds=0
        )

    def test_delete_missing_pod_is_ignored(self):
        """Повторный запуск идемпотентен: 404 при удалении не роняет скрипт."""
        core = MagicMock()
        core.delete_namespaced_pod.side_effect = make_api_exception(404)
        chaos_test.delete_pod(core, "p1", "default")  # без исключения

    def test_delete_other_error_is_logged_not_raised(self):
        core = MagicMock()
        core.delete_namespaced_pod.side_effect = make_api_exception(403)
        chaos_test.delete_pod(core, "p1", "default")  # не падаем, только WARNING


class TestCreateChaosPod:
    def test_creates_pod_with_bad_image_after_cleanup(self):
        core = MagicMock()
        core.read_namespaced_pod.side_effect = make_api_exception(404)  # pod уже удалён
        chaos_test.create_chaos_pod(core, "default")

        # сначала идемпотентная очистка старого пода
        core.delete_namespaced_pod.assert_called_once()
        # затем создание с несуществующим образом
        core.create_namespaced_pod.assert_called_once()
        kwargs = core.create_namespaced_pod.call_args.kwargs
        assert kwargs["namespace"] == "default"
        body = kwargs["body"]
        assert body.metadata.name == chaos_test.POD_NAME
        assert body.spec.containers[0].image == chaos_test.BAD_IMAGE


class TestRunDiagnosis:
    @patch("chaos_test.requests.post")
    def test_posts_to_diagnose_endpoint(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"diagnosis": {"root_cause": "Image pull failed"}, "report": "r"}
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp

        result = chaos_test.run_diagnosis("http://localhost:8000/", "default", "chaos-imagepull-test")

        assert result["diagnosis"]["root_cause"] == "Image pull failed"
        args, kwargs = mock_post.call_args
        assert args[0] == "http://localhost:8000/diagnose"  # хвостовой слэш нормализован
        assert kwargs["json"] == {
            "namespace": "default",
            "pod_name": "chaos-imagepull-test",
            "description": "Pod in ImagePullBackOff",
        }


class TestCheckRootCause:
    @pytest.mark.parametrize(
        "root_cause,expected",
        [
            ("The container image cannot be pulled", True),
            ("IMAGEPULLBACKOFF due to invalid tag", True),
            ("Pull back-off detected on node-1", True),
            ("OOMKilled: memory limit exceeded", False),
            ("", False),
        ],
    )
    def test_case_insensitive_keywords(self, root_cause, expected):
        diagnosis = {"root_cause": root_cause} if root_cause else {}
        assert chaos_test.check_root_cause(diagnosis) is expected


class TestMainCleanup:
    def test_main_removes_pod_even_when_agent_fails(self):
        """Идемпотентность/безопасность: cleanup в finally даже при ошибке /diagnose."""
        core = MagicMock()
        core.read_namespaced_pod.side_effect = make_api_exception(404)
        import requests as _requests

        with patch.object(chaos_test, "load_kube_config"), \
             patch("chaos_test.client.CoreV1Api", return_value=core), \
             patch("chaos_test.time.sleep"), \
             patch("chaos_test.run_diagnosis", side_effect=_requests.ConnectionError("agent down")):
            exit_code = chaos_test.main([])

        assert exit_code == 1
        # create был вызван один раз, delete — минимум дважды (предочистка + финальный cleanup)
        core.create_namespaced_pod.assert_called_once()
        assert core.delete_namespaced_pod.call_count >= 2

    def test_main_success_flow(self):
        core = MagicMock()
        core.read_namespaced_pod.side_effect = make_api_exception(404)

        diagnosis_ok = {"diagnosis": {"root_cause": "ImagePullBackOff: image not found"}, "report": "rep"}
        with patch.object(chaos_test, "load_kube_config"), \
             patch("chaos_test.client.CoreV1Api", return_value=core), \
             patch("chaos_test.time.sleep"), \
             patch("chaos_test.run_diagnosis", return_value=diagnosis_ok):
            exit_code = chaos_test.main(["--wait", "0"])

        assert exit_code == 0
        core.delete_namespaced_pod.assert_called()
