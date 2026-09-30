"""M1 done-when, exercised through the real app lifespan (not just the
loader in isolation): invalid config fails fast at startup, and valid
config is queryable in-process once the app is up.
"""

import logging

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from lab_orchestrator.adapters.tux2lab_client import FakeTux2LabClient, SSHTux2LabClient
from lab_orchestrator.core.config import MachineConfigError, get_settings


def test_app_startup_fails_fast_on_invalid_machines_config(tmp_path, monkeypatch):
    bad_config = tmp_path / "machines.yaml"
    bad_config.write_text("machines: {}\n")
    monkeypatch.setenv("LAB_ORCH_MACHINES_CONFIG_PATH", str(bad_config))
    get_settings.cache_clear()

    from lab_orchestrator.main import create_app

    app = create_app()
    with pytest.raises(MachineConfigError, match="defines no machines"), TestClient(app):
        pass


def test_app_startup_loads_machines_queryable_in_process():
    from lab_orchestrator.main import app

    with TestClient(app) as client:
        registry = app.state.machines
        assert registry.get_machine("machine_1").display_name == "Machine 1"
        # and the app is actually serving, not just holding state
        response = client.get("/healthz")
    assert response.status_code == 200


# --- M8: explicit tux2lab backend + logging ------------------------------


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("LAB_ORCH_DB_PATH", str(tmp_path / "test.db"))
    get_settings.cache_clear()


def test_backend_ssh_without_settings_fails_startup(isolated_db, monkeypatch):
    monkeypatch.setenv("LAB_ORCH_TUX2LAB_BACKEND", "ssh")
    for var in ("HOST", "USERNAME", "KEY_PATH"):
        monkeypatch.delenv(f"LAB_ORCH_TUX2LAB_SSH_{var}", raising=False)
    get_settings.cache_clear()

    from lab_orchestrator.main import create_app

    with (
        pytest.raises(ValueError, match="tux2lab SSH connection settings not configured"),
        TestClient(create_app()),
    ):
        pass


def test_backend_ssh_with_settings_uses_real_client(isolated_db, monkeypatch, tmp_path):
    # Construction only -- the client connects lazily, on first command.
    monkeypatch.setenv("LAB_ORCH_TUX2LAB_BACKEND", "ssh")
    monkeypatch.setenv("LAB_ORCH_TUX2LAB_SSH_HOST", "host.docker.internal")
    monkeypatch.setenv("LAB_ORCH_TUX2LAB_SSH_USERNAME", "lab-orchestrator")
    monkeypatch.setenv("LAB_ORCH_TUX2LAB_SSH_KEY_PATH", str(tmp_path / "key"))
    get_settings.cache_clear()

    from lab_orchestrator.main import make_tux2lab_client

    assert isinstance(make_tux2lab_client(get_settings()), SSHTux2LabClient)


def test_backend_fake_ignores_ssh_settings(isolated_db, monkeypatch, tmp_path):
    monkeypatch.setenv("LAB_ORCH_TUX2LAB_BACKEND", "fake")
    monkeypatch.setenv("LAB_ORCH_TUX2LAB_SSH_HOST", "host.docker.internal")
    monkeypatch.setenv("LAB_ORCH_TUX2LAB_SSH_USERNAME", "lab-orchestrator")
    monkeypatch.setenv("LAB_ORCH_TUX2LAB_SSH_KEY_PATH", str(tmp_path / "key"))
    get_settings.cache_clear()

    from lab_orchestrator.main import create_app

    app = create_app()
    with TestClient(app):
        assert isinstance(app.state.tux2lab, FakeTux2LabClient)


def test_invalid_backend_is_a_config_error(monkeypatch):
    monkeypatch.setenv("LAB_ORCH_TUX2LAB_BACKEND", "libvirt")
    get_settings.cache_clear()
    with pytest.raises(ValidationError):
        get_settings()


def test_package_logs_reach_stdout_at_configured_level(capsys, monkeypatch):
    from lab_orchestrator.main import configure_logging

    # Earlier tests' app lifespans already attached a handler bound to
    # *their* captured stdout; start clean so it binds to this test's.
    package_logger = logging.getLogger("lab_orchestrator")
    monkeypatch.setattr(package_logger, "handlers", [])
    monkeypatch.setattr(package_logger, "level", package_logger.level)

    configure_logging("INFO")
    configure_logging("INFO")  # idempotent: no duplicate handler
    logging.getLogger("lab_orchestrator.core.janitor").info("janitor says hi")
    logging.getLogger("lab_orchestrator.core.janitor").debug("too quiet to show")

    out = capsys.readouterr().out
    assert out.count("janitor says hi") == 1
    assert "INFO lab_orchestrator.core.janitor: janitor says hi" in out
    assert "too quiet to show" not in out
