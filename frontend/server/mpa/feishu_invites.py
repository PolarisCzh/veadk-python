"""Studio-managed invitations to create private Feishu Bots."""

from __future__ import annotations

import hashlib
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from starlette.concurrency import run_in_threadpool
from tos.enum import ACLType
from tos.exceptions import TosClientError, TosServerError

from frontend.server.feishu_bot_setup.feishu_app_registration import (
    render_qr_data_url,
)
from frontend.server.storage import STUDIO_STORAGE_ROOT_PREFIX

PAGE = "/feishu-bot-invite"
PUBLIC = "/web/feishu-bot-invite"
ADMIN = "/web/feishu-bot-invites"
PUBLIC_PATHS = {PAGE, f"{PUBLIC}/redeem", f"{PUBLIC}/status"}
MAX_RECORD_BYTES = 128 * 1024
START_LEASE_SECONDS = 120
TOKEN = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{43}$")]
RUNTIME_ID = Annotated[str, StringConstraints(pattern=r"^r-[a-z0-9-]{1,64}$")]
REGION = Annotated[str, StringConstraints(pattern=r"^[a-z0-9-]{1,48}$")]
AttemptState = Literal["starting", "pending", "bound", "uncertain"]
PublicState = Literal[
    "starting",
    "pending",
    "bound",
    "uncertain",
    "expired",
    "full",
    "revoked",
    "invite_expired",
]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Target(Input):
    runtime_id: RUNTIME_ID = Field(alias="runtimeId")
    region: REGION


class Create(Target):
    max_uses: int = Field(alias="maxUses", strict=True, ge=1, le=100)
    valid_hours: int = Field(alias="validHours", strict=True, ge=1, le=168)


class Revoke(Target):
    token: TOKEN


class Redeem(Input):
    token: TOKEN
    attempt: TOKEN


class Attempt(BaseModel):
    generation: str
    state: AttemptState
    expires_at: float
    binding_id: str = ""


class Invitation(BaseModel):
    runtime_id: RUNTIME_ID
    region: REGION
    issuer: str
    expires_at: float
    max_uses: int
    revoked: bool = False
    attempts: dict[str, Attempt] = Field(default_factory=dict)


class Binding(BaseModel):
    id: Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_-]{1,128}$")]
    binding_type: Literal["private"] = Field(alias="bindingType")
    status: Literal[
        "PENDING", "SCANNED", "AUTHORIZED", "REGISTERING", "BOUND", "FAILED", "EXPIRED"
    ]
    login_url: str = Field(default="", alias="loginUrl", max_length=4096)
    expires_at: datetime = Field(alias="expiresAt")
    poll_after_seconds: int = Field(default=10, alias="pollAfterSeconds")


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class InviteStore:
    """One private object per invitation so reservation and accounting share an ETag."""

    def __init__(self, bucket: str, client_factory: Callable[[], Any]):
        self._bucket = bucket
        self._client_factory = client_factory

    @staticmethod
    def _key(token: str) -> str:
        return f"{STUDIO_STORAGE_ROOT_PREFIX}/feishu-bot-invites/{_hash(token)}.json"

    def load(self, token: str) -> tuple[Invitation, str]:
        try:
            response = self._client_factory().get_object(
                bucket=self._bucket, key=self._key(token)
            )
            try:
                body = response.read(MAX_RECORD_BYTES + 1)
            finally:
                stream = getattr(getattr(response, "resp", None), "resp", None)
                if stream is not None:
                    stream.close()
            if not response.etag or len(body) > MAX_RECORD_BYTES:
                raise ValueError("Invalid invitation object")
            return Invitation.model_validate_json(body), str(response.etag)
        except (TosServerError, TosClientError, ValueError) as error:
            code = (
                404
                if isinstance(error, TosServerError) and error.status_code == 404
                else 503
            )
            raise HTTPException(code, "invite_unavailable") from None

    def save(self, token: str, invitation: Invitation, etag: str = "") -> bool:
        try:
            self._client_factory().put_object(
                bucket=self._bucket,
                key=self._key(token),
                content=invitation.model_dump_json().encode(),
                content_type="application/json",
                cache_control="no-store",
                acl=ACLType.ACL_Private,
                **({"if_match": etag} if etag else {"forbid_overwrite": True}),
            )
        except (TosServerError, TosClientError) as error:
            if isinstance(error, TosServerError) and error.status_code in {409, 412}:
                return False
            raise HTTPException(503, "invite_unavailable") from None
        return True

    def change(self, token: str, edit: Callable[[Invitation], None]) -> Invitation:
        for _ in range(8):
            current, etag = self.load(token)
            updated = current.model_copy(deep=True)
            edit(updated)
            if updated == current or self.save(token, updated, etag):
                return updated
        raise HTTPException(503, "invite_busy")


RuntimeBinding = Callable[[Invitation, str | None], Awaitable[dict[str, Any]]]
AdminAuthorizer = Callable[[Request, str, str], str]


def _status(status: PublicState, **extra: Any) -> dict[str, Any]:
    return {"status": status, **extra}


class FeishuInvites:
    def __init__(
        self, store: InviteStore, runtime: RuntimeBinding, clock: Callable[[], float]
    ):
        self.store = store
        self.runtime = runtime
        self.clock = clock

    async def _binding(self, invite: Invitation, binding_id: str | None) -> Binding:
        try:
            result = Binding.model_validate(await self.runtime(invite, binding_id))
            if result.expires_at.utcoffset() is None or (
                binding_id is not None and result.id != binding_id
            ):
                raise ValueError("Invalid binding response")
            return result
        except ValueError:
            raise HTTPException(502, "binding_unavailable") from None

    def _closed(self, invite: Invitation) -> PublicState | None:
        if invite.revoked:
            return "revoked"
        if invite.expires_at <= self.clock():
            return "invite_expired"
        return None

    async def _poll(
        self, token: str, key: str, invite: Invitation, attempt: Attempt, *, qr: bool
    ) -> dict[str, Any]:
        result = await self._binding(invite, attempt.binding_id)

        def record(current: Invitation) -> None:
            stored = current.attempts.get(key)
            if (
                not stored
                or stored.generation != attempt.generation
                or stored.state == "bound"
            ):
                return
            if result.status == "EXPIRED":
                del current.attempts[key]
            elif result.status == "BOUND":
                stored.state = "bound"
            elif result.status == "FAILED":
                stored.state = "uncertain"

        saved = await run_in_threadpool(self.store.change, token, record)
        current = saved.attempts.get(key)
        if current is None or current.generation != attempt.generation:
            return _status("expired")
        if current.state != "pending":
            return _status(current.state)
        if closed := self._closed(saved):
            return _status(closed)
        return self._pending(result, qr=qr)

    @staticmethod
    def _pending(result: Binding, *, qr: bool) -> dict[str, Any]:
        payload = _status(
            "pending", pollAfterSeconds=max(3, min(result.poll_after_seconds, 60))
        )
        if qr:
            parsed = urlsplit(result.login_url)
            if parsed.scheme != "https" or parsed.netloc not in {
                "open.feishu.cn",
                "accounts.feishu.cn",
            }:
                raise HTTPException(502, "binding_unavailable")
            payload["qr"] = render_qr_data_url(result.login_url)
        return payload

    async def redeem(self, body: Redeem, *, start: bool) -> dict[str, Any]:
        token, key = body.token, _hash(body.attempt)
        invite, _ = await run_in_threadpool(self.store.load, token)
        existing = invite.attempts.get(key)
        if existing and existing.state == "bound":
            return _status("bound")
        if closed := self._closed(invite):
            return _status(closed)
        if existing and existing.state in {"pending", "uncertain"}:
            return await self._poll(token, key, invite, existing, qr=start)
        if not start:
            return _status("starting" if existing else "expired", pollAfterSeconds=3)

        now = self.clock()
        if len(invite.attempts) >= invite.max_uses:
            for other_key, other in invite.attempts.items():
                if (
                    other.state == "pending"
                    and other.expires_at <= now
                    and (await self._poll(token, other_key, invite, other, qr=False))[
                        "status"
                    ]
                    == "expired"
                ):
                    break

        reservation = Attempt(
            generation=secrets.token_hex(16),
            state="starting",
            expires_at=now + START_LEASE_SECONDS,
        )

        def reserve(current: Invitation) -> None:
            if self._closed(current):
                return
            current.attempts = {
                name: attempt
                for name, attempt in current.attempts.items()
                if attempt.state != "starting" or attempt.expires_at > self.clock()
            }
            if key not in current.attempts and len(current.attempts) < current.max_uses:
                current.attempts[key] = reservation

        saved = await run_in_threadpool(self.store.change, token, reserve)
        if closed := self._closed(saved):
            return _status(closed)
        active = saved.attempts.get(key)
        if active is None:
            return _status("full")
        if active.generation != reservation.generation:
            if active.state == "pending":
                return await self._poll(token, key, saved, active, qr=True)
            return _status(active.state, pollAfterSeconds=3)

        # No QR reaches a browser until its Runtime binding ID is durable.
        try:
            result = await self._binding(saved, None)
            if result.status != "PENDING":
                raise HTTPException(502, "binding_unavailable")
            payload = self._pending(result, qr=True)
        except HTTPException:

            def release(current: Invitation) -> None:
                if current.attempts.get(key) == reservation:
                    del current.attempts[key]

            await run_in_threadpool(self.store.change, token, release)
            raise

        def publish(current: Invitation) -> None:
            stored = current.attempts.get(key)
            if (
                stored is not None
                and stored == reservation
                and not self._closed(current)
            ):
                stored.binding_id = result.id
                stored.expires_at = result.expires_at.timestamp()
                stored.state = "pending"

        updated = await run_in_threadpool(self.store.change, token, publish)
        if closed := self._closed(updated):
            return _status(closed)
        current = updated.attempts.get(key)
        if not current or current.binding_id != result.id:
            return _status("expired")
        return payload


async def call_runtime_binding(
    invite: Invitation,
    binding_id: str | None,
    *,
    resolve: Callable[[str, str], tuple[str, str, str, str]],
) -> dict[str, Any]:
    from veadk.integrations.mpa.channel_proxy import channel_management_headers

    prefix = "api/v1/channels/feishu"
    path = f"{prefix}/bindings"
    if binding_id is not None:
        if re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", binding_id) is None:
            raise HTTPException(502, "binding_unavailable")
        path += f"/{binding_id}"
    try:
        endpoint, key, auth_type, _ = await run_in_threadpool(
            resolve, invite.runtime_id, invite.region
        )
        if auth_type != "key_auth":
            raise ValueError("Runtime key authentication is required")
        headers = channel_management_headers(path, is_admin=True, api_key=key)
        headers["Authorization"] = "Bearer " + headers["X-MPA-Channel-Key"]
        headers["X-User-Id"] = invite.issuer
        async with httpx.AsyncClient(
            base_url=endpoint.rstrip("/") + "/",
            headers=headers,
            timeout=20,
            follow_redirects=False,
        ) as client:
            response = (
                await client.get(path)
                if binding_id
                else await client.post(
                    path, json={"bindingType": "private", "domain": "feishu"}
                )
            )
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict):
                raise TypeError("Invalid binding response")
            app_id = result.get("appId")
            if (
                result.get("status") == "FAILED"
                and result.get("bindingType") == "private"
                and isinstance(app_id, str)
                and re.fullmatch(r"cli_[a-zA-Z0-9]{1,128}", app_id)
            ):
                diagnostic = await client.get(
                    f"{prefix}/diagnostics", params={"appId": app_id}
                )
                diagnostic.raise_for_status()
                detail = diagnostic.json()
                if (
                    isinstance(detail, dict)
                    and detail.get("appId") == app_id
                    and detail.get("configured") is True
                ):
                    result["status"] = "BOUND"
            return result
    except (httpx.HTTPError, ValueError, TypeError, HTTPException):
        raise HTTPException(502, "binding_unavailable") from None


def mount_routes(
    app: FastAPI,
    store: InviteStore | None,
    *,
    authorize: AdminAuthorizer,
    runtime: RuntimeBinding,
    clock: Callable[[], float] = time.time,
) -> None:
    def require_store() -> InviteStore:
        if store is None:
            raise HTTPException(503, "invite_storage_required")
        return store

    @app.post(ADMIN, status_code=201)
    async def create(request: Request, response: Response, body: Create):
        issuer = await run_in_threadpool(
            authorize, request, body.runtime_id, body.region
        )
        token = secrets.token_urlsafe(32)
        invite = Invitation(
            runtime_id=body.runtime_id,
            region=body.region,
            issuer=issuer,
            expires_at=clock() + body.valid_hours * 3600,
            max_uses=body.max_uses,
        )
        if not await run_in_threadpool(require_store().save, token, invite):
            raise HTTPException(503, "invite_busy")
        response.headers["Cache-Control"] = "no-store"
        return {"token": token}

    @app.post(f"{ADMIN}/revoke", status_code=204)
    async def revoke(request: Request, body: Revoke) -> Response:
        await run_in_threadpool(authorize, request, body.runtime_id, body.region)

        def cancel(invite: Invitation) -> None:
            if (invite.runtime_id, invite.region) != (body.runtime_id, body.region):
                raise HTTPException(404, "invite_unavailable")
            invite.revoked = True

        await run_in_threadpool(require_store().change, body.token, cancel)
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    @app.post(f"{PUBLIC}/redeem")
    async def redeem(body: Redeem, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return await FeishuInvites(require_store(), runtime, clock).redeem(
            body, start=True
        )

    @app.post(f"{PUBLIC}/status")
    async def status(body: Redeem, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return await FeishuInvites(require_store(), runtime, clock).redeem(
            body, start=False
        )
