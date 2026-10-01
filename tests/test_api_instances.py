"""Integration tests for POST/GET /v1/instances (M5), through the real
app and its real lifespan wiring -- not just instance_manager in
isolation. `test_full_workflow_reaches_ready` is the literal M5
done-when: architecture doc §8's curl workflow, reproduced against
TestClient with the fake Tux2LabClient main.py wires in by default.
"""

from __future__ import annotations

import re
import time

import pytest
from fastapi.testclient import TestClient

from lab_orchestrator.core import instance_manager
from lab_orchestrator.core.config import get_settings
from lab_orchestrator.db.database import get_engine


@pytest.fixture
def fast_settings(monkeypatch, tmp_path):
    """Short poll interval/timeout so tests that let provisioning run for
    real don't make the suite slow, and a per-test temp DB file -- without
    this, every test in this file would silently share the same real
    orchestrator.db, leaking rows across tests (caught by running these
    tests for real: the first test to insert a "hermann" row made every
    later test see it as still active).
    """
    monkeypatch.setenv("LAB_ORCH_PROVISIONING_POLL_INTERVAL_SECONDS", "0.01")
    monkeypatch.setenv("LAB_ORCH_PROVISIONING_TIMEOUT_SECONDS", "2")
    monkeypatch.setenv("LAB_ORCH_DB_PATH", str(tmp_path / "test.db"))
    get_settings.cache_clear()
    get_engine.cache_clear()


def test_get_unknown_instance_returns_404(fast_settings):
    from lab_orchestrator.main import create_app

    with TestClient(create_app()) as client:
        response = client.get("/v1/instances/does-not-exist")
    assert response.status_code == 404


def test_post_unknown_machine_type_returns_404(fast_settings):
    from lab_orchestrator.main import create_app

    with TestClient(create_app()) as client:
        response = client.post("/v1/instances", json={"user": "hermann", "machine_type": "nope"})
    assert response.status_code == 404


def test_post_disabled_machine_returns_400(fast_settings, monkeypatch, tmp_path):
    disabled_config = tmp_path / "machines.yaml"
    real_config = __import__("pathlib").Path("config/machines.yaml")
    disabled_config.write_text(real_config.read_text().replace("enabled: true", "enabled: false"))
    monkeypatch.setenv("LAB_ORCH_MACHINES_CONFIG_PATH", str(disabled_config))

    from lab_orchestrator.main import create_app

    with TestClient(create_app()) as client:
        response = client.post(
            "/v1/instances", json={"user": "hermann", "machine_type": "machine_1"}
        )
    assert response.status_code == 400


def test_post_returns_202_with_expected_shape(fast_settings):
    from lab_orchestrator.main import create_app

    with TestClient(create_app()) as client:
        response = client.post(
            "/v1/instances", json={"user": "hermann", "machine_type": "machine_1"}
        )
    assert response.status_code == 202
    body = response.json()
    assert body["machine_type"] == "machine_1"
    assert body["state"] == "PROVISIONING"
    assert isinstance(body["instance_id"], str) and len(body["instance_id"]) == 26  # ULID


def test_post_second_request_same_user_returns_409(fast_settings, monkeypatch):
    # Deterministic, not timing-dependent: stub provisioning to a no-op
    # so the first instance stays REQUESTED (active) indefinitely,
    # rather than racing the real background task toward DESTROYED.
    async def _noop_provision(**kwargs):
        return None

    monkeypatch.setattr(instance_manager, "provision_instance", _noop_provision)

    from lab_orchestrator.main import create_app

    with TestClient(create_app()) as client:
        first = client.post(
            "/v1/instances", json={"user": "hermann", "machine_type": "machine_1"}
        )
        assert first.status_code == 202
        second = client.post(
            "/v1/instances", json={"user": "hermann", "machine_type": "machine_2"}
        )
    assert second.status_code == 409


def test_full_workflow_reaches_ready(fast_settings, monkeypatch):
    """The literal M5 done-when: architecture doc §8's
    POST -> poll -> (ssh) curl workflow, working end-to-end against the
    fake Tux2LabClient. Only the TCP/22 check is stubbed (the fake's IP
    isn't reachable from the sandbox) -- naming and everything else is the
    real, unmodified app.
    """

    async def _always_reachable(host, port, timeout=3.0):
        return True

    monkeypatch.setattr(instance_manager, "_tcp_port_open", _always_reachable)

    from lab_orchestrator.main import create_app

    with TestClient(create_app()) as client:
        post_response = client.post(
            "/v1/instances", json={"user": "hermann", "machine_type": "machine_1"}
        )
        assert post_response.status_code == 202
        instance_id = post_response.json()["instance_id"]
        assert post_response.json()["state"] == "PROVISIONING"

        # Poll, the way a real client would (architecture doc §8) --
        # not asserting READY on the first GET, since provisioning is
        # genuinely asynchronous.
        deadline = time.monotonic() + 5
        get_response = None
        while time.monotonic() < deadline:
            get_response = client.get(f"/v1/instances/{instance_id}")
            assert get_response.status_code == 200
            if get_response.json()["state"] == "READY":
                break
            time.sleep(0.05)

    assert get_response is not None
    body = get_response.json()
    assert body["state"] == "READY"
    assert re.fullmatch(r"lab-m01-aurora-[0-9a-z]{5}", body["hostname"])
    assert "hermann" not in body["hostname"]
    assert body["ip"] == "10.28.28.100"
    assert body["ssh"] == {"username": "labuser", "port": 22}
    assert body["machine_name"] == "Machine 1"


# --- M9: machine list, per-user list, early release ------------------------


def _wait_for_state(client, instance_id, state, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/v1/instances/{instance_id}").json()
        if body["state"] == state:
            return body
        time.sleep(0.05)
    raise AssertionError(f"{instance_id} never reached {state}")


@pytest.fixture
def reachable(monkeypatch):
    async def _always_reachable(host, port, timeout=3.0):
        return True

    monkeypatch.setattr(instance_manager, "_tcp_port_open", _always_reachable)


def test_get_machines_lists_enabled_machines(fast_settings, monkeypatch, tmp_path):
    import yaml

    data = yaml.safe_load(__import__("pathlib").Path("config/machines.yaml").read_text())
    data["machines"]["machine_2"]["enabled"] = False
    config = tmp_path / "machines.yaml"
    config.write_text(yaml.safe_dump(data))
    monkeypatch.setenv("LAB_ORCH_MACHINES_CONFIG_PATH", str(config))
    get_settings.cache_clear()

    from lab_orchestrator.main import create_app

    with TestClient(create_app()) as client:
        response = client.get("/v1/machines")
    assert response.status_code == 200
    machine_types = [m["machine_type"] for m in response.json()]
    assert "machine_1" in machine_types
    assert "machine_2" not in machine_types
    assert {"machine_type": "machine_1", "display_name": "Machine 1"} in response.json()


def test_list_instances_by_user(fast_settings, reachable):
    from lab_orchestrator.main import create_app

    with TestClient(create_app()) as client:
        assert client.get("/v1/instances", params={"user": "hermann"}).json() == []
        created = client.post(
            "/v1/instances", json={"user": "hermann", "machine_type": "machine_1"}
        ).json()
        _wait_for_state(client, created["instance_id"], "READY")

        mine = client.get("/v1/instances", params={"user": "hermann"}).json()
        theirs = client.get("/v1/instances", params={"user": "someone-else"}).json()

    assert [i["instance_id"] for i in mine] == [created["instance_id"]]
    assert mine[0]["state"] == "READY"
    assert theirs == []


def test_list_instances_requires_user(fast_settings):
    from lab_orchestrator.main import create_app

    with TestClient(create_app()) as client:
        assert client.get("/v1/instances").status_code == 422
        assert client.get("/v1/instances", params={"user": ""}).status_code == 422


def test_instance_response_has_utc_expiry_and_failure_reason(fast_settings, reachable):
    from lab_orchestrator.main import create_app

    with TestClient(create_app()) as client:
        created = client.post(
            "/v1/instances", json={"user": "hermann", "machine_type": "machine_1"}
        ).json()
        body = client.get(f"/v1/instances/{created['instance_id']}").json()
    assert body["expires_at"].endswith(("+00:00", "Z"))
    assert body["failure_reason"] is None


def test_release_by_owner_then_janitor_destroys_and_frees_quota(fast_settings, reachable):
    from lab_orchestrator.core import janitor
    from lab_orchestrator.main import create_app

    app = create_app()
    with TestClient(app) as client:
        created = client.post(
            "/v1/instances", json={"user": "hermann", "machine_type": "machine_1"}
        ).json()
        instance_id = created["instance_id"]
        _wait_for_state(client, instance_id, "READY")

        released = client.delete(f"/v1/instances/{instance_id}", params={"user": "hermann"})
        assert released.status_code == 202
        assert released.json()["state"] == "DESTROYING"

        # Releasing again is a no-op, not an error.
        again = client.delete(f"/v1/instances/{instance_id}", params={"user": "hermann"})
        assert again.status_code == 202

        destroyed = client.portal.call(
            lambda: janitor.run_once(
                engine=app.state.db_engine, tux2lab=app.state.tux2lab, settings=app.state.settings
            )
        )
        assert destroyed == 1
        assert client.get(f"/v1/instances/{instance_id}").json()["state"] == "DESTROYED"
        assert client.portal.call(app.state.tux2lab.list) == []

        # The user's quota slot is free again.
        assert client.get("/v1/instances", params={"user": "hermann"}).json() == []
        new = client.post(
            "/v1/instances", json={"user": "hermann", "machine_type": "machine_1"}
        )
        assert new.status_code == 202


def test_release_by_other_user_or_unknown_id_is_404(fast_settings, reachable):
    from lab_orchestrator.main import create_app

    with TestClient(create_app()) as client:
        created = client.post(
            "/v1/instances", json={"user": "hermann", "machine_type": "machine_1"}
        ).json()
        instance_id = created["instance_id"]

        other = client.delete(f"/v1/instances/{instance_id}", params={"user": "mallory"})
        unknown = client.delete("/v1/instances/does-not-exist", params={"user": "hermann"})
        missing_user = client.delete(f"/v1/instances/{instance_id}")
        state_after = client.get(f"/v1/instances/{instance_id}").json()["state"]

    assert other.status_code == 404
    assert unknown.status_code == 404
    assert other.json() == unknown.json()  # doesn't reveal that the id exists
    assert missing_user.status_code == 422
    assert state_after != "DESTROYING"
