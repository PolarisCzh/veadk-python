import base64
import json
from dataclasses import dataclass
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from frontend.server.mpa_identity_callback import (
    MpaCallbackTarget,
    MpaIdentityCallbackError,
    MpaRuntimeCredentials,
    RuntimeCallbackResult,
    _call_runtime_callback,
    _parse_runtime_payload,
    _relay_user_pool_callback,
    _runtime_result_response,
    build_user_pool_hosted_callback_url,
    mount_mpa_identity_callback,
    parse_identity_relay_state,
    select_mpa_runtime,
)


def _state(payload: dict[str, object]) -> str:
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode()
    return encoded.rstrip("=")


def _relay_state(*, target: str = "mi-agent1", **target_metadata: object) -> str:
    return _state(
        {
            "request_id": "request-1",
            "provider_id": "provider-1",
            "request_state": _state(
                {"target": target, "type": "esa", **target_metadata}
            ),
        }
    )


@pytest.mark.parametrize("target_metadata", [{}, {"is_debug": "ignored"}])
def test_parse_identity_relay_state_accepts_mpa_target(
    target_metadata: dict[str, str],
) -> None:
    parsed = parse_identity_relay_state(_relay_state(**target_metadata))

    assert parsed is not None
    assert parsed.request_id == "request-1"
    assert parsed.provider_id == "provider-1"
    assert parsed.target == MpaCallbackTarget("mi-agent1")


@pytest.mark.parametrize(
    "state",
    [
        "",
        "not-base64",
        _state({"request_id": "request-1"}),
        _state(
            {
                "request_id": "request-1",
                "provider_id": "provider-1",
                "request_state": "not-base64",
            }
        ),
        _state(
            {
                "request_id": "request-1",
                "provider_id": "provider-1",
                "request_state": _state({"target": "ci-claw1", "is_debug": False}),
            }
        ),
    ],
)
def test_parse_identity_relay_state_rejects_invalid_payloads(state: str) -> None:
    assert parse_identity_relay_state(state) is None


@pytest.mark.parametrize(
    ("connection_type", "expected_path"),
    [
        ("OAuth", "/login/generic_oauth/callback"),
        ("OIDC", "/login/generic_oidc/callback"),
    ],
)
def test_build_user_pool_hosted_callback_url(
    connection_type: str,
    expected_path: str,
) -> None:
    assert (
        build_user_pool_hosted_callback_url(
            "https://pool.example.com/oidc",
            connection_type,
        )
        == f"https://pool.example.com{expected_path}"
    )


@pytest.mark.parametrize(
    ("issuer", "connection_type"),
    [
        ("http://pool.example.com", "OAuth"),
        ("https://127.0.0.1", "OAuth"),
        ("https://pool.exämple.com", "OAuth"),
        ("https://pool.example.com", "SAML"),
    ],
)
def test_build_user_pool_hosted_callback_url_rejects_unsafe_values(
    issuer: str,
    connection_type: str,
) -> None:
    with pytest.raises(ValueError):
        build_user_pool_hosted_callback_url(issuer, connection_type)


def test_select_mpa_runtime_matches_agent_id_only() -> None:
    runtime = SimpleNamespace(
        envs=[
            SimpleNamespace(key="MPA_AGENT_ID", value="mi-agent1"),
            SimpleNamespace(key="MPA_IS_DEBUG_RUNTIME", value="true"),
        ]
    )

    assert select_mpa_runtime(
        [("cn-beijing", runtime)],
        MpaCallbackTarget("mi-agent1"),
    ) == ("cn-beijing", runtime)


@pytest.mark.parametrize("candidates", [[], [("cn-beijing", object())]])
def test_select_mpa_runtime_rejects_missing_match(candidates) -> None:
    with pytest.raises(ValueError, match="not found"):
        select_mpa_runtime(candidates, MpaCallbackTarget("mi-agent1"))


def test_select_mpa_runtime_rejects_duplicate_match() -> None:
    runtime = SimpleNamespace(
        envs=[SimpleNamespace(key="MPA_AGENT_ID", value="mi-agent1")]
    )
    with pytest.raises(ValueError, match="not unique"):
        select_mpa_runtime(
            [("cn-beijing", runtime), ("cn-shanghai", runtime)],
            MpaCallbackTarget("mi-agent1"),
        )


@dataclass
class _Session:
    access_token: str = "studio-access-token"


_DEFAULT_SESSION = _Session()


class _OAuthHandler:
    def __init__(self, session: _Session | None, *, refreshed: bool = False) -> None:
        self.session = session
        self.refreshed = refreshed
        self.config = SimpleNamespace(session_cookie_name="studio_session")

    async def get_or_refresh_session(self, _request):
        return self.session, self.refreshed

    def create_session_cookie(self, _session):
        return {
            "key": "studio_session",
            "value": "refreshed-session",
            "httponly": True,
        }


def _app(
    *,
    session: _Session | None = _DEFAULT_SESSION,
    refreshed: bool = False,
    runtime_payload: dict[str, object] | None = None,
    runtime_status: int = 200,
):
    requests: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "pool.example.com":
            assert request.url.path == "/login/generic_oauth/callback"
            return httpx.Response(
                302,
                headers={
                    "location": (
                        "https://studio.example.com/oauth/callback"
                        "?code=user-pool-code&state=user-pool-state"
                    )
                },
            )
        return httpx.Response(
            runtime_status,
            json=runtime_payload or {"code": 0, "message": "authorized", "error": ""},
        )

    app = FastAPI()
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream),
        follow_redirects=False,
    )

    async def hosted_callback(provider_id: str) -> str:
        assert provider_id == "provider-1"
        return "https://pool.example.com/login/generic_oauth/callback"

    async def runtime_credentials(target: MpaCallbackTarget) -> MpaRuntimeCredentials:
        assert target == MpaCallbackTarget("mi-agent1")
        return MpaRuntimeCredentials(
            endpoint_origin="https://runtime.example.com",
            api_key="runtime-api-key",
        )

    mount_mpa_identity_callback(
        app,
        oauth2_handler=_OAuthHandler(session, refreshed=refreshed),
        hosted_callback_resolver=hosted_callback,
        runtime_credentials_resolver=runtime_credentials,
        http_client=http_client,
    )
    return app, http_client, requests


def test_callback_redirects_to_studio_login_and_preserves_callback() -> None:
    app, _, requests = _app(session=None)
    with TestClient(app, base_url="https://studio.example.com") as client:
        response = client.get(
            "/oauth/callback",
            params={"code": "idp-code", "state": _relay_state()},
            follow_redirects=False,
        )

    assert response.status_code == 302
    login = urlsplit(response.headers["location"])
    assert login.path == "/oauth2/login"
    callback = parse_qs(login.query)["redirect"][0]
    assert callback.startswith("/oauth/callback?")
    assert parse_qs(urlsplit(callback).query) == {
        "code": ["idp-code"],
        "state": [_relay_state()],
    }
    assert requests == []


def test_callback_relays_to_user_pool_and_runtime_server_side() -> None:
    app, _, requests = _app(refreshed=True)

    with TestClient(app, base_url="https://studio.example.com") as client:
        response = client.get(
            "/oauth/callback",
            params={"code": "idp-code", "state": _relay_state()},
        )

    assert response.status_code == 200
    assert "授权成功" in response.text
    assert response.headers["cache-control"] == "private, no-store, max-age=0"
    assert "studio_session=refreshed-session" in response.headers["set-cookie"]
    assert len(requests) == 2
    assert dict(requests[0].url.params) == {
        "code": "idp-code",
        "state": _relay_state(),
    }
    assert requests[1].url.path == "/identity/oauth/callback"
    assert dict(requests[1].url.params) == {
        "code": "user-pool-code",
        "state": "user-pool-state",
    }
    assert requests[1].headers["authorization"] == "Bearer runtime-api-key"
    assert "x-jwt-token" not in requests[1].headers


def test_callback_preserves_runtime_expired_result() -> None:
    app, _, _ = _app(
        runtime_status=400,
        runtime_payload={
            "code": 4003,
            "message": "",
            "error": "Invalid or expired state",
        },
    )

    with TestClient(app, base_url="https://studio.example.com") as client:
        response = client.get(
            "/oauth/callback",
            params={"code": "idp-code", "state": _relay_state()},
        )

    assert response.status_code == 400
    assert "授权链接已失效" in response.text


@pytest.mark.parametrize(
    ("params", "expected_status"),
    [
        ({"state": _relay_state()}, 400),
        ({"code": "idp-code", "state": "invalid"}, 400),
    ],
)
def test_callback_rejects_invalid_browser_input(
    params: dict[str, str],
    expected_status: int,
) -> None:
    app, _, requests = _app()

    with TestClient(app, base_url="https://studio.example.com") as client:
        response = client.get("/oauth/callback", params=params)

    assert response.status_code == expected_status
    assert response.json()["code"] == 4002
    assert requests == []


def test_callback_rejects_runtime_redirect_without_leaking_credentials() -> None:
    requests: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "pool.example.com":
            return httpx.Response(
                302,
                headers={
                    "location": "https://studio.example.com/oauth/callback?code=c&state=s"
                },
            )
        return httpx.Response(302, headers={"location": "https://evil.example.com"})

    app = FastAPI()
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))

    async def callback_url(_provider_id: str) -> str:
        return "https://pool.example.com/login/generic_oauth/callback"

    async def credentials(_target: MpaCallbackTarget) -> MpaRuntimeCredentials:
        return MpaRuntimeCredentials("https://runtime.example.com", "secret-key")

    mount_mpa_identity_callback(
        app,
        oauth2_handler=_OAuthHandler(_Session()),
        hosted_callback_resolver=callback_url,
        runtime_credentials_resolver=credentials,
        http_client=http_client,
    )

    with TestClient(app, base_url="https://studio.example.com") as client:
        response = client.get(
            "/oauth/callback",
            params={"code": "idp-code", "state": _relay_state()},
        )

    assert response.status_code == 502
    assert response.json() == {
        "code": 5000,
        "message": "",
        "error": "MPA authorization failed",
    }
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_user_pool_relay_requires_redirect_code_and_state() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200))
    )
    try:
        with pytest.raises(MpaIdentityCallbackError, match="did not return"):
            await _relay_user_pool_callback(
                client,
                "https://pool.example.com/login/generic_oauth/callback",
                "idp-code",
                _relay_state(),
            )
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(
            200,
            headers={"content-length": "70000"},
            content=b"{}",
        ),
        httpx.Response(200, content=b"x" * 70000),
        httpx.Response(200, content=b"not-json"),
        httpx.Response(200, json={"code": True, "message": "", "error": ""}),
    ],
)
def test_runtime_payload_rejects_invalid_responses(response: httpx.Response) -> None:
    with pytest.raises(MpaIdentityCallbackError):
        _parse_runtime_payload(response)


@pytest.mark.asyncio
async def test_runtime_callback_requires_api_key() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(500))
    )
    try:
        with pytest.raises(MpaIdentityCallbackError, match="key authentication"):
            await _call_runtime_callback(
                client,
                MpaRuntimeCredentials("https://runtime.example.com", ""),
                "code",
                "state",
            )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_runtime_callback_rejects_endpoint_paths() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(500))
    )
    try:
        with pytest.raises(MpaIdentityCallbackError, match="Runtime endpoint"):
            await _call_runtime_callback(
                client,
                MpaRuntimeCredentials(
                    "https://runtime.example.com/attacker-controlled",
                    "api-key",
                ),
                "code",
                "state",
            )
    finally:
        await client.aclose()


def test_runtime_nonstandard_error_is_preserved_as_json() -> None:
    response = _runtime_result_response(
        RuntimeCallbackResult(
            400,
            {"code": 4004, "message": "", "error": "token exchange failed"},
        )
    )

    assert response.status_code == 400
    assert json.loads(bytes(response.body)) == {
        "code": 4004,
        "message": "",
        "error": "token exchange failed",
    }
