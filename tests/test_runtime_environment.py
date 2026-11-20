"""Runtime configuration checks use temporary files and never start services."""

import os
from pathlib import Path
from types import SimpleNamespace
import sys
import subprocess

import pytest

from app.settings import runtime_config_path, load_runtime_environment, read_runtime_config
from app.settings import validate_ports


def test_settings_schema_import_does_not_require_credentials(tmp_path):
    package = tmp_path / "app"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    source = Path(__file__).resolve().parents[1] / "app/settings.py"
    (package / "settings.py").write_bytes(source.read_bytes())
    env = {key: value for key, value in os.environ.items() if not key.startswith("CLEANSIGHT_")}
    result = subprocess.run(
        [sys.executable, "-c", "import app.settings as m; assert 'settings' not in vars(m); assert 'db_host' in m.Settings.model_fields"],
        cwd=tmp_path, env=env, check=True, capture_output=True, timeout=20,
    )
    assert not result.stdout


def test_settings_access_preserves_shared_instance():
    from app.settings import get_settings, settings
    assert get_settings() is settings


def write_runtime(root, content):
    path = runtime_config_path(root)
    path.parent.mkdir(exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_loads_only_runtime_yaml_with_native_scalars(tmp_path):
    write_runtime(tmp_path, 'port: 21000\ndebug: false\ndb_password: "a:#= value"\nffmpeg_path: null\n')
    for name in (".env", ".env.dev", ".env.prod", ".env.test"):
        (tmp_path / name).write_text("LEGACY_ONLY=forbidden\n", encoding="utf-8")
    env = {}
    load_runtime_environment(tmp_path, env)
    assert env == {"CLEANSIGHT_PORT": "21000", "CLEANSIGHT_DEBUG": "false", "CLEANSIGHT_DB_PASSWORD": "a:#= value"}


def test_process_environment_wins(tmp_path):
    write_runtime(tmp_path, "port: 21000\n")
    env = {"CLEANSIGHT_PORT": "22000"}
    load_runtime_environment(tmp_path, env)
    assert env == {"CLEANSIGHT_PORT": "22000"}


def test_missing_yaml_does_not_fall_back(tmp_path):
    (tmp_path / ".env").write_text("SECRET=old\n", encoding="utf-8")
    env = {}
    load_runtime_environment(tmp_path, env)
    assert env == {}


def test_legacy_role_variable_does_not_select_a_file(tmp_path):
    write_runtime(tmp_path, "port: 21000\n")
    (tmp_path / ".env.prod").write_text("CLEANSIGHT_PORT=22000\n", encoding="utf-8")
    env = {"CLEANSIGHT_ENV": "prod"}
    load_runtime_environment(tmp_path, env)
    assert env["CLEANSIGHT_PORT"] == "21000"


@pytest.mark.parametrize("content", ["- list\n", "port: [21000]\n", "CLEANSIGHT_PORT: 21000\n"])
def test_runtime_yaml_rejects_invalid_shape(tmp_path, content):
    path = write_runtime(tmp_path, content)
    with pytest.raises(ValueError):
        read_runtime_config(path)


def test_runtime_yaml_unknown_fields_are_rejected_before_export(tmp_path):
    write_runtime(tmp_path, "port: 21000\nmisspelled_secret: private-password\n")
    env = {}
    with pytest.raises(ValueError) as error:
        load_runtime_environment(tmp_path, env, known_fields={"port"})
    assert not env
    assert "private-password" not in str(error.value)


def test_yaml_settings_use_pydantic_validation_and_environment_priority(tmp_path, monkeypatch):
    from app.settings import Settings

    write_runtime(tmp_path, "port: 21000\ndebug: true\n")
    monkeypatch.delenv("CLEANSIGHT_DEBUG", raising=False)
    monkeypatch.setenv("CLEANSIGHT_PORT", "22000")
    env = dict(os.environ)
    load_runtime_environment(tmp_path, env, known_fields=Settings.model_fields)
    monkeypatch.setenv("CLEANSIGHT_DEBUG", env["CLEANSIGHT_DEBUG"])
    configured = Settings()
    assert configured.port == 22000 and configured.debug is True
    write_runtime(tmp_path, 'port: "invalid-number"\n')
    env = {}
    load_runtime_environment(tmp_path, env, known_fields=Settings.model_fields)
    monkeypatch.setenv("CLEANSIGHT_PORT", env["CLEANSIGHT_PORT"])
    with pytest.raises(ValueError):
        Settings()

def test_strict_mode_is_not_bypassed_by_legacy_dev_role(monkeypatch):
    from app.settings import Settings
    monkeypatch.setenv("CLEANSIGHT_ENV", "dev")
    with pytest.raises(ValueError, match="CLEANSIGHT_DB_HOST"):
        Settings(db_host="", strict=True)
    assert Settings(db_host="", strict=False).db_host == ""


def configured_ports(**overrides):
    values = dict(
        port=21000, host="127.0.0.1", mediamtx_proxy_port=21004,
        mediamtx_internal_port=21006, mediamtx_rtp_port=21002, mediamtx_rtcp_port=21003,
        db_password="must-never-appear",
    )
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.parametrize("overrides", [
    {"port": 0}, {"port": 65536}, {"mediamtx_proxy_port": 21000}, {"mediamtx_rtcp_port": 21002},
])
def test_bad_ports_fail_before_start(overrides):
    with pytest.raises(ValueError):
        validate_ports(configured_ports(**overrides))
