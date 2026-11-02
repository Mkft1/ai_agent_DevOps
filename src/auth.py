"""OIDC-аутентификация и ролевая авторизация (RBAC) для HTTP API.

Модель безопасности:
    * JWT проверяется ПОЛНОСТЬЮ на стороне API: подпись (асимметричные
      алгоритмы RS/PS/ES/EdDSA по JWKS провайдера), ``iss``, ``aud``, ``exp``.
      Симметричный HS256 сознательно НЕ разрешён — защита от algorithm
      confusion (подмена подписи публичным ключом).
    * Публичные ключи берутся через OIDC Discovery
      (``{issuer}/.well-known/openid-configuration`` -> ``jwks_uri``) и
      кэшируются на :data:`JWKS_CACHE_TTL_SECONDS` (ротация ключей
      подхватывается автоматически после истечения TTL).
    * Fail-closed: любая ошибка проверки токена ИЛИ недоступность JWKS —
      ``401``; недостаточная роль — ``403``.
    * ``OIDC_ENABLED=false`` (по умолчанию) — аутентификация полностью
      отключена, все эндпоинты работают как раньше (обратная совместимость).
    * Логи НЕ содержат токенов и секретов — только типы ошибок и ``sub``.

Роли читаются из claim ``roles`` или ``groups`` (форматы Keycloak, Azure AD,
Okta, Auth0); достаточно совпадения хотя бы с одной ролью из разрешённых
(``ALLOWED_ROLES_DIAGNOSE`` / ``ALLOWED_ROLES_WEBHOOK``).

Вебхук Alertmanager аутентифицируется одним из двух способов:
    1. JWT с ролью из ``ALLOWED_ROLES_WEBHOOK`` (service account);
    2. shared-секрет в заголовке ``X-Webhook-Secret``
       (``WEBHOOK_SHARED_SECRET``; constant-time сравнение через
       :func:`secrets.compare_digest`).

Зависимости FastAPI:
    * :func:`get_current_user` — Bearer JWT из ``Authorization``;
    * :func:`require_role` — фабрика зависимостей с проверкой роли;
    * :func:`require_webhook_auth` — JWT-или-секрет для вебхука.
"""

from __future__ import annotations

import secrets
import time
import warnings
from collections.abc import Callable
from typing import Any

import httpx
from fastapi import Depends, HTTPException, Request

from src.config import Settings, get_settings
from src.utils.logger import get_logger

# authlib.jose при импорте издаёт AuthlibDeprecationWarning (рекомендация
# перейти на joserfc; API совместим до authlib 2.0). Особенность authlib:
# authlib.deprecate сам вставляет фильтр simplefilter("always", ...) —
# поэтому ignore-фильтр ставим ПОСЛЕ импорта authlib.deprecate (первый
# совпавший фильтр побеждает), иначе предупреждение прорвётся в логи/pytest.
with warnings.catch_warnings():
    from authlib.deprecate import AuthlibDeprecationWarning

    warnings.simplefilter("ignore", AuthlibDeprecationWarning)
    from authlib.jose import JoseError, JsonWebKey, JsonWebToken

logger = get_logger(__name__)

# Разрешённые алгоритмы подписи: только асимметричные (OIDC-провайдеры
# подписывают access-токены приватным ключом). HS256 исключён — иначе
# атакующий может "подписать" токен публичным ключом (algorithm confusion).
ALLOWED_ALGORITHMS = [
    "RS256", "RS384", "RS512",
    "PS256", "PS384", "PS512",
    "ES256", "ES384", "ES512",
    "EdDSA",
]

# Собственный экземпляр JsonWebToken с фиксированным списком алгоритмов
# (модульный `jwt`-объект authlib разрешает и симметричные HS*).
_jwt = JsonWebToken(ALLOWED_ALGORITHMS)

# TTL кэша JWKS (сек): баланс между нагрузкой на провайдер и скоростью
# отзыва скомпрометированных/ротированных ключей.
JWKS_CACHE_TTL_SECONDS = 300.0

# Кэш наборов ключей: issuer -> (time.monotonic() загрузки, KeySet).
_JWKS_CACHE: dict[str, tuple[float, Any]] = {}

# Детали ответов намеренно обобщённые: не раскрываем, какое именно claim
# не прошло (минимум информации для атакующего).
UNAUTHORIZED_DETAIL = "Invalid or expired token"
FORBIDDEN_DETAIL = "Insufficient role permissions"

# Заголовок shared-секрета вебхука Alertmanager.
WEBHOOK_SECRET_HEADER = "X-Webhook-Secret"

# Идентичность запроса при выключенном OIDC (обратная совместимость).
ANONYMOUS_USER: dict[str, Any] = {
    "sub": "anonymous",
    "roles": [],
    "auth_method": "none",
}

# Идентичность вебхука при аутентификации shared-секретом.
WEBHOOK_SECRET_USER: dict[str, Any] = {
    "sub": "alertmanager",
    "roles": ["alertmanager"],
    "auth_method": "shared_secret",
}


def _unauthorized() -> HTTPException:
    """401 с заголовком ``WWW-Authenticate: Bearer`` (RFC 6750)."""
    return HTTPException(
        status_code=401,
        detail=UNAUTHORIZED_DETAIL,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _forbidden() -> HTTPException:
    """403: токен валиден, но роли недостаточно для этого эндпоинта."""
    return HTTPException(status_code=403, detail=FORBIDDEN_DETAIL)


def _fetch_json(url: str) -> dict:
    """GET JSON-документа у OIDC-провайдера.

    Вынесено в отдельную функцию: единственная точка выхода в сеть,
    удобно мокать в тестах (``monkeypatch src.auth._fetch_json``).
    """
    response = httpx.get(url, timeout=5.0)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError(f"OIDC document at {url} is not a JSON object")
    return data


def discover_jwks_uri(issuer: str) -> str:
    """OIDC Discovery: ``jwks_uri`` из ``{issuer}/.well-known/openid-configuration``."""
    base = issuer.rstrip("/")
    config = _fetch_json(f"{base}/.well-known/openid-configuration")
    jwks_uri = config.get("jwks_uri")
    if not jwks_uri:
        raise ValueError("OIDC discovery document does not contain 'jwks_uri'")
    return str(jwks_uri)


def get_signing_keys(issuer: str) -> Any:
    """Набор публичных ключей провайдера (authlib KeySet) с TTL-кэшем.

    Кэш per-issuer по :func:`time.monotonic` (не зависит от перевода часов).
    Ошибки сети/парсинга НЕ кэшируются — следующий запрос повторит попытку.
    """
    now = time.monotonic()
    cached = _JWKS_CACHE.get(issuer)
    if cached is not None and (now - cached[0]) < JWKS_CACHE_TTL_SECONDS:
        return cached[1]
    jwks_uri = discover_jwks_uri(issuer)
    key_set = JsonWebKey.import_key_set(_fetch_json(jwks_uri))
    _JWKS_CACHE[issuer] = (now, key_set)
    return key_set


def clear_jwks_cache() -> None:
    """Очистить кэш JWKS (тесты, принудительная ротация ключей)."""
    _JWKS_CACHE.clear()


def verify_jwt_token(token: str, issuer: str, audience: str) -> dict:
    """Проверить JWT: подпись по JWKS провайдера + claims ``iss``/``aud``/``exp``.

    Args:
        token: компактный JWS (``header.payload.signature``).
        issuer: ожидаемый ``iss`` (URL OIDC-провайдера).
        audience: ожидаемый ``aud`` (идентификатор этого API).

    Returns:
        Словарь claims токена (``sub``, ``roles``/``groups``, ...).

    Raises:
        HTTPException 401: токен невалиден/просрочен, issuer/audience не
            совпадают, неизвестный ``kid``, недоступен JWKS (fail-closed).
    """
    try:
        key_set = get_signing_keys(issuer)
    except (httpx.HTTPError, ValueError, KeyError, JoseError) as exc:
        # Fail-closed: провайдер недоступен — запросы не принимаем.
        logger.warning("OIDC: JWKS unavailable for issuer %s: %s: %s",
                       issuer, type(exc).__name__, exc)
        raise _unauthorized() from exc

    claims_options = {
        "iss": {"essential": True, "value": issuer},
        "aud": {"essential": True, "value": audience},
        "exp": {"essential": True},
    }
    try:
        claims = _jwt.decode(token, key_set, claims_options=claims_options)
        claims.validate()
    except (JoseError, ValueError, KeyError) as exc:
        # В логе — только тип/описание ошибки, НИКОГДА сам токен.
        logger.warning("JWT verification failed: %s: %s", type(exc).__name__, exc)
        raise _unauthorized() from exc
    return dict(claims)


def extract_user_roles(claims: dict) -> list[str]:
    """Роли пользователя из JWT-claims: ``roles`` или ``groups``.

    Поддерживаются форматы Keycloak (``groups``/``roles``), Azure AD
    (``roles``), Okta/Auth0 (``groups``); значение — список или строка.
    """
    for claim in ("roles", "groups"):
        value = claims.get(claim)
        if isinstance(value, str):
            return [value]
        if isinstance(value, (list, tuple)):
            return [str(item) for item in value]
    return []


def _extract_bearer_token(request: Request) -> str:
    """Bearer-токен из заголовка ``Authorization``; пусто, если его нет."""
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return token.strip()


async def get_current_user(request: Request) -> dict:
    """FastAPI-зависимость: аутентифицированный пользователь из Bearer JWT.

    При ``OIDC_ENABLED=false`` возвращает :data:`ANONYMOUS_USER` без проверки
    заголовков — полная обратная совместимость. При включённом OIDC:
    отсутствие/некорректность токена -> 401 (fail-closed).
    """
    settings: Settings = get_settings()
    if not settings.oidc_enabled:
        return ANONYMOUS_USER
    token = _extract_bearer_token(request)
    if not token:
        logger.warning("Auth: missing Bearer token for %s", request.url.path)
        raise _unauthorized()
    return verify_jwt_token(token, settings.oidc_issuer_url, settings.oidc_audience)


def require_role(
    required_roles: list[str] | Callable[[], list[str]],
) -> Callable[..., dict]:
    """Фабрика зависимостей: доступ только для перечисленных ролей.

    Args:
        required_roles: список ролей ИЛИ zero-arg callable, возвращающий
            список (позволяет читать актуальную конфигурацию на момент
            запроса, а не импорта модуля).

    Returns:
        Зависимость FastAPI: проверяет JWT (:func:`get_current_user`) и
        наличие хотя бы одной разрешённой роли в claim ``roles``/``groups``;
        иначе 403. Возвращает claims пользователя.
    """

    def role_checker(user: dict = Depends(get_current_user)) -> dict:
        # OIDC выключен — полная обратная совместимость: ролевая проверка
        # не выполняется (get_current_user вернул ANONYMOUS_USER).
        if not get_settings().oidc_enabled:
            return user
        allowed = required_roles() if callable(required_roles) else required_roles
        user_roles = extract_user_roles(user)
        if not any(role in allowed for role in user_roles):
            # Логируем только subject — без токена и списка ролей клиента.
            logger.warning(
                "Auth: subject %r lacks required roles for %s",
                user.get("sub"),
                "protected endpoint",
            )
            raise _forbidden()
        return user

    return role_checker


async def require_webhook_auth(request: Request) -> dict:
    """Аутентификация вебхука Alertmanager: JWT с ролью ИЛИ shared-секрет.

    Порядок проверок (при ``OIDC_ENABLED=true``):
        1. заголовок ``X-Webhook-Secret`` совпал с ``WEBHOOK_SHARED_SECRET``
           (constant-time) -> сервисная идентичность ``alertmanager``;
        2. иначе — обычный Bearer JWT (:func:`get_current_user`) с ролью из
           ``ALLOWED_ROLES_WEBHOOK``; без токена -> 401, не та роль -> 403.

    Alertmanager не умеет подписывать JWT — для него shared-секрет;
    service account с mTLS/OIDC может ходить с токеном.
    """
    settings: Settings = get_settings()
    if not settings.oidc_enabled:
        return ANONYMOUS_USER

    expected = settings.webhook_shared_secret
    provided = request.headers.get(WEBHOOK_SECRET_HEADER)
    if expected and provided is not None:
        # compare_digest на байтах: устойчиво к не-ASCII и не течёт по времени.
        if secrets.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
            return dict(WEBHOOK_SECRET_USER)
        logger.warning("Webhook auth: invalid %s header (subject unknown)",
                       WEBHOOK_SECRET_HEADER)

    user = await get_current_user(request)
    user_roles = extract_user_roles(user)
    if not any(role in settings.allowed_roles_webhook for role in user_roles):
        logger.warning("Webhook auth: subject %r lacks webhook roles", user.get("sub"))
        raise _forbidden()
    return user
