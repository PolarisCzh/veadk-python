"""Authorized Studio entry points for VeADK-owned MPA creation."""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from veadk.integrations.mpa.managed.config import (
    ConfigurationError,
    load_profile,
    validate_image_reference,
)
from veadk.integrations.mpa.managed.credentials import load_volcengine_credentials
from veadk.integrations.mpa.managed.tasks import CreationTasks, TaskError, owner_key


class CreationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requestId: UUID
    agentId: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    description: str = Field(default="", max_length=512)
    region: str = Field(pattern=r"^cn-[a-z]+$", max_length=32)
    runtimeImage: str = Field(default="", max_length=1024)
    workerImage: str = Field(default="", max_length=1024)
    configYaml: str | None = Field(default=None, repr=False)

    @field_validator("runtimeImage", "workerImage")
    @classmethod
    def validate_image(cls, value):
        return validate_image_reference(value)


def mount_mpa_creation_routes(
    app: FastAPI,
    *,
    owner,
    authorize_upload=None,
    service: CreationTasks | None = None,
    supported=True,
):
    tasks = service

    def get_tasks():
        nonlocal tasks
        if tasks is None:
            tasks = CreationTasks(
                Path(os.getenv("VEADK_MPA_TASK_DB", ".adk/mpa-creation.sqlite3"))
            )
        return tasks

    def profile(region):
        if not supported:
            raise ConfigurationError("MPA creation requires the Volcengine provider")
        path = os.getenv("VEADK_MPA_CREATE_CONFIG", "mpa-create.config.yaml")
        result = load_profile(path, region=region)
        try:
            load_volcengine_credentials(result.managed.credential_file)
        except ValueError:
            raise ConfigurationError(
                "Configure server deployment credentials"
            ) from None
        return path, result

    @app.get("/web/mpa-creation/config")
    async def inspect(request: Request, region: str):
        owner(request)
        upload_allowed = False
        if supported and authorize_upload is not None:
            try:
                authorize_upload(request)
                upload_allowed = True
            except HTTPException as exc:
                if exc.status_code not in {401, 403}:
                    raise
        try:
            _, result = profile(region)
            return {**result.summary(), "uploadAllowed": upload_allowed}
        except ConfigurationError as exc:
            return {
                "configured": False,
                "region": region,
                "error": str(exc),
                "uploadAllowed": upload_allowed,
            }

    @app.post("/web/mpa-creation/tasks", status_code=202)
    async def create(request: Request):
        identity = owner_key(owner(request))
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > 2 * 262144 + 8192:
                raise HTTPException(413, "Creation request is too large")
        try:
            body = CreationRequest.model_validate_json(bytes(content))
        except ValidationError:
            raise HTTPException(422, "Invalid MPA creation request") from None
        uploaded = body.configYaml
        if uploaded is None and len(content) > 8192:
            raise HTTPException(413, "Creation request is too large")
        if uploaded is not None:
            if not supported:
                raise HTTPException(
                    400, "MPA creation requires the Volcengine provider"
                )
            if authorize_upload is None:
                raise HTTPException(403, "Administrator required for YAML upload")
            authorize_upload(request)
            encoded = uploaded.encode("utf-8")
            if not encoded or len(encoded) > 262144:
                raise HTTPException(413, "YAML must be between 1 byte and 256 KiB")
            try:
                values = yaml.safe_load(uploaded)
            except yaml.YAMLError:
                raise HTTPException(
                    400, "Invalid YAML in MPA creation configuration"
                ) from None
            managed = values.get("managed") if isinstance(values, dict) else None
            if isinstance(managed, dict) and any(
                managed.get(key)
                for key in (
                    "template-file",
                    "template_file",
                    "credential-file",
                    "credential_file",
                )
            ):
                raise HTTPException(
                    400, "Uploaded YAML cannot reference server-side files"
                )
            tasks = get_tasks()
            fd, name = tempfile.mkstemp(
                prefix="mpa-create-", suffix=".yaml", dir=tasks.path.parent
            )
            config_path = Path(name)
            delegated = False
            try:
                with os.fdopen(fd, "wb") as file:
                    file.write(encoded)
                result = load_profile(config_path, region=body.region)
                try:
                    load_volcengine_credentials(result.managed.credential_file)
                except ValueError:
                    raise ConfigurationError(
                        "Configure server deployment credentials"
                    ) from None
                payload = body.model_dump(mode="json", exclude={"configYaml"})
                payload["configDigest"] = hashlib.sha256(encoded).hexdigest()
                images = result.image_defaults()
                for field in ("runtimeImage", "workerImage"):
                    if payload[field]:
                        images[field] = payload[field]
                    else:
                        payload.pop(field)
                created = await tasks.start(
                    identity,
                    payload,
                    config_path=config_path,
                    timeout=result.managed.timeout_seconds,
                    images=images,
                    ephemeral_config=True,
                )
                delegated = True
                return created
            except ConfigurationError as exc:
                raise HTTPException(400, str(exc)) from None
            except TaskError as exc:
                raise HTTPException(409, str(exc)) from None
            finally:
                if not delegated:
                    config_path.unlink(missing_ok=True)
        try:
            path, config = profile(body.region)
            payload = body.model_dump(mode="json", exclude={"configYaml"})
            images = config.image_defaults()
            for field in ("runtimeImage", "workerImage"):
                if payload[field]:
                    images[field] = payload[field]
                else:
                    payload.pop(field)
            return await get_tasks().start(
                identity,
                payload,
                config_path=path,
                timeout=config.managed.timeout_seconds,
                images=images,
            )
        except ConfigurationError as exc:
            raise HTTPException(400, str(exc)) from None
        except TaskError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get("/web/mpa-creation/tasks/{task_id}")
    async def status(request: Request, task_id: str):
        identity = owner_key(owner(request))
        try:
            return get_tasks().get(identity, task_id)
        except TaskError:
            raise HTTPException(404, "Creation task not found") from None

    @app.post("/web/mpa-creation/tasks/{task_id}/cancel")
    async def cancel(request: Request, task_id: str):
        identity = owner_key(owner(request))
        try:
            return await get_tasks().cancel(identity, task_id)
        except TaskError:
            raise HTTPException(404, "Creation task not found") from None

    async def shutdown():
        if tasks is not None:
            await tasks.close()

    previous_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(instance: FastAPI) -> AsyncIterator[dict[str, Any]]:
        try:
            async with previous_lifespan(instance) as state:
                yield dict(state or {})
        finally:
            await shutdown()

    app.router.lifespan_context = lifespan
