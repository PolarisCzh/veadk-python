import sys
import time

import pytest
import yaml
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from frontend.server.mpa_creation import mount_mpa_creation_routes
from veadk.integrations.mpa.managed.tasks import CreationTasks, TaskError


def test_creation_routes_require_management_authorization(tmp_path):
    app = FastAPI()

    def denied(request):
        raise HTTPException(403, "Denied")

    mount_mpa_creation_routes(
        app, owner=denied, service=CreationTasks(tmp_path / "tasks.db")
    )
    with TestClient(app) as client:
        for method, path in [
            ("get", "/web/mpa-creation/config?region=cn-beijing"),
            ("post", "/web/mpa-creation/tasks"),
            ("get", "/web/mpa-creation/tasks/unknown"),
            ("post", "/web/mpa-creation/tasks/unknown/cancel"),
        ]:
            assert getattr(client, method)(path).status_code == 403


def test_missing_server_profile_is_actionable_and_safe(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "VEADK_MPA_CREATE_CONFIG", str(tmp_path / "private-missing.yaml")
    )
    app = FastAPI()
    mount_mpa_creation_routes(
        app, owner=lambda request: "local", service=CreationTasks(tmp_path / "tasks.db")
    )
    with TestClient(app) as client:
        result = client.get("/web/mpa-creation/config?region=cn-beijing")
        assert result.status_code == 200
        assert result.json()["configured"] is False
        assert "VEADK_MPA_CREATE_CONFIG" in result.json()["error"]
        assert "configuration file was not found" in result.json()["error"]
        assert "private-missing" not in result.text
        assert (
            client.post(
                "/web/mpa-creation/tasks", json={"command": "arbitrary"}
            ).status_code
            == 422
        )


def test_one_creation_upload_bypasses_missing_default_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("VEADK_MPA_CREATE_CONFIG", str(tmp_path / "missing.yaml"))
    monkeypatch.setenv("TEST_MPA_ADMIN", "postgresql://admin:fake@db/registry")
    monkeypatch.setenv("TEST_MPA_REGISTRY", "postgresql://user:fake@db/registry")
    monkeypatch.setattr(
        "frontend.server.mpa_creation.load_volcengine_credentials", lambda _: None
    )
    captured = {}

    class Tasks:
        path = tmp_path / "tasks.db"

        async def start(self, owner, payload, **kwargs):
            captured.update(owner=owner, payload=payload, kwargs=kwargs)
            captured["yaml"] = kwargs["config_path"].read_text()
            captured["mode"] = kwargs["config_path"].stat().st_mode & 0o777
            kwargs["config_path"].unlink()
            return {"taskId": "task-1", "state": "running"}

        async def close(self):
            pass

    app = FastAPI()
    mount_mpa_creation_routes(
        app,
        owner=lambda request: "owner",
        authorize_upload=lambda request: None,
        service=Tasks(),
    )
    config = yaml.safe_dump(
        {
            "region": "cn-beijing",
            "managed": {
                "version": 1,
                "database-admin-url-env": "TEST_MPA_ADMIN",
                "shared-database-url-env": "TEST_MPA_REGISTRY",
                "from-runtime": "r-reference",
                "worker": {"existing-id": "t-worker"},
            },
        }
    )
    body = {
        "requestId": "11111111-1111-4111-8111-111111111111",
        "agentId": "mi-test",
        "description": "",
        "region": "cn-beijing",
        "runtimeImage": "registry.example/mpa:test",
        "configYaml": config,
    }
    with TestClient(app) as client:
        assert (
            client.get("/web/mpa-creation/config?region=cn-beijing").json()[
                "uploadAllowed"
            ]
            is True
        )
        response = client.post("/web/mpa-creation/tasks", json=body)
    assert response.status_code == 202, response.text
    assert captured["yaml"] == config
    assert captured["mode"] == 0o600
    assert captured["kwargs"]["ephemeral_config"] is True
    assert captured["kwargs"]["images"]["runtimeImage"] == "registry.example/mpa:test"
    assert "configYaml" not in captured["payload"]


def test_upload_is_the_exact_file_read_by_the_task_child(tmp_path, monkeypatch):
    monkeypatch.setenv("VEADK_MPA_CREATE_CONFIG", str(tmp_path / "missing.yaml"))
    monkeypatch.setenv("TEST_MPA_ADMIN", "postgresql://admin:fake@db/registry")
    monkeypatch.setenv("TEST_MPA_REGISTRY", "postgresql://user:fake@db/registry")
    monkeypatch.setattr(
        "frontend.server.mpa_creation.load_volcengine_credentials", lambda _: None
    )
    config = yaml.safe_dump(
        {
            "region": "cn-beijing",
            "managed": {
                "version": 1,
                "database-admin-url-env": "TEST_MPA_ADMIN",
                "shared-database-url-env": "TEST_MPA_REGISTRY",
                "from-runtime": "r-reference",
                "worker": {"existing-id": "t-worker"},
            },
        }
    )
    script = (
        "import json,pathlib,sys; "
        "request=json.load(sys.stdin); "
        f"assert pathlib.Path(request['config']).read_text() == {config!r}; "
        "print('MPA_EVENT ' + json.dumps({'result': "
        "{'runtime_id':'r-test','skill_space_id':'space-test',"
        "'gateway_id':'g-test','agent_id':'mi-test',"
        "'region':'cn-beijing','state':'ready'}}))"
    )
    service = CreationTasks(tmp_path / "tasks.db")
    service.command = lambda: [sys.executable, "-c", script]
    app = FastAPI()
    mount_mpa_creation_routes(
        app,
        owner=lambda request: "owner",
        authorize_upload=lambda request: None,
        service=service,
    )
    with TestClient(app) as client:
        created = client.post(
            "/web/mpa-creation/tasks",
            json={
                "requestId": "11111111-1111-4111-8111-111111111111",
                "agentId": "mi-test",
                "region": "cn-beijing",
                "configYaml": config,
            },
        )
        assert created.status_code == 202
        for _ in range(100):
            task = client.get(
                f"/web/mpa-creation/tasks/{created.json()['taskId']}"
            ).json()
            if task["state"] not in {"running", "cancelling"}:
                break
            time.sleep(0.02)
        assert task["state"] == "succeeded", task
        assert "configYaml" not in task
        assert "configDigest" not in task
    assert not list(tmp_path.glob("mpa-create-*.yaml"))


@pytest.mark.parametrize("failure", ["credentials", "task"])
def test_upload_errors_remove_the_task_private_file(tmp_path, monkeypatch, failure):
    monkeypatch.setenv("TEST_MPA_ADMIN", "postgresql://admin:fake@db/registry")
    monkeypatch.setenv("TEST_MPA_REGISTRY", "postgresql://user:fake@db/registry")
    if failure == "credentials":

        def bad_credentials(_):
            raise ValueError("secret credential failure")

        monkeypatch.setattr(
            "frontend.server.mpa_creation.load_volcengine_credentials",
            bad_credentials,
        )
    else:
        monkeypatch.setattr(
            "frontend.server.mpa_creation.load_volcengine_credentials", lambda _: None
        )

    class Tasks:
        path = tmp_path / "tasks.db"

        async def start(self, *args, **kwargs):
            assert failure == "task"
            raise TaskError("Duplicate creation request")

        async def close(self):
            pass

    app = FastAPI()
    mount_mpa_creation_routes(
        app,
        owner=lambda request: "owner",
        authorize_upload=lambda request: None,
        service=Tasks(),
    )
    config = yaml.safe_dump(
        {
            "region": "cn-beijing",
            "managed": {
                "version": 1,
                "database-admin-url-env": "TEST_MPA_ADMIN",
                "shared-database-url-env": "TEST_MPA_REGISTRY",
                "from-runtime": "r-reference",
                "worker": {"existing-id": "t-worker"},
            },
        }
    )
    with TestClient(app) as client:
        response = client.post(
            "/web/mpa-creation/tasks",
            json={
                "requestId": "11111111-1111-4111-8111-111111111111",
                "agentId": "mi-test",
                "region": "cn-beijing",
                "configYaml": config,
            },
        )
    assert response.status_code == (400 if failure == "credentials" else 409)
    assert "secret credential failure" not in response.text
    assert not list(tmp_path.glob("mpa-create-*.yaml"))


def test_upload_requires_admin_and_rejects_untrusted_files(tmp_path, monkeypatch):
    monkeypatch.setenv("VEADK_MPA_CREATE_CONFIG", str(tmp_path / "missing.yaml"))
    app = FastAPI()
    service = CreationTasks(tmp_path / "tasks.db")

    def denied(request):
        raise HTTPException(403, "Administrator required")

    mount_mpa_creation_routes(
        app, owner=lambda request: "owner", authorize_upload=denied, service=service
    )
    body = {
        "requestId": "11111111-1111-4111-8111-111111111111",
        "agentId": "mi-test",
        "region": "cn-beijing",
        "configYaml": "managed: {version: 1}",
    }
    with TestClient(app) as client:
        assert (
            client.get("/web/mpa-creation/config?region=cn-beijing").json()[
                "uploadAllowed"
            ]
            is False
        )
        assert client.post("/web/mpa-creation/tasks", json=body).status_code == 403


@pytest.mark.parametrize(
    ("supported", "authorize_upload", "expected"),
    [(False, lambda request: None, 400), (True, None, 403)],
)
def test_upload_requires_supported_provider_and_admin_route(
    tmp_path, supported, authorize_upload, expected
):
    app = FastAPI()
    mount_mpa_creation_routes(
        app,
        owner=lambda request: "owner",
        authorize_upload=authorize_upload,
        service=CreationTasks(tmp_path / "tasks.db"),
        supported=supported,
    )
    with TestClient(app) as client:
        response = client.post(
            "/web/mpa-creation/tasks",
            json={
                "requestId": "11111111-1111-4111-8111-111111111111",
                "agentId": "mi-test",
                "region": "cn-beijing",
                "configYaml": "managed: {version: 1}",
            },
        )
    assert response.status_code == expected
    assert not list(tmp_path.glob("mpa-create-*.yaml"))


def test_upload_inspection_preserves_unexpected_authorization_failure(tmp_path):
    app = FastAPI()

    def unavailable(request):
        raise HTTPException(503, "Role service unavailable")

    mount_mpa_creation_routes(
        app,
        owner=lambda request: "owner",
        authorize_upload=unavailable,
        service=CreationTasks(tmp_path / "tasks.db"),
    )
    with TestClient(app) as client:
        response = client.get("/web/mpa-creation/config?region=cn-beijing")
    assert response.status_code == 503


@pytest.mark.parametrize(
    ("yaml_text", "status"),
    [
        ("managed: [secret", 400),
        ("managed: {version: 1, template-file: /etc/passwd}", 400),
        ("managed: {version: 1, credential_file: /etc/passwd}", 400),
        ("", 413),
        ("x" * 262145, 413),
    ],
)
def test_upload_rejects_invalid_content_before_start(
    tmp_path, monkeypatch, yaml_text, status
):
    monkeypatch.setenv("VEADK_MPA_CREATE_CONFIG", str(tmp_path / "missing.yaml"))

    class Tasks:
        path = tmp_path / "tasks.db"

        async def start(self, *args, **kwargs):
            pytest.fail("invalid upload started a task")

        async def close(self):
            pass

    app = FastAPI()
    mount_mpa_creation_routes(
        app,
        owner=lambda request: "owner",
        authorize_upload=lambda request: None,
        service=Tasks(),
    )
    with TestClient(app) as client:
        response = client.post(
            "/web/mpa-creation/tasks",
            json={
                "requestId": "11111111-1111-4111-8111-111111111111",
                "agentId": "mi-test",
                "region": "cn-beijing",
                "configYaml": yaml_text,
            },
        )
    assert response.status_code == status
    assert "secret" not in response.text
    assert not list(tmp_path.glob("mpa-create-*.yaml"))


def test_upload_region_or_server_secret_failure_cleans_private_file(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("VEADK_MPA_CREATE_CONFIG", str(tmp_path / "missing.yaml"))
    config = yaml.safe_dump(
        {
            "region": "cn-shanghai",
            "managed": {
                "version": 1,
                "worker": {"existing-id": "t-worker"},
                "from-runtime": "r-reference",
                "database-admin-url-env": "TEST_MPA_MISSING_ADMIN",
            },
        }
    )
    app = FastAPI()
    mount_mpa_creation_routes(
        app,
        owner=lambda request: "owner",
        authorize_upload=lambda request: None,
        service=CreationTasks(tmp_path / "tasks.db"),
    )
    body = {
        "requestId": "11111111-1111-4111-8111-111111111111",
        "agentId": "mi-test",
        "region": "cn-beijing",
        "configYaml": config,
    }
    with TestClient(app) as client:
        assert client.post("/web/mpa-creation/tasks", json=body).status_code == 400
        body["region"] = "cn-shanghai"
        result = client.post("/web/mpa-creation/tasks", json=body)
        assert result.status_code == 400
        assert "TEST_MPA_MISSING_ADMIN" in result.text
    assert not list(tmp_path.glob("mpa-create-*.yaml"))
