"""Managed configuration fails before any cloud or database write."""

import pytest
import yaml

from veadk.integrations.mpa.managed.config import (
    load_profile,
    with_creation_resources,
    ConfigurationError,
)


def profile_file(tmp_path, monkeypatch, **managed):
    monkeypatch.setenv("TEST_MPA_ADMIN", "postgresql://admin:fake@db/registry")
    monkeypatch.setenv("TEST_MPA_REGISTRY", "postgresql://user:fake@db/registry")
    data = {
        "region": "cn-beijing",
        "managed": {
            "version": 1,
            "database-admin-url-env": "TEST_MPA_ADMIN",
            "shared-database-url-env": "TEST_MPA_REGISTRY",
            "from-runtime": "r-source",
            "worker": {"existing-id": "t-worker"},
            **managed,
        },
    }
    path = tmp_path / "profile.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def test_summary_contains_no_secrets(tmp_path, monkeypatch):
    profile = load_profile(profile_file(tmp_path, monkeypatch))
    assert profile.region == "cn-beijing"
    assert "fake" not in str(profile.summary())
    assert "postgresql" not in str(profile.summary())
    assert profile.summary()["pgHost"] == "db"
    assert profile.summary()["pgPort"] == "5432"


def test_studio_deployment_identity_is_inherited_by_managed_profile(
    tmp_path, monkeypatch
):
    path = profile_file(tmp_path, monkeypatch)
    values = {
        "VEADK_STUDIO_MPA_USER_POOL_NAME": "studio-pool",
        "VEADK_STUDIO_MPA_USER_POOL_CLIENT_NAME": "studio-client",
        "VEADK_STUDIO_MPA_IDENTITY_CALLBACK_URL": "https://studio.example.com/oauth/callback",
        "VEADK_STUDIO_MPA_IDENTITY_REGION": "cn-shanghai",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    profile = load_profile(path)
    assert {
        key: profile.values[key]
        for key in (
            "user_pool_name",
            "user_pool_client_name",
            "identity_callback_url",
            "identity_region",
        )
    } == {
        "user_pool_name": "studio-pool",
        "user_pool_client_name": "studio-client",
        "identity_callback_url": "https://studio.example.com/oauth/callback",
        "identity_region": "cn-shanghai",
    }
    assert "studio-pool" not in str(profile.summary())

    data = yaml.safe_load(path.read_text())
    data.update(
        {
            "user-pool-name": "studio-pool",
            "user-pool-client-name": "studio-client",
            "identity-callback-url": "https://studio.example.com/oauth/callback",
            "identity-region": "cn-shanghai",
        }
    )
    path.write_text(yaml.safe_dump(data))
    assert load_profile(path).values["user_pool_name"] == "studio-pool"


@pytest.mark.parametrize(
    "field,value",
    [
        ("user-pool-name", "different-pool"),
        ("user-pool-client-name", "different-client"),
        ("identity-callback-url", "https://other.example.com/oauth/callback"),
        ("identity-region", "cn-beijing"),
    ],
)
def test_studio_deployment_identity_rejects_conflicting_yaml(
    tmp_path, monkeypatch, field, value
):
    path = profile_file(tmp_path, monkeypatch)
    for key, selected in {
        "VEADK_STUDIO_MPA_USER_POOL_NAME": "studio-pool",
        "VEADK_STUDIO_MPA_USER_POOL_CLIENT_NAME": "studio-client",
        "VEADK_STUDIO_MPA_IDENTITY_CALLBACK_URL": "https://studio.example.com/oauth/callback",
        "VEADK_STUDIO_MPA_IDENTITY_REGION": "cn-shanghai",
    }.items():
        monkeypatch.setenv(key, selected)
    data = yaml.safe_load(path.read_text())
    data[field] = value
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigurationError, match="differs from Studio deployment"):
        load_profile(path)


def test_studio_deployment_identity_rejects_partial_and_runtime_env_conflict(
    tmp_path, monkeypatch
):
    path = profile_file(tmp_path, monkeypatch)
    monkeypatch.setenv("VEADK_STUDIO_MPA_USER_POOL_NAME", "studio-pool")
    with pytest.raises(ConfigurationError, match="Incomplete Studio MPA identity"):
        load_profile(path)
    for key, value in {
        "VEADK_STUDIO_MPA_USER_POOL_CLIENT_NAME": "studio-client",
        "VEADK_STUDIO_MPA_IDENTITY_CALLBACK_URL": "https://studio.example.com/oauth/callback",
        "VEADK_STUDIO_MPA_IDENTITY_REGION": "cn-shanghai",
    }.items():
        monkeypatch.setenv(key, value)
    data = yaml.safe_load(path.read_text())
    data["managed"]["runtime"] = {"env": {"MPA_USER_POOL_NAME": "other-pool"}}
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigurationError, match="differs from Studio deployment"):
        load_profile(path)


def test_standalone_managed_profile_keeps_explicit_identity(tmp_path, monkeypatch):
    path = profile_file(tmp_path, monkeypatch)
    data = yaml.safe_load(path.read_text())
    data["user-pool-name"] = "standalone-pool"
    path.write_text(yaml.safe_dump(data))
    assert load_profile(path).values["user_pool_name"] == "standalone-pool"


def split_profile_file(tmp_path, monkeypatch):
    path = profile_file(
        tmp_path,
        monkeypatch,
        postgres={
            "admin-workspace-id": "ws-management",
            "business-workspace-id": "ws-business",
            "admin-database-url-env": "TEST_REGISTRY_ADMIN",
        },
    )
    monkeypatch.setenv("TEST_MPA_ADMIN", "postgresql://app:fake@business/aidb")
    monkeypatch.setenv(
        "TEST_MPA_REGISTRY", "postgresql://registry:fake@management/mpa_admin_db"
    )
    monkeypatch.setenv("TEST_REGISTRY_ADMIN", "postgresql://owner:fake@management/aidb")
    return path


def test_two_workspaces_keep_business_defaults_and_safe_management_summary(
    tmp_path, monkeypatch
):
    path = split_profile_file(tmp_path, monkeypatch)
    profile = load_profile(path)
    assert profile.summary()["pgHost"] == "business"
    assert profile.summary()["postgresLayout"] == "split-workspaces"
    assert profile.summary()["adminWorkspaceName"] == "mpa_admin_workspace"
    assert profile.summary()["adminDatabaseName"] == "mpa_admin_db"
    assert "fake" not in str(profile.summary())
    # The powerful management maintenance credential is unnecessary at runtime.
    monkeypatch.delenv("TEST_REGISTRY_ADMIN")
    assert load_profile(path).summary() == profile.summary()


@pytest.mark.parametrize(
    "change", ["same-workspace", "same-host", "wrong-name", "wrong-database"]
)
def test_two_workspaces_reject_invalid_boundaries(tmp_path, monkeypatch, change):
    path = split_profile_file(tmp_path, monkeypatch)
    data = yaml.safe_load(path.read_text())
    if change == "same-workspace":
        data["managed"]["postgres"]["business-workspace-id"] = "ws-management"
    elif change == "wrong-name":
        data["managed"]["postgres"]["admin-workspace-name"] = "wrong"
    elif change == "same-host":
        monkeypatch.setenv(
            "TEST_MPA_REGISTRY", "postgresql://registry:fake@BUSINESS/mpa_admin_db"
        )
    else:
        monkeypatch.setenv(
            "TEST_MPA_REGISTRY", "postgresql://registry:fake@management/old_registry"
        )
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigurationError) as error:
        load_profile(path)
    assert "fake" not in str(error.value)


def test_creation_resources_override_runtime_environment_without_changing_profile(
    tmp_path, monkeypatch
):
    profile = load_profile(profile_file(tmp_path, monkeypatch))
    selected = with_creation_resources(
        profile,
        {
            "pgHost": "db",
            "pgPort": "5432",
            "openvikingUrl": "https://api.vikingdb.cn-beijing.volces.com/openviking",
            "openvikingResourceId": "ov-example",
        },
    )
    assert selected.managed.runtime.env == {
        "PGHOST": "db",
        "PGPORT": "5432",
        "OPENVIKING_URL": "https://api.vikingdb.cn-beijing.volces.com/openviking",
        "OPENVIKING_RESOURCE_ID": "ov-example",
    }
    assert profile.managed.runtime.env == {}
    assert "fake" not in str(selected.managed.runtime.env)

    changed_url = with_creation_resources(
        profile,
        {
            "openvikingUrl": "https://new.example.test/openviking",
        },
    )
    assert changed_url.managed.runtime.env["OPENVIKING_RESOURCE_ID"] == ""


@pytest.mark.parametrize(
    "resources",
    [
        {"pgHost": "different.example", "pgPort": "5432"},
        {"pgHost": "db", "pgPort": "5433"},
        {"openvikingUrl": "http://example.test/openviking"},
        {"openvikingUrl": "https://example.test:443/openviking"},
        {"openvikingUrl": "https://user:private@example.test/openviking"},
        {"openvikingResourceId": "ov-example"},
    ],
)
def test_creation_resources_reject_unsafe_or_incompatible_values(
    tmp_path, monkeypatch, resources
):
    profile = load_profile(profile_file(tmp_path, monkeypatch))
    with pytest.raises(ConfigurationError):
        with_creation_resources(profile, resources)


def test_invalid_profile_and_missing_secret_are_safe(tmp_path, monkeypatch):
    path = profile_file(tmp_path, monkeypatch, **{"provider-root": "/not-supported"})
    with pytest.raises(ConfigurationError):
        load_profile(path)
    path = profile_file(tmp_path, monkeypatch)
    monkeypatch.delenv("TEST_MPA_ADMIN")
    with pytest.raises(ConfigurationError, match="TEST_MPA_ADMIN"):
        load_profile(path)


def test_profile_rejects_region_change_and_ambiguous_source(tmp_path, monkeypatch):
    path = profile_file(tmp_path, monkeypatch)
    with pytest.raises(ConfigurationError, match="region"):
        load_profile(path, region="cn-shanghai")
    path = profile_file(tmp_path, monkeypatch, **{"template-file": "private.json"})
    with pytest.raises(ConfigurationError):
        load_profile(path)


def test_invalid_yaml_does_not_echo_secret(tmp_path):
    path = tmp_path / "invalid.yaml"
    path.write_text("password: [never-echo-this")
    with pytest.raises(ConfigurationError) as error:
        load_profile(path)
    assert "never-echo-this" not in str(error.value)


def test_adoption_requires_explicit_network_before_mutations(tmp_path, monkeypatch):
    path = profile_file(tmp_path, monkeypatch, apig={"adopt-id": "g-existing"})
    with pytest.raises(ConfigurationError, match="VPC"):
        load_profile(path)


def test_cli_managed_dry_run_never_calls_cloud(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from veadk.cli.cli_mpa import mpa
    from veadk.integrations.mpa.managed import service

    monkeypatch.setattr(
        service, "RuntimeCloud", lambda **kw: pytest.fail("dry-run called cloud")
    )
    path = profile_file(tmp_path, monkeypatch)
    result = CliRunner().invoke(
        mpa, ["provision", "--config", str(path), "--agent-id", "my-agent", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert '"dryRun": true' in result.output
    assert "fake" not in result.output


def test_missing_profile_reports_server_setting_without_disclosing_path(tmp_path):
    with pytest.raises(ConfigurationError) as error:
        load_profile(tmp_path / "private-deployment.yaml")
    message = str(error.value)
    assert "configuration file was not found" in message
    assert "VEADK_MPA_CREATE_CONFIG" in message
    assert str(tmp_path) not in message


def test_invalid_yaml_identifies_syntax_without_echoing_input(tmp_path):
    path = tmp_path / "invalid.yaml"
    path.write_text("password: [never-echo-this")
    with pytest.raises(ConfigurationError) as error:
        load_profile(path)
    assert "Invalid YAML" in str(error.value)
    assert "never-echo-this" not in str(error.value)


def test_missing_template_is_distinct_from_missing_profile(tmp_path, monkeypatch):
    path = profile_file(
        tmp_path, monkeypatch, **{"from-runtime": "", "template-file": "missing.json"}
    )
    with pytest.raises(ConfigurationError, match="Runtime template file was not found"):
        load_profile(path)


def test_unreadable_profile_has_actionable_safe_error(tmp_path, monkeypatch):
    from pathlib import Path

    path = tmp_path / "private.yaml"
    path.write_text("managed: {}")

    def denied(*args, **kwargs):
        raise PermissionError("private-path-and-details")

    monkeypatch.setattr(Path, "read_text", denied)
    with pytest.raises(ConfigurationError) as error:
        load_profile(path)
    assert "permission" in str(error.value).lower()
    assert "private-path-and-details" not in str(error.value)


def test_invalid_template_json_does_not_echo_input(tmp_path, monkeypatch):
    (tmp_path / "template.json").write_text('{"password": "never-echo-this"')
    path = profile_file(
        tmp_path, monkeypatch, **{"from-runtime": "", "template-file": "template.json"}
    )
    with pytest.raises(ConfigurationError) as error:
        load_profile(path)
    assert "Invalid Runtime template JSON" in str(error.value)
    assert "never-echo-this" not in str(error.value)


def test_explicit_runtime_image_is_optional_and_not_exposed(tmp_path, monkeypatch):
    profile = load_profile(profile_file(tmp_path, monkeypatch))
    assert profile.managed.runtime.image is None
    profile = load_profile(
        profile_file(
            tmp_path, monkeypatch, runtime={"image": "registry.example/mpa:v1"}
        )
    )
    assert profile.managed.runtime.image == "registry.example/mpa:v1"
    assert profile.summary()["runtimeImage"] == "registry.example/mpa:v1"


@pytest.mark.parametrize(
    "runtime",
    [
        {"image": ""},
        {"image": " "},
        {"image": "bad image"},
        {"image": "repo/<tag>"},
        {"unknown": "value"},
    ],
)
def test_invalid_runtime_image_settings_fail_locally(tmp_path, monkeypatch, runtime):
    with pytest.raises(ConfigurationError):
        load_profile(profile_file(tmp_path, monkeypatch, runtime=runtime))


def test_flat_mode_accepts_explicit_runtime_image_and_still_requires_other_fields(
    tmp_path, monkeypatch
):
    path = profile_file(
        tmp_path,
        monkeypatch,
        **{"from-runtime": "", "runtime": {"image": "registry.example/mpa:v1"}},
    )
    data = yaml.safe_load(path.read_text())
    data.update(
        {
            "pg-host": "pg.example",
            "pg-user": "app",
            "pg-password": "fake",
            "model-provider": "openai",
            "model-api-base": "https://model.example",
            "model-api-key": "fake",
            "model-name": "model",
        }
    )
    path.write_text(yaml.safe_dump(data))
    profile = load_profile(path)
    assert "image" not in profile.values
    assert profile.managed.runtime.image == "registry.example/mpa:v1"
    del data["pg-host"]
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigurationError, match="pg_host"):
        load_profile(path)


def test_runtime_environment_resolves_secret_refs_without_exposing_values(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("TEST_RUNTIME_SECRET", "private-test-value")
    profile = load_profile(
        profile_file(
            tmp_path,
            monkeypatch,
            runtime={"env": {"MODEL_AGENT_API_KEY": "${TEST_RUNTIME_SECRET}"}},
        )
    )
    assert profile.managed.runtime.env["MODEL_AGENT_API_KEY"] == "private-test-value"
    assert "private-test-value" not in str(profile.summary())
    monkeypatch.delenv("TEST_RUNTIME_SECRET")
    with pytest.raises(ConfigurationError, match="TEST_RUNTIME_SECRET"):
        load_profile(
            profile_file(
                tmp_path,
                monkeypatch,
                runtime={"env": {"MODEL_AGENT_API_KEY": "${TEST_RUNTIME_SECRET}"}},
            )
        )


@pytest.mark.parametrize(
    "settings",
    [
        {"cpu-milli": 0},
        {"memory-mb": -1},
        {"min-instance": -1},
        {"max-instance": 0},
        {"max-concurrency": 0},
        {"min-instance": 3, "max-instance": 2},
        {"env": {"NOT A KEY": "private-test-value"}},
        {"env": {"PGDATABASE": "private-test-value"}},
        {"env": {"MPA_AGENT_ID": "private-test-value"}},
        {"env": {"AGENTKIT_RUNTIME_ID": "private-test-value"}},
        {"env": {"SKILL_SPACE_ID": "private-test-value"}},
        {"env": {"AGENTKIT_TOOL_ID": "private-test-value"}},
        {"env": {"CHANNEL_STATE_ENCRYPTION_KEY": "private-test-value"}},
        {"env": {"SHARED_APIG_DATABASE_URL": "private-test-value"}},
        {"env": {"DEPLOYMENT_DATABASE_ADMIN_URL": "private-test-value"}},
    ],
)
def test_runtime_override_validation_is_safe(tmp_path, monkeypatch, settings):
    with pytest.raises(ConfigurationError) as error:
        load_profile(profile_file(tmp_path, monkeypatch, runtime=settings))
    assert "private-test-value" not in str(error.value)
