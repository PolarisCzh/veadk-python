from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from threading import Lock
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from tos.exceptions import TosServerError

from frontend.server.mpa.feishu_invites import InviteStore, mount_routes


class TosError(TosServerError):
    def __init__(self, status_code):
        super().__init__(
            SimpleNamespace(request_id="fake", status=status_code, headers={}),
            "fake storage error",
            "",
            "",
            "",
        )


class FakeTos:
    def __init__(self):
        self.data = {}
        self.lock = Lock()

    def get_object(self, *, bucket, key):
        with self.lock:
            if key not in self.data:
                raise TosError(404)
            body, version = self.data[key]
        return SimpleNamespace(etag=str(version), read=BytesIO(body).read)

    def put_object(
        self, *, key, content, if_match=None, forbid_overwrite=False, **kwargs
    ):
        with self.lock:
            previous = self.data.get(key)
            if previous is None:
                assert forbid_overwrite
                version = 1
            elif if_match != str(previous[1]):
                raise TosError(412)
            else:
                version = previous[1] + 1
            self.data[key] = (content, version)


class FakeRuntime:
    def __init__(self):
        self.calls = []
        self.statuses = {}
        self.key = Lock()

    async def __call__(self, target, binding_id):
        assert target.runtime_id == "r-example"
        assert target.region == "cn-beijing"
        assert target.issuer == "studio-admin"
        with self.key:
            self.calls.append(binding_id)
            if binding_id is None:
                binding_id = f"binding-{len(self.calls)}"
                self.statuses[binding_id] = "PENDING"
            status = self.statuses[binding_id]
        return {
            "id": binding_id,
            "bindingType": "private",
            "status": status,
            "loginUrl": f"https://open.feishu.cn/page/launcher?user_code={binding_id}",
            "expiresAt": "2026-11-23T00:10:00+00:00",
            "pollAfterSeconds": 5,
            "appId": f"cli_{binding_id}" if status == "BOUND" else None,
            "appSecret": "upstream-only",
        }


def fixture():
    tos = FakeTos()
    runtime = FakeRuntime()
    now = [1795392000.0]

    def authorize(request: Request, runtime_id: str, region: str):
        if request.headers.get("x-test-admin") != "yes":
            raise HTTPException(403)
        return "studio-admin"

    app = FastAPI()
    mount_routes(
        app,
        InviteStore("private-test", lambda: tos),
        authorize=authorize,
        runtime=runtime,
        clock=lambda: now[0],
    )
    return TestClient(app), tos, runtime, now


ADMIN = "/web/feishu-bot-invites"
PUBLIC = "/web/feishu-bot-invite"
TARGET = {"runtimeId": "r-example", "region": "cn-beijing"}
HEADERS = {"x-test-admin": "yes"}
FIRST = "a" * 43
SECOND = "b" * 43


def create(client, limit=1):
    response = client.post(
        ADMIN,
        headers=HEADERS,
        json={
            **TARGET,
            "maxUses": limit,
            "validHours": 24,
        },
    )
    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    return response.json()["token"]


def redeem(client, token, key, path="redeem"):
    response = client.post(f"{PUBLIC}/{path}", json={"token": token, "attempt": key})
    assert response.status_code == 200, response.text
    assert "upstream-only" not in response.text
    return response.json()


def test_concurrent_last_slot_refresh_and_success_are_idempotent():
    client, tos, runtime, _ = fixture()
    token = create(client)
    with ThreadPoolExecutor(2) as executor:
        results = list(
            executor.map(lambda key: redeem(client, token, key), [FIRST, SECOND])
        )
    assert {item["status"] for item in results} == {"pending", "full"}
    key = FIRST if results[0]["status"] == "pending" else SECOND
    assert results[0 if key == FIRST else 1]["qr"].startswith("data:image/png;base64,")
    assert b"upstream-only" not in next(iter(tos.data.values()))[0]
    assert token.encode() not in next(iter(tos.data.values()))[0]
    assert redeem(client, token, key)["status"] == "pending"
    assert runtime.calls.count(None) == 1
    runtime.statuses["binding-1"] = "BOUND"
    assert redeem(client, token, key, "status")["status"] == "bound"
    assert redeem(client, token, key, "status")["status"] == "bound"
    assert redeem(client, token, "c" * 43)["status"] == "full"


def test_shared_link_has_independent_sessions_and_can_be_revoked():
    client, _, runtime, _ = fixture()
    token = create(client, 2)
    first = redeem(client, token, FIRST)
    second = redeem(client, token, SECOND)
    assert first["qr"] != second["qr"]
    assert runtime.calls == [None, None]
    body = {**TARGET, "token": token}
    assert client.post(ADMIN + "/revoke", json=body).status_code == 403
    assert client.post(ADMIN + "/revoke", headers=HEADERS, json=body).status_code == 204
    assert redeem(client, token, FIRST, "status")["status"] == "revoked"
    assert redeem(client, token, "c" * 43)["status"] == "revoked"
    assert runtime.calls == [None, None]


def test_expired_attempt_releases_slot_only_after_runtime_confirmation():
    client, _, runtime, now = fixture()
    token = create(client)
    assert redeem(client, token, FIRST)["status"] == "pending"
    now[0] += 660
    assert redeem(client, token, SECOND)["status"] == "full"
    runtime.statuses["binding-1"] = "EXPIRED"
    assert redeem(client, token, SECOND)["status"] == "pending"
    assert runtime.calls == [None, "binding-1", "binding-1", None]


def test_link_scope_validation_expiry_and_unknown_token():
    client, _, runtime, now = fixture()
    token = create(client)
    body = {"token": token, "attempt": FIRST, "runtimeId": "r-other"}
    assert client.post(PUBLIC + "/redeem", json=body).status_code == 422
    assert (
        client.post(
            PUBLIC + "/redeem",
            json={
                "token": "s" * 43,
                "attempt": FIRST,
            },
        ).status_code
        == 404
    )
    assert (
        client.post(
            ADMIN + "/revoke",
            headers=HEADERS,
            json={
                **TARGET,
                "runtimeId": "r-other",
                "token": token,
            },
        ).status_code
        == 404
    )
    assert (
        client.post(
            ADMIN,
            headers=HEADERS,
            json={
                **TARGET,
                "maxUses": 101,
                "validHours": 24,
            },
        ).status_code
        == 422
    )
    now[0] += 24 * 3600
    assert redeem(client, token, FIRST)["status"] == "invite_expired"
    assert runtime.calls == []


def test_uncertain_registration_keeps_slot_and_reconciles_original():
    client, _, runtime, _ = fixture()
    token = create(client)
    assert redeem(client, token, FIRST, "status")["status"] == "expired"
    assert redeem(client, token, FIRST)["status"] == "pending"
    runtime.statuses["binding-1"] = "FAILED"
    assert redeem(client, token, FIRST, "status")["status"] == "uncertain"
    assert redeem(client, token, SECOND)["status"] == "full"
    runtime.statuses["binding-1"] = "BOUND"
    assert redeem(client, token, FIRST)["status"] == "bound"
    assert redeem(client, token, SECOND)["status"] == "full"
    assert runtime.calls == [None, "binding-1", "binding-1"]


def test_bad_runtime_response_is_redacted_and_releases_fresh_reservation():
    client, _, runtime, _ = fixture()
    token = create(client)
    fake = FakeRuntime.__call__

    async def invalid(self, target, binding_id):
        result = await fake(self, target, binding_id)
        result["loginUrl"] = "https://example.test/untrusted"
        return result

    from unittest.mock import patch

    with patch.object(FakeRuntime, "__call__", invalid):
        response = client.post(
            PUBLIC + "/redeem", json={"token": token, "attempt": FIRST}
        )
    assert response.status_code == 502
    assert response.json() == {"detail": "binding_unavailable"}
    assert redeem(client, token, SECOND)["status"] == "pending"
    assert runtime.calls == [None, None]


def test_runtime_adapter_uses_fixed_private_request_and_checks_configured_app(
    monkeypatch,
):
    import asyncio

    import httpx

    from frontend.server.mpa import feishu_invites as invites

    requests = []
    real_async_client = httpx.AsyncClient

    def respond(request):
        requests.append((request.method, request.url.path))
        assert request.headers["x-mpa-channel-key"] == "fake-runtime-key"
        assert request.headers["authorization"] == "Bearer fake-runtime-key"
        assert request.headers["x-user-id"] == "studio-admin"
        path = request.url.path
        if request.method == "POST":
            assert request.read() == b'{"bindingType":"private","domain":"feishu"}'
            return httpx.Response(201, json={"status": "PENDING", "id": "alpha"})
        if path.endswith("/bindings/alpha"):
            return httpx.Response(
                200,
                json={
                    "status": "FAILED",
                    "bindingType": "private",
                    "appId": "cli_fake",
                },
            )
        assert request.url.params["appId"] == "cli_fake"
        return httpx.Response(200, json={"configured": True, "appId": "cli_fake"})

    monkeypatch.setattr(
        invites.httpx,
        "AsyncClient",
        lambda **kwargs: real_async_client(
            transport=httpx.MockTransport(respond), **kwargs
        ),
    )
    invite = invites.Invitation(
        runtime_id="r-example",
        region="cn-beijing",
        issuer="studio-admin",
        max_uses=1,
        expires_at=1795478400.0,
    )

    def resolve(runtime_id, region):
        assert (runtime_id, region) == ("r-example", "cn-beijing")
        return (
            "https://mpa.example.test/runtime",
            "fake-runtime-key",
            "key_auth",
            "public",
        )

    async def run():
        assert (await invites.call_runtime_binding(invite, None, resolve=resolve))[
            "status"
        ] == "PENDING"
        assert (await invites.call_runtime_binding(invite, "alpha", resolve=resolve))[
            "status"
        ] == "BOUND"

    asyncio.run(run())
    assert requests == [
        ("POST", "/runtime/api/v1/channels/feishu/bindings"),
        ("GET", "/runtime/api/v1/channels/feishu/bindings/alpha"),
        ("GET", "/runtime/api/v1/channels/feishu/diagnostics"),
    ]


def test_shared_storage_missing_or_unavailable_fails_closed():
    client, tos, runtime, _ = fixture()
    token = create(client)
    key = next(iter(tos.data))
    value, version = tos.data[key]
    tos.data[key] = (value + b"invalid", version)
    response = client.post(PUBLIC + "/redeem", json={"token": token, "attempt": FIRST})
    assert response.status_code == 503
    app = FastAPI()
    mount_routes(app, None, authorize=lambda *_: "studio-admin", runtime=runtime)
    response = TestClient(app).post(
        ADMIN,
        json={
            **TARGET,
            "maxUses": 1,
            "validHours": 24,
        },
    )
    assert response.json() == {"detail": "invite_storage_required"}
    assert runtime.calls == []


def test_etag_contention_does_not_oversell_or_fallback(monkeypatch):
    client, tos, runtime, _ = fixture()
    token = create(client)
    original = tos.put_object

    def contention(*args, **kwargs):
        if kwargs.get("if_match") is not None:
            raise TosError(412)
        return original(*args, **kwargs)

    monkeypatch.setattr(tos, "put_object", contention)
    response = client.post(PUBLIC + "/redeem", json={"token": token, "attempt": FIRST})
    assert response.json() == {"detail": "invite_busy"}
    assert runtime.calls == []

    def outage(*args, **kwargs):
        raise TosError(503)

    monkeypatch.setattr(tos, "put_object", outage)
    response = client.post(
        ADMIN,
        headers=HEADERS,
        json={
            **TARGET,
            "maxUses": 1,
            "validHours": 24,
        },
    )
    assert response.json() == {"detail": "invite_unavailable"}


def test_mismatched_binding_status_cannot_charge_or_release_reservation(monkeypatch):
    client, _, runtime, _ = fixture()
    token = create(client)
    redeem(client, token, FIRST)
    original = FakeRuntime.__call__

    async def mismatch(self, target, binding_id):
        payload = await original(self, target, binding_id)
        return {**payload, "id": "wrong-binding", "status": "EXPIRED"}

    with monkeypatch.context() as scope:
        scope.setattr(FakeRuntime, "__call__", mismatch)
        response = client.post(
            PUBLIC + "/status", json={"token": token, "attempt": FIRST}
        )
    assert response.status_code == 502
    assert redeem(client, token, SECOND)["status"] == "full"
    assert runtime.calls == [None, "binding-1"]


def test_same_browser_replays_while_first_request_is_creating_qr(monkeypatch):
    from asyncio import to_thread
    from threading import Event

    client, _, runtime, _ = fixture()
    token = create(client)
    entered = Event()
    release = Event()
    original = FakeRuntime.__call__

    async def slow(self, target, binding_id):
        if binding_id is None:
            entered.set()
            assert await to_thread(release.wait, 5)
        return await original(self, target, binding_id)

    monkeypatch.setattr(FakeRuntime, "__call__", slow)
    with ThreadPoolExecutor(1) as executor:
        first = executor.submit(redeem, client, token, FIRST)
        try:
            assert entered.wait(5)
            assert redeem(client, token, FIRST, "status")["status"] == "starting"
            assert redeem(client, token, FIRST)["status"] == "starting"
        finally:
            release.set()
        assert first.result(timeout=5)["status"] == "pending"
    assert runtime.calls == [None]


def test_revocation_during_creation_never_exposes_qr(monkeypatch):
    client, tos, runtime, _ = fixture()
    token = create(client)
    original = FakeRuntime.__call__

    async def revoke_during_start(self, target, binding_id):
        InviteStore("private-test", lambda: tos).change(
            token, lambda invitation: setattr(invitation, "revoked", True)
        )
        return await original(self, target, binding_id)

    monkeypatch.setattr(FakeRuntime, "__call__", revoke_during_start)
    assert redeem(client, token, FIRST) == {"status": "revoked"}
    assert runtime.calls == [None]


def test_no_qr_if_upstream_does_not_report_private_pending(monkeypatch):
    client, _, runtime, _ = fixture()
    token = create(client)
    original = FakeRuntime.__call__

    async def already_bound(self, target, binding_id):
        payload = await original(self, target, binding_id)
        return {**payload, "status": "BOUND"}

    with monkeypatch.context() as scope:
        scope.setattr(FakeRuntime, "__call__", already_bound)
        response = client.post(
            PUBLIC + "/redeem", json={"token": token, "attempt": FIRST}
        )
    assert response.status_code == 502
    assert redeem(client, token, SECOND)["status"] == "pending"
    assert runtime.calls == [None, None]


def test_runtime_adapter_refuses_unsafe_binding_id_and_unsupported_auth():
    import asyncio

    import pytest

    from frontend.server.mpa import feishu_invites as invites

    invite = invites.Invitation(
        runtime_id="r-example",
        region="cn-beijing",
        issuer="studio-admin",
        max_uses=1,
        expires_at=1795478400.0,
    )

    def resolver(*_):
        return ("https://mpa.example.test", "", "custom_jwt", "public")

    async def run():
        with pytest.raises(HTTPException) as rejected:
            await invites.call_runtime_binding(invite, "../outside", resolve=resolver)
        assert rejected.value.detail == "binding_unavailable"
        with pytest.raises(HTTPException) as rejected:
            await invites.call_runtime_binding(invite, None, resolve=resolver)
        assert rejected.value.detail == "binding_unavailable"

    asyncio.run(run())
