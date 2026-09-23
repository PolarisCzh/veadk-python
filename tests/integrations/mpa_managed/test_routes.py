from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from frontend.server.mpa_creation import mount_mpa_creation_routes
from frontend.server.mpa_creation import CreationRequest
from veadk.integrations.mpa.managed.tasks import CreationTasks


def test_config_exposes_only_safe_workspace_summary(tmp_path, monkeypatch):
    from frontend.server import mpa_creation
    from tests.integrations.mpa_managed.test_config import split_profile_file

    monkeypatch.setattr(mpa_creation, "load_volcengine_credentials", lambda *_: None)
    path = split_profile_file(tmp_path, monkeypatch)
    monkeypatch.setenv("VEADK_MPA_CREATE_CONFIG", str(path))
    app = FastAPI()
    mount_mpa_creation_routes(
        app, owner=lambda request: "local", service=CreationTasks(tmp_path / "tasks.db")
    )
    with TestClient(app) as client:
        result = client.get("/web/mpa-creation/config?region=cn-beijing")
    data = result.json()
    assert data["postgresLayout"] == "split-workspaces"
    assert data["adminDatabaseName"] == "mpa_admin_db"
    assert data["pgHost"] == "business"
    assert "fake" not in result.text
    assert "postgresql" not in result.text
    assert "ws-management" not in result.text


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


def test_creation_request_rejects_invalid_dependency_fields():
    base = dict(
        requestId="11111111-1111-4111-8111-111111111111",
        agentId="mi-test",
        description="",
        region="cn-beijing",
    )
    for invalid in (
        {"pgHost": "db.example/path"},
        {"pgPort": "0"},
        {"pgPort": "not-a-port"},
        {"openvikingUrl": "https://api.example.test/?token=private"},
        {"openvikingResourceId": "ov bad"},
    ):
        from pydantic import ValidationError
        import pytest

        with pytest.raises(ValidationError):
            CreationRequest.model_validate({**base, **invalid})
