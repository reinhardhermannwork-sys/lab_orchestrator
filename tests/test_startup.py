"""M1 done-when, exercised through the real app lifespan (not just the
loader in isolation): invalid config fails fast at startup, and valid
config is queryable in-process once the app is up.
"""

import pytest
from fastapi.testclient import TestClient

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
