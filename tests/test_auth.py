"""Тесты OIDC-аутентификации и RBAC (src/auth.py, wiring в src/main.py, ui/dashboard.py).

Подход:
    * реальные RS256-JWT, подписанные одноразовым RSA-ключом (cryptography);
    * JWKS/Discovery отдаёт фейковый провайдер (monkeypatch ``src.auth._fetch_json``) —
      сетевых обращений нет;
    * HTTP-слой через ``fastapi.testclient.TestClient`` c мок-оркестратором;
    * UI-слой через ``streamlit.testing.v1.AppTest``.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import logging
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.auth import (
    ANONYMOUS_USER,
    FORBIDDEN_DETAIL,
    UNAUTHORIZED_DETAIL,
    WEBHOOK_SECRET_HEADER,
    clear_jwks_cache,
    discover_jwks_uri,
    extract_user_roles,
    get_signing_keys,
    require_role,
    verify_jwt_token,
)
from src.config import Settings
from src.main import app, get_orchestrator

ISSUER = "https://oidc.test/realms/devops"
AUDIENCE = "ai-agent-devops-api"
TEST_KID = "test-key-1"
WEBHOOK_SECRET = "s3cr3t-webhook-value"

UI_PATH = Path(__file__).resolve().parents[1] / "ui" / "dashboard.py"

SAMPLE_DIAGNOSIS = {
    "root_cause": "oom",
    "evidence": [],
    "suggested_fix": "raise limits",
    "severity": "high",
    "raw_data": {},
    "warnings": [],
    "llm_response": "{}",
}


# ----------------------------------------------------------------------
# Крипто-хелперы: RSA-ключи и подписанные JWT (импорт authlib внутри
# функций — модуль уже загружен src/auth c подавленным DeprecationWarning).
# ----------------------------------------------------------------------
@pytest.fixture(scope="module")
def rsa_keys() -> tuple[str, str]:
    """Одноразовая RSA-пара (private_pem, public_pem) на весь модуль."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_pem, public_pem


def public_jwk(public_pem: str, kid: str = TEST_KID) -> dict:
    """Публичный ключ в формате JWK (как отдаёт jwks_uri провайдера)."""
    from authlib.jose import JsonWebKey

    return JsonWebKey.import_key(
        public_pem, {"kid": kid, "use": "sig", "alg": "RS256"}
    ).as_dict()


def sign_token(claims: dict, private_pem: str, kid: str = TEST_KID, alg: str = "RS256") -> str:
    """Подписать JWT приватным ключом (compact JWS)."""
    from authlib.jose import jwt as jose_jwt

    token = jose_jwt.encode({"alg": alg, "kid": kid}, claims, private_pem)
    return token.decode() if isinstance(token, bytes) else str(token)


def make_claims(
    roles: list[str] | None = None,
    sub: str = "sre-user-1",
    exp_offset: int = 3600,
    iss: str = ISSUER,
    aud: str = AUDIENCE,
    with_exp: bool = True,
) -> dict:
    """Claims валидного access-токена (настраиваемые отклонения)."""
    now = int(time.time())
    claims: dict = {"iss": iss, "aud": aud, "sub": sub, "iat": now}
    if with_exp:
        claims["exp"] = now + exp_offset
    if roles is not None:
        claims["roles"] = roles
    return claims


def make_unsigned_jwt(claims: dict) -> str:
    """JWT без настоящей подписи — для клиентского декодирования в UI-тестах."""
    def b64(data: dict) -> str:
        raw = json.dumps(data).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64(claims)}.fake-signature"


# ----------------------------------------------------------------------
# Фикстуры: фейковый OIDC-провайдер, конфиг, клиент, оркестратор.
# ----------------------------------------------------------------------
@pytest.fixture()
def fake_oidc(monkeypatch: pytest.MonkeyPatch, rsa_keys: tuple[str, str]) -> list[str]:
    """Discovery + JWKS через monkeypatch _fetch_json; возвращает журнал URL."""
    _, public_pem = rsa_keys
    jwks = {"keys": [public_jwk(public_pem)]}
    discovery = {
        "issuer": ISSUER,
        "jwks_uri": f"{ISSUER}/protocol/openid-connect/certs",
    }
    calls: list[str] = []

    def fake_fetch(url: str) -> dict:
        calls.append(url)
        if url.endswith("/.well-known/openid-configuration"):
            return discovery
        if url.endswith("/protocol/openid-connect/certs"):
            return jwks
        raise AssertionError(f"unexpected OIDC url: {url}")

    monkeypatch.setattr("src.auth._fetch_json", fake_fetch)
    return calls


@pytest.fixture()
def oidc_enabled(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Включённый OIDC: env + сброс кэша settings + единый источник в main."""
    import src.config as config_module
    import src.main as main_module

    monkeypatch.setenv("OIDC_ENABLED", "true")
    monkeypatch.setenv("OIDC_ISSUER_URL", ISSUER)
    monkeypatch.setenv("OIDC_AUDIENCE", AUDIENCE)
    monkeypatch.delenv("WEBHOOK_SHARED_SECRET", raising=False)
    config_module.get_settings.cache_clear()
    fresh = config_module.get_settings()
    monkeypatch.setattr(main_module, "settings", fresh)
    yield fresh
    config_module.get_settings.cache_clear()


@pytest.fixture()
def oidc_with_secret(oidc_enabled: Settings, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """OIDC + настроенный WEBHOOK_SHARED_SECRET."""
    import src.config as config_module
    import src.main as main_module

    monkeypatch.setenv("WEBHOOK_SHARED_SECRET", WEBHOOK_SECRET)
    config_module.get_settings.cache_clear()
    fresh = config_module.get_settings()
    monkeypatch.setattr(main_module, "settings", fresh)
    yield fresh
    config_module.get_settings.cache_clear()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def mock_orchestrator() -> MagicMock:
    from src.agent.orchestrator import IncidentOrchestrator

    orch = MagicMock(spec=IncidentOrchestrator)
    orch.adiagnose.return_value = dict(SAMPLE_DIAGNOSIS)
    orch.format_report.return_value = "[report]"
    app.dependency_overrides[get_orchestrator] = lambda: orch
    yield orch
    app.dependency_overrides.pop(get_orchestrator, None)


@pytest.fixture(autouse=True)
def _reset_state():
    """Изоляция: кэш JWKS и окна rate limiter очищаются между тестами."""
    from src.main import limiter

    clear_jwks_cache()
    limiter.reset()
    yield
    clear_jwks_cache()
    limiter.reset()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def webhook_payload(ns: str = "default", pod: str = "web-1") -> dict:
    return {
        "version": "4",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": "X", "namespace": ns, "pod": pod},
                "annotations": {"summary": "s"},
                "startsAt": "2026-10-19T00:00:00Z",
            }
        ],
    }


# ----------------------------------------------------------------------
# Конфигурация: дефолты и fail-fast валидация OIDC-блока.
# ----------------------------------------------------------------------
class TestConfigOIDC:
    def test_defaults_are_backward_compatible(self) -> None:
        s = Settings(_env_file=None)
        assert s.oidc_enabled is False
        assert s.oidc_issuer_url == ""
        assert s.oidc_audience == ""
        assert s.allowed_roles_diagnose == ["sre-user", "sre-admin"]
        assert s.allowed_roles_webhook == ["alertmanager", "sre-admin"]
        assert s.webhook_shared_secret is None

    def test_enabled_without_issuer_fails_fast(self) -> None:
        with pytest.raises(ValidationError, match="OIDC_ISSUER_URL"):
            Settings(_env_file=None, oidc_enabled=True)

    def test_enabled_without_audience_fails_fast(self) -> None:
        with pytest.raises(ValidationError, match="OIDC_ISSUER_URL"):
            Settings(_env_file=None, oidc_enabled=True, oidc_issuer_url=ISSUER)

    def test_enabled_without_issuer_but_audience_fails_fast(self) -> None:
        with pytest.raises(ValidationError, match="OIDC_ISSUER_URL"):
            Settings(_env_file=None, oidc_enabled=True, oidc_audience=AUDIENCE)

    def test_enabled_with_full_config_ok(self) -> None:
        s = Settings(
            _env_file=None,
            oidc_enabled=True,
            oidc_issuer_url=ISSUER,
            oidc_audience=AUDIENCE,
        )
        assert s.oidc_enabled is True

    def test_disabled_allows_empty_issuer(self) -> None:
        assert Settings(_env_file=None, oidc_enabled=False).oidc_issuer_url == ""


# ----------------------------------------------------------------------
# verify_jwt_token: подпись + iss/aud/exp; все негативные кейсы -> 401.
# ----------------------------------------------------------------------
class TestVerifyJwtToken:
    def test_valid_token_returns_claims(
        self, fake_oidc, rsa_keys: tuple[str, str]
    ) -> None:
        private_pem, _ = rsa_keys
        token = sign_token(make_claims(roles=["sre-user"]), private_pem)
        claims = verify_jwt_token(token, ISSUER, AUDIENCE)
        assert claims["sub"] == "sre-user-1"
        assert claims["roles"] == ["sre-user"]
        assert claims["iss"] == ISSUER
        assert claims["aud"] == AUDIENCE

    def test_expired_token_rejected(self, fake_oidc, rsa_keys) -> None:
        from fastapi import HTTPException

        private_pem, _ = rsa_keys
        token = sign_token(make_claims(exp_offset=-300), private_pem)
        with pytest.raises(HTTPException) as exc_info:
            verify_jwt_token(token, ISSUER, AUDIENCE)
        assert exc_info.value.status_code == 401
        assert exc_info.value.detail == UNAUTHORIZED_DETAIL

    def test_missing_exp_rejected(self, fake_oidc, rsa_keys) -> None:
        from fastapi import HTTPException

        private_pem, _ = rsa_keys
        token = sign_token(make_claims(with_exp=False), private_pem)
        with pytest.raises(HTTPException) as exc_info:
            verify_jwt_token(token, ISSUER, AUDIENCE)
        assert exc_info.value.status_code == 401

    def test_wrong_audience_rejected(self, fake_oidc, rsa_keys) -> None:
        from fastapi import HTTPException

        private_pem, _ = rsa_keys
        token = sign_token(make_claims(aud="someone-elses-api"), private_pem)
        with pytest.raises(HTTPException) as exc_info:
            verify_jwt_token(token, ISSUER, AUDIENCE)
        assert exc_info.value.status_code == 401

    def test_wrong_issuer_rejected(self, fake_oidc, rsa_keys) -> None:
        from fastapi import HTTPException

        private_pem, _ = rsa_keys
        token = sign_token(make_claims(iss="https://evil.test/realms/x"), private_pem)
        with pytest.raises(HTTPException) as exc_info:
            verify_jwt_token(token, ISSUER, AUDIENCE)
        assert exc_info.value.status_code == 401

    def test_garbage_token_rejected(self, fake_oidc) -> None:
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            verify_jwt_token("not-a-jwt-at-all", ISSUER, AUDIENCE)
        assert exc_info.value.status_code == 401

    def test_tampered_signature_rejected(self, fake_oidc, rsa_keys) -> None:
        from fastapi import HTTPException

        private_pem, _ = rsa_keys
        token = sign_token(make_claims(), private_pem)
        head, payload, signature = token.split(".")
        tampered_sig = ("A" if signature[0] != "A" else "B") + signature[1:]
        with pytest.raises(HTTPException) as exc_info:
            verify_jwt_token(f"{head}.{payload}.{tampered_sig}", ISSUER, AUDIENCE)
        assert exc_info.value.status_code == 401

    def test_hs256_algorithm_confusion_rejected(self, fake_oidc) -> None:
        """HS256-токен не принимается: симметричные алгоритмы запрещены."""
        from fastapi import HTTPException

        hs_token = sign_token(
            make_claims(roles=["sre-admin"]),
            "attacker-known-secret-key",
            alg="HS256",
        )
        with pytest.raises(HTTPException) as exc_info:
            verify_jwt_token(hs_token, ISSUER, AUDIENCE)
        assert exc_info.value.status_code == 401

    def test_unknown_kid_rejected(self, fake_oidc, rsa_keys) -> None:
        from fastapi import HTTPException

        private_pem, _ = rsa_keys
        token = sign_token(make_claims(), private_pem, kid="rotated-away")
        with pytest.raises(HTTPException) as exc_info:
            verify_jwt_token(token, ISSUER, AUDIENCE)
        assert exc_info.value.status_code == 401

    def test_jwks_unreachable_fails_closed(self, monkeypatch) -> None:
        """Провайдер недоступен -> 401 (fail-closed), а не открытый доступ."""
        from fastapi import HTTPException

        def broken_fetch(url: str) -> dict:
            raise httpx.ConnectError("oidc provider down")

        monkeypatch.setattr("src.auth._fetch_json", broken_fetch)
        with pytest.raises(HTTPException) as exc_info:
            verify_jwt_token("whatever.token.value", ISSUER, AUDIENCE)
        assert exc_info.value.status_code == 401

    def test_token_never_appears_in_logs(
        self, fake_oidc, rsa_keys, caplog: pytest.LogCaptureFixture
    ) -> None:
        private_pem, _ = rsa_keys
        expired = sign_token(make_claims(exp_offset=-10), private_pem)
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(Exception):
                verify_jwt_token(expired, ISSUER, AUDIENCE)
        assert expired not in caplog.text
        assert "s3cr3t" not in caplog.text


class TestDiscoveryAndCache:
    def test_discover_jwks_uri(self, fake_oidc) -> None:
        assert discover_jwks_uri(ISSUER) == f"{ISSUER}/protocol/openid-connect/certs"

    def test_discovery_trailing_slash_normalized(self, fake_oidc) -> None:
        uri = discover_jwks_uri(ISSUER + "/")
        assert "//protocol" not in uri

    def test_discovery_without_jwks_uri_raises(self, monkeypatch) -> None:
        monkeypatch.setattr("src.auth._fetch_json", lambda url: {"issuer": ISSUER})
        with pytest.raises(ValueError, match="jwks_uri"):
            discover_jwks_uri(ISSUER)

    def test_fetch_json_rejects_non_dict(self, monkeypatch) -> None:
        from src.auth import _fetch_json

        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = ["not", "a", "dict"]
        monkeypatch.setattr("src.auth.httpx.get", lambda url, timeout: response)
        with pytest.raises(ValueError, match="not a JSON object"):
            _fetch_json("https://oidc.test/doc")

    def test_jwks_cached_between_verifications(
        self, fake_oidc: list[str], rsa_keys
    ) -> None:
        private_pem, _ = rsa_keys
        token = sign_token(make_claims(roles=["sre-user"]), private_pem)
        verify_jwt_token(token, ISSUER, AUDIENCE)
        first_round = len(fake_oidc)
        verify_jwt_token(token, ISSUER, AUDIENCE)
        # Вторая верификация не ходит в сеть: кэш per-issuer.
        assert len(fake_oidc) == first_round
        assert first_round == 2  # discovery + jwks

    def test_clear_cache_forces_refetch(self, fake_oidc: list[str], rsa_keys) -> None:
        private_pem, _ = rsa_keys
        token = sign_token(make_claims(roles=["sre-user"]), private_pem)
        verify_jwt_token(token, ISSUER, AUDIENCE)
        clear_jwks_cache()
        verify_jwt_token(token, ISSUER, AUDIENCE)
        assert len(fake_oidc) == 4  # два полных цикла discovery+jwks

    def test_ttl_expiry_refetches(
        self, fake_oidc: list[str], rsa_keys, monkeypatch
    ) -> None:
        import src.auth as auth_module

        private_pem, _ = rsa_keys
        token = sign_token(make_claims(roles=["sre-user"]), private_pem)
        verify_jwt_token(token, ISSUER, AUDIENCE)
        monkeypatch.setattr(auth_module, "JWKS_CACHE_TTL_SECONDS", 0.0)
        verify_jwt_token(token, ISSUER, AUDIENCE)
        assert len(fake_oidc) == 4

    def test_get_signing_keys_returns_keyset(self, fake_oidc) -> None:
        key_set = get_signing_keys(ISSUER)
        assert key_set.keys


class TestExtractUserRoles:
    def test_roles_claim_list(self) -> None:
        assert extract_user_roles({"roles": ["a", "b"]}) == ["a", "b"]

    def test_groups_claim_fallback(self) -> None:
        assert extract_user_roles({"groups": ["/sre"]}) == ["/sre"]

    def test_roles_claim_takes_precedence(self) -> None:
        assert extract_user_roles({"roles": ["r"], "groups": ["g"]}) == ["r"]

    def test_string_value_normalized(self) -> None:
        assert extract_user_roles({"roles": "solo"}) == ["solo"]

    def test_missing_claims_empty(self) -> None:
        assert extract_user_roles({"sub": "x"}) == []

    def test_non_string_items_stringified(self) -> None:
        assert extract_user_roles({"groups": [1, 2]}) == ["1", "2"]


# ----------------------------------------------------------------------
# HTTP-слой: /diagnose под защитой JWT+роли; пробы открыты всегда.
# ----------------------------------------------------------------------
class TestDiagnoseProtection:
    def test_oidc_disabled_is_backward_compatible(
        self, client: TestClient, mock_orchestrator
    ) -> None:
        """Без OIDC (дефолт) /diagnose работает без Authorization."""
        resp = client.post("/diagnose", json={"namespace": "default", "description": "x"})
        assert resp.status_code == 200

    def test_oidc_enabled_without_token_returns_401(
        self, client: TestClient, oidc_enabled, mock_orchestrator
    ) -> None:
        resp = client.post("/diagnose", json={"namespace": "default", "description": "x"})
        assert resp.status_code == 401
        assert resp.json()["detail"] == UNAUTHORIZED_DETAIL
        assert resp.headers.get("WWW-Authenticate") == "Bearer"

    def test_oidc_enabled_invalid_token_returns_401(
        self, client: TestClient, oidc_enabled, fake_oidc, mock_orchestrator
    ) -> None:
        resp = client.post(
            "/diagnose",
            json={"namespace": "default", "description": "x"},
            headers=bearer("garbage.token.value"),
        )
        assert resp.status_code == 401

    def test_non_bearer_scheme_returns_401(
        self, client: TestClient, oidc_enabled, fake_oidc, mock_orchestrator
    ) -> None:
        resp = client.post(
            "/diagnose",
            json={"namespace": "default", "description": "x"},
            headers={"Authorization": "Basic dXNlcjpwYXNz"},
        )
        assert resp.status_code == 401

    def test_empty_bearer_returns_401(
        self, client: TestClient, oidc_enabled, fake_oidc, mock_orchestrator
    ) -> None:
        resp = client.post(
            "/diagnose",
            json={"namespace": "default", "description": "x"},
            headers={"Authorization": "Bearer "},
        )
        assert resp.status_code == 401

    def test_valid_token_with_allowed_role_returns_200(
        self, client: TestClient, oidc_enabled, fake_oidc, mock_orchestrator, rsa_keys
    ) -> None:
        private_pem, _ = rsa_keys
        token = sign_token(make_claims(roles=["sre-user"]), private_pem)
        resp = client.post(
            "/diagnose",
            json={"namespace": "default", "description": "x"},
            headers=bearer(token),
        )
        assert resp.status_code == 200
        assert resp.json()["diagnosis"]["root_cause"] == "oom"

    def test_valid_token_with_insufficient_role_returns_403(
        self, client: TestClient, oidc_enabled, fake_oidc, mock_orchestrator, rsa_keys
    ) -> None:
        private_pem, _ = rsa_keys
        token = sign_token(make_claims(roles=["readonly-guest"]), private_pem)
        resp = client.post(
            "/diagnose",
            json={"namespace": "default", "description": "x"},
            headers=bearer(token),
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == FORBIDDEN_DETAIL

    def test_token_without_roles_returns_403(
        self, client: TestClient, oidc_enabled, fake_oidc, mock_orchestrator, rsa_keys
    ) -> None:
        private_pem, _ = rsa_keys
        token = sign_token(make_claims(roles=None), private_pem)
        resp = client.post(
            "/diagnose",
            json={"namespace": "default", "description": "x"},
            headers=bearer(token),
        )
        assert resp.status_code == 403

    def test_groups_claim_is_accepted(
        self, client: TestClient, oidc_enabled, fake_oidc, mock_orchestrator, rsa_keys
    ) -> None:
        """Azure AD/Okta-формат: роли в claim `groups`."""
        private_pem, _ = rsa_keys
        claims = make_claims(roles=None)
        claims["groups"] = ["/sre-admin", "/everyone"]
        token = sign_token(claims, private_pem)
        resp = client.post(
            "/diagnose",
            json={"namespace": "default", "description": "x"},
            headers=bearer(token),
        )
        assert resp.status_code == 403  # /sre-admin != sre-admin (строгое сравнение)
        claims["groups"] = ["sre-admin"]
        token = sign_token(claims, private_pem)
        resp = client.post(
            "/diagnose",
            json={"namespace": "default", "description": "x"},
            headers=bearer(token),
        )
        assert resp.status_code == 200

    def test_auth_precedes_orchestrator_503(
        self, client: TestClient, oidc_enabled, fake_oidc, monkeypatch
    ) -> None:
        """Без токена -> 401, а не 503 'LLM not configured' (порядок зависимостей)."""
        import src.main as main_module
        from src.agent import llm_client

        monkeypatch.setattr(
            llm_client,
            "get_settings",
            lambda: Settings(_env_file=None, qwen_api_key=None, openai_api_key=None),
        )
        monkeypatch.setattr(main_module, "_orchestrator", None)
        try:
            resp = client.post("/diagnose", json={"namespace": "default", "description": "x"})
            assert resp.status_code == 401
            assert "LLM is not configured" not in resp.text
        finally:
            main_module._orchestrator = None


class TestOpenEndpoints:
    def test_health_never_protected(
        self, client: TestClient, oidc_enabled, fake_oidc
    ) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_ready_never_protected(
        self, client: TestClient, oidc_enabled, fake_oidc
    ) -> None:
        with patch("src.main._check_kubernetes", return_value="ok"), patch(
            "src.main._check_prometheus", new=AsyncMock(return_value="ok")
        ):
            resp = client.get("/ready")
        assert resp.status_code == 200

    def test_metrics_never_protected(
        self, client: TestClient, oidc_enabled, fake_oidc
    ) -> None:
        resp = client.get("/metrics")
        assert resp.status_code == 200
        assert "http_requests_total" in resp.text


# ----------------------------------------------------------------------
# Вебхук: JWT-или-секрет; обе ветки + негативные кейсы.
# ----------------------------------------------------------------------
class TestWebhookAuth:
    def test_oidc_disabled_webhook_open(
        self, client: TestClient, mock_orchestrator
    ) -> None:
        resp = client.post("/webhook/alertmanager", json=webhook_payload())
        assert resp.status_code == 200

    def test_oidc_enabled_no_credentials_401(
        self, client: TestClient, oidc_enabled, fake_oidc, mock_orchestrator
    ) -> None:
        resp = client.post("/webhook/alertmanager", json=webhook_payload())
        assert resp.status_code == 401

    def test_shared_secret_accepted(
        self, client: TestClient, oidc_with_secret, mock_orchestrator
    ) -> None:
        resp = client.post(
            "/webhook/alertmanager",
            json=webhook_payload(),
            headers={WEBHOOK_SECRET_HEADER: WEBHOOK_SECRET},
        )
        assert resp.status_code == 200
        assert resp.json()["processed"] == 1

    def test_wrong_shared_secret_no_jwt_401(
        self, client: TestClient, oidc_with_secret, fake_oidc, mock_orchestrator
    ) -> None:
        resp = client.post(
            "/webhook/alertmanager",
            json=webhook_payload(),
            headers={WEBHOOK_SECRET_HEADER: "wrong-secret"},
        )
        assert resp.status_code == 401

    def test_jwt_with_webhook_role_accepted(
        self, client: TestClient, oidc_enabled, fake_oidc, mock_orchestrator, rsa_keys
    ) -> None:
        private_pem, _ = rsa_keys
        token = sign_token(make_claims(roles=["alertmanager"], sub="am-sa"), private_pem)
        resp = client.post(
            "/webhook/alertmanager", json=webhook_payload(), headers=bearer(token)
        )
        assert resp.status_code == 200
        assert resp.json()["processed"] == 1

    def test_jwt_with_diagnose_role_forbidden_on_webhook(
        self, client: TestClient, oidc_enabled, fake_oidc, mock_orchestrator, rsa_keys
    ) -> None:
        """sre-user валиден для /diagnose, но НЕ для вебхука -> 403."""
        private_pem, _ = rsa_keys
        token = sign_token(make_claims(roles=["sre-user"]), private_pem)
        resp = client.post(
            "/webhook/alertmanager", json=webhook_payload(), headers=bearer(token)
        )
        assert resp.status_code == 403

    def test_secret_not_configured_header_ignored(
        self, client: TestClient, oidc_enabled, fake_oidc, mock_orchestrator
    ) -> None:
        """WEBHOOK_SHARED_SECRET не задан — заголовок не даёт доступ."""
        resp = client.post(
            "/webhook/alertmanager",
            json=webhook_payload(),
            headers={WEBHOOK_SECRET_HEADER: "anything"},
        )
        assert resp.status_code == 401

    def test_secret_never_appears_in_logs(
        self,
        client: TestClient,
        oidc_with_secret,
        fake_oidc,
        mock_orchestrator,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.DEBUG):
            client.post(
                "/webhook/alertmanager",
                json=webhook_payload(),
                headers={WEBHOOK_SECRET_HEADER: "wrong-secret-value"},
            )
        assert WEBHOOK_SECRET not in caplog.text
        assert "wrong-secret-value" not in caplog.text


class TestRequireRoleFactory:
    @staticmethod
    def _enabled() -> Settings:
        return Settings(
            _env_file=None,
            oidc_enabled=True,
            oidc_issuer_url=ISSUER,
            oidc_audience=AUDIENCE,
        )

    def test_callable_roles_checked_when_enabled(self) -> None:
        """require_role принимает zero-arg callable (актуальный конфиг)."""
        dependency = require_role(lambda: ["dynamic-role"])
        with patch("src.auth.get_settings", return_value=self._enabled()):
            user = dependency({"sub": "u", "roles": ["dynamic-role"]})
        assert user["sub"] == "u"

    def test_static_roles_list_forbidden(self) -> None:
        from fastapi import HTTPException

        dependency = require_role(["static"])
        with patch("src.auth.get_settings", return_value=self._enabled()):
            with pytest.raises(HTTPException) as exc_info:
                dependency({"sub": "u", "roles": ["other"]})
        assert exc_info.value.status_code == 403

    def test_anonymous_passthrough_when_disabled(self) -> None:
        with patch("src.auth.get_settings", return_value=Settings(_env_file=None)):
            dependency = require_role(["whatever"])
            assert dependency(ANONYMOUS_USER) == ANONYMOUS_USER


# ----------------------------------------------------------------------
# UI: клиентское декодирование JWT и gating кнопки аппрува.
# ----------------------------------------------------------------------
def _load_dashboard():
    spec = importlib.util.spec_from_file_location("ui_dashboard_auth", UI_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dash = _load_dashboard()


class TestDashboardJwtDecode:
    def test_decode_payload_valid(self) -> None:
        token = make_unsigned_jwt({"sub": "u1", "roles": ["sre-admin"]})
        payload = dash.decode_jwt_payload(token)
        assert payload["sub"] == "u1"
        assert payload["roles"] == ["sre-admin"]

    def test_decode_payload_garbage(self) -> None:
        assert dash.decode_jwt_payload("garbage") == {}
        assert dash.decode_jwt_payload("") == {}
        assert dash.decode_jwt_payload("a.!!!not-base64!!!.c") == {}

    def test_decode_payload_non_dict_json(self) -> None:
        raw = base64.urlsafe_b64encode(b"[1,2,3]").rstrip(b"=").decode()
        assert dash.decode_jwt_payload(f"h.{raw}.s") == {}

    def test_decode_roles_from_roles_claim(self) -> None:
        token = make_unsigned_jwt({"sub": "u", "roles": ["sre-admin"]})
        assert dash.decode_jwt_roles(token) == ["sre-admin"]

    def test_decode_roles_from_groups_claim(self) -> None:
        token = make_unsigned_jwt({"sub": "u", "groups": ["devops-admin"]})
        assert dash.decode_jwt_roles(token) == ["devops-admin"]

    def test_decode_roles_absent(self) -> None:
        token = make_unsigned_jwt({"sub": "u"})
        assert dash.decode_jwt_roles(token) == []

    def test_admin_roles_constant(self) -> None:
        assert set(dash.ADMIN_ROLES) == {"sre-admin", "devops-admin"}


@pytest.fixture()
def audit_file(tmp_path: Path) -> Path:
    event = {
        "ts": "2026-10-19T11:00:00Z",
        "event_type": "diagnose",
        "incident": {"namespace": "prod", "pod_name": "web-1", "description": "d"},
        "result": {
            "root_cause": "eviction",
            "severity": "critical",
            "suggested_fix": "kubectl delete pod web-1",
            "warnings": [],
            "requires_approval": True,
        },
    }
    path = tmp_path / "audit.jsonl"
    path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    return path


@pytest.fixture()
def oidc_ui_env(audit_file: Path, monkeypatch: pytest.MonkeyPatch):
    """Env для AppTest: OIDC включён, журнал аудита — фикстура."""
    import src.config

    monkeypatch.setenv("AUDIT_LOG_PATH", str(audit_file))
    monkeypatch.setenv("OIDC_ENABLED", "true")
    monkeypatch.setenv("OIDC_ISSUER_URL", ISSUER)
    monkeypatch.setenv("OIDC_AUDIENCE", AUDIENCE)
    src.config.get_settings.cache_clear()
    yield
    src.config.get_settings.cache_clear()


class TestDashboardLoginUI:
    @staticmethod
    def _run(session: dict | None = None):
        from streamlit.testing.v1 import AppTest

        at = AppTest.from_file(str(UI_PATH), default_timeout=30)
        for key, value in (session or {}).items():
            at.session_state[key] = value
        at.run()
        return at

    def test_login_form_shown_when_oidc_enabled(self, oidc_ui_env) -> None:
        at = self._run()
        assert not at.exception
        # Журнал не показывается до логина:
        assert len(at.dataframe) == 0
        assert len(at.text_input) == 1
        labels = [b.label for b in at.button]
        assert "Sign in" in labels
        assert not any("Approve & Apply" in label for label in labels)

    def test_login_rejects_garbage_token(self, oidc_ui_env) -> None:
        at = self._run()
        at.text_input[0].input("not-a-jwt")
        sign_in = next(b for b in at.button if b.label == "Sign in")
        sign_in.click().run()
        assert not at.exception
        assert any("Invalid JWT" in e.value for e in at.error)
        assert not at.session_state.get("user_token")

    def test_login_accepts_decodable_token(self, oidc_ui_env) -> None:
        token = make_unsigned_jwt({"sub": "admin-1", "roles": ["sre-admin"]})
        at = self._run()
        at.text_input[0].input(token)
        sign_in = next(b for b in at.button if b.label == "Sign in")
        sign_in.click().run()
        assert not at.exception
        assert at.session_state["user_token"] == token

    def test_admin_sees_approve_button(self, oidc_ui_env) -> None:
        token = make_unsigned_jwt({"sub": "admin-1", "roles": ["sre-admin"]})
        at = self._run({"user_token": token})
        assert not at.exception
        approve = [b for b in at.button if "Approve & Apply" in b.label]
        assert len(approve) == 1
        approve[0].click().run()
        assert at.success[0].value == "Action approved and applied!"

    def test_devops_admin_role_also_allowed(self, oidc_ui_env) -> None:
        token = make_unsigned_jwt({"sub": "d-1", "roles": ["devops-admin"]})
        at = self._run({"user_token": token})
        assert [b for b in at.button if "Approve & Apply" in b.label]

    def test_non_admin_cannot_approve(self, oidc_ui_env) -> None:
        token = make_unsigned_jwt({"sub": "user-1", "roles": ["sre-user"]})
        at = self._run({"user_token": token})
        assert not at.exception
        approve = [b for b in at.button if "Approve & Apply" in b.label]
        assert approve == []
        assert any("admin role" in i.value for i in at.info)
        # Карточка и метрики видны — скрыта только кнопка:
        assert len(at.dataframe) == 1

    def test_token_without_roles_cannot_approve(self, oidc_ui_env) -> None:
        token = make_unsigned_jwt({"sub": "user-2"})
        at = self._run({"user_token": token})
        assert [b for b in at.button if "Approve & Apply" in b.label] == []

    def test_sidebar_shows_user_and_logout(self, oidc_ui_env) -> None:
        token = make_unsigned_jwt({"sub": "admin-1", "roles": ["sre-admin"]})
        at = self._run({"user_token": token})
        logout = [b for b in at.button if b.label == "Logout"]
        assert len(logout) == 1
        logout[0].click().run()
        assert not at.session_state.get("user_token")
        # После logout снова экран логина:
        assert len(at.text_input) == 1

    def test_oidc_disabled_no_login_no_user_block(
        self, audit_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OIDC выключен — прежнее поведение: без логина и без gating'а."""
        import src.config

        monkeypatch.setenv("AUDIT_LOG_PATH", str(audit_file))
        monkeypatch.delenv("OIDC_ENABLED", raising=False)
        src.config.get_settings.cache_clear()
        try:
            at = self._run()
            assert not at.exception
            assert len(at.text_input) == 0
            assert [b for b in at.button if b.label == "Logout"] == []
            approve = [b for b in at.button if "Approve & Apply" in b.label]
            assert len(approve) == 1
        finally:
            src.config.get_settings.cache_clear()
