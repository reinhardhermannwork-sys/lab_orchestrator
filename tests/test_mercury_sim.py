"""Tests for M13 step SP1 (architecture doc §18): providers, the Mercury
simulation lease, runs inside a lease, the watchdog, and per-provider
reconcile and limits -- all against FakeMercurySimProvider.
"""

from __future__ import annotations

import json
import time
from datetime import timedelta

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from ulid import ULID

from lab_orchestrator import naming
from lab_orchestrator.adapters.mercury_sim import FakeMercurySimProvider
from lab_orchestrator.adapters.provider import (
    MAX_SETUP_BYTES,
    InvalidIdentifierError,
    NotSupportedError,
    ProviderConnectionError,
    ProviderRegistry,
    RunRefusedError,
)
from lab_orchestrator.adapters.tux2lab_client import FakeTux2LabClient
from lab_orchestrator.core import instance_manager, janitor
from lab_orchestrator.core.config import (
    MachineConfigError,
    Settings,
    get_settings,
    load_machine_definitions,
)
from lab_orchestrator.core.reconcile import reconcile_on_startup
from lab_orchestrator.core.state_machine import InstanceState
from lab_orchestrator.db.database import create_db_engine, get_engine
from lab_orchestrator.db.init_db import init_db, sync_machine_definitions
from lab_orchestrator.db.models import instances, runs, utcnow

REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
VM_CONFIG = REPO_ROOT / "config" / "machines.yaml"
SIM_CONFIG = REPO_ROOT / "config" / "machines.sim.yaml"

SIM_ENTRY = SIM_CONFIG.read_text().split("machines:\n", 1)[1]


@pytest.fixture
def both_config(tmp_path):
    """machines.yaml's three VMs plus the simulated Mercury."""
    path = tmp_path / "machines.yaml"
    path.write_text(VM_CONFIG.read_text() + "\n" + SIM_ENTRY)
    return path


@pytest.fixture
def machines(both_config):
    return load_machine_definitions(both_config)


@pytest.fixture
def engine(tmp_path, machines):
    eng = create_db_engine(tmp_path / "test.db")
    init_db(eng)
    sync_machine_definitions(eng, machines)
    yield eng
    eng.dispose()


@pytest.fixture
def sim():
    return FakeMercurySimProvider()


@pytest.fixture
def vms():
    return FakeTux2LabClient()


@pytest.fixture
def providers(sim, vms):
    return ProviderRegistry({"tux2lab": vms, "mercury-sim": sim})


@pytest.fixture
def settings():
    return Settings(provisioning_poll_interval_seconds=0.0, provisioning_timeout_seconds=2.0)


@pytest.fixture
def probes(monkeypatch):
    """Every readiness probe as (host, port); all succeed."""
    seen: list[tuple[str, int]] = []

    async def _open(host, port, timeout=3.0):
        seen.append((host, port))
        return True

    monkeypatch.setattr(instance_manager, "_tcp_port_open", _open)
    return seen


def row_of(engine, instance_id):
    with engine.connect() as conn:
        return dict(
            conn.execute(sa.select(instances).where(instances.c.id == instance_id)).mappings().one()
        )


def runs_of(engine, instance_id):
    with engine.connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                sa.select(runs).where(runs.c.instance_id == instance_id).order_by(runs.c.id)
            ).mappings()
        ]


async def ready_lease(engine, machines, providers, settings, *, user="hermann", setup=None):
    created = await instance_manager.create_instance(
        engine=engine,
        machines=machines,
        settings=settings,
        user_id=user,
        machine_type="mercsc0000",
        case_id="case-42",
        setup=setup,
        providers=providers,
    )
    await instance_manager.provision_instance(
        engine=engine,
        machines=machines,
        providers=providers,
        settings=settings,
        instance_id=created["id"],
    )
    return row_of(engine, created["id"])


# --- config ------------------------------------------------------------------


def test_sim_machine_config_loads():
    m = load_machine_definitions(SIM_CONFIG).get_machine("mercsc0000")
    assert m.provider == "mercury-sim"
    assert m.image == m.tux2lab_image == "mercsc0000-v1.2"
    assert m.protocol == "vnc"
    assert m.probe_port == 5900
    assert m.lease_lifetime_hours(Settings(lease_lifetime_hours=1.0)) == 4


def test_vm_machines_keep_their_defaults():
    m = load_machine_definitions(VM_CONFIG).get_machine("machine_1")
    assert m.provider == "tux2lab"
    assert m.image == "image_1_software_1"
    assert m.probe_port == 22
    assert m.lease_lifetime_hours(Settings(lease_lifetime_hours=1.5)) == 1.5


@pytest.mark.parametrize(
    "extra",
    [
        '    image: "a"\n    tux2lab_image: "a"\n',  # both names
        '    image: "a"\n    provider: "libvirt"\n',  # unknown provider
        '    image: "a"\n    probe_port: 0\n',
    ],
)
def test_bad_machine_entries_are_config_errors(tmp_path, extra):
    path = tmp_path / "machines.yaml"
    path.write_text(
        'machines:\n  m:\n    display_name: "M"\n    code: "m01"\n    codename: "x"\n'
        '    protocol: "ssh"\n' + extra
    )
    with pytest.raises(MachineConfigError):
        load_machine_definitions(path)


# --- DB: limits per provider, upgrade of an older DB ------------------------------


def _insert(engine, instance_id, *, provider, user=None, machine_type=None):
    now = utcnow()
    with engine.begin() as conn:
        conn.execute(
            instances.insert().values(
                id=instance_id,
                user_id=user or f"user-{instance_id}",
                machine_type=machine_type or ("mercsc0000" if provider == "mercury-sim" else "machine_1"),
                provider=provider,
                state=InstanceState.READY.value,
                created_at=now,
                expires_at=now + timedelta(hours=1),
            )
        )


def test_limits_are_counted_per_provider(engine):
    _insert(engine, "s1", provider="mercury-sim")
    with pytest.raises(IntegrityError, match=r"global active-instance quota \(1\) exceeded for mercury-sim"):
        _insert(engine, "s2", provider="mercury-sim")
    # The full simulation slot does not block VMs, up to their own limit.
    for i in range(3):
        _insert(engine, f"v{i}", provider="tux2lab")
    with pytest.raises(IntegrityError, match=r"global active-instance quota \(3\) exceeded for tux2lab"):
        _insert(engine, "v3", provider="tux2lab")


def test_settings_carry_both_limits(monkeypatch):
    monkeypatch.setenv("LAB_ORCH_MAX_ACTIVE_INSTANCES", "2")
    monkeypatch.setenv("LAB_ORCH_MAX_ACTIVE_MERCURY_SIM", "4")
    assert Settings(_env_file=None).provider_limits() == {"tux2lab": 2, "mercury-sim": 4}


def test_init_db_upgrades_a_pre_m13_database(tmp_path, machines):
    eng = create_db_engine(tmp_path / "old.db")
    with eng.begin() as conn:
        # The instances table and trigger as M10 created them.
        conn.exec_driver_sql(
            "CREATE TABLE machine_definitions (id VARCHAR PRIMARY KEY, display_name VARCHAR NOT NULL, "
            "code VARCHAR NOT NULL UNIQUE, codename VARCHAR NOT NULL UNIQUE, tux2lab_image VARCHAR NOT NULL, "
            "protocol VARCHAR NOT NULL, enabled BOOLEAN NOT NULL)"
        )
        conn.exec_driver_sql(
            "INSERT INTO machine_definitions VALUES ('machine_1', 'M1', 'm01', 'aurora', 'img', 'ssh', 1)"
        )
        conn.exec_driver_sql(
            "CREATE TABLE instances (id VARCHAR PRIMARY KEY, user_id VARCHAR NOT NULL, "
            "machine_type VARCHAR NOT NULL REFERENCES machine_definitions(id), vm_hostname VARCHAR, "
            "vm_ip VARCHAR, state VARCHAR NOT NULL, created_at DATETIME NOT NULL, ready_at DATETIME, "
            "expires_at DATETIME NOT NULL, disconnect_since DATETIME, guacamole_connection_id VARCHAR, "
            "destroyed_at DATETIME, failure_reason TEXT)"
        )
        conn.exec_driver_sql(
            "INSERT INTO instances (id, user_id, machine_type, state, created_at, expires_at) "
            "VALUES ('old', 'u', 'machine_1', 'READY', '2026-10-01 00:00:00', '2026-10-01 04:00:00')"
        )
        conn.exec_driver_sql(
            "CREATE TRIGGER trg_instances_max_global_active BEFORE INSERT ON instances "
            "BEGIN SELECT RAISE(ABORT, 'old trigger'); END;"
        )

    init_db(eng, provider_limits={"tux2lab": 3, "mercury-sim": 1})
    sync_machine_definitions(eng, machines)

    assert row_of(eng, "old")["provider"] == "tux2lab"
    with eng.connect() as conn:
        triggers = set(
            conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'trigger'").scalars()
        )
    assert triggers == {"trg_instances_max_active_tux2lab", "trg_instances_max_active_mercury_sim"}
    _insert(eng, "new", provider="mercury-sim")  # the old trigger is gone
    init_db(eng)  # idempotent
    eng.dispose()


# --- the provider interface ---------------------------------------------------


async def test_setup_is_checked_before_it_crosses_the_boundary(sim, vms):
    with pytest.raises(InvalidIdentifierError):
        await sim.install("lab-mrc-mercury-aaaaa", "mercsc0000-v1.2", ["not", "an", "object"])
    with pytest.raises(InvalidIdentifierError):
        await sim.install("lab-mrc-mercury-aaaaa", "mercsc0000-v1.2", {"x": "y" * MAX_SETUP_BYTES})
    with pytest.raises(NotSupportedError):
        await vms.install("lab-m01-aurora-aaaaa", "image_1_software_1", {"x": 1})
    await sim.install("lab-mrc-mercury-aaaaa", "mercsc0000-v1.2", {"b": 1, "a": [2]})
    assert sim.setups["lab-mrc-mercury-aaaaa"] == '{"a":[2],"b":1}'


async def test_run_arguments_are_checked(sim, vms):
    with pytest.raises(InvalidIdentifierError):
        await sim.run("lab-mrc-mercury-aaaaa", str(ULID()), "WASH; rm -rf /")
    with pytest.raises(InvalidIdentifierError):
        await sim.run("lab-mrc-mercury-aaaaa", "not-a-ulid", "WASHDOWN")
    with pytest.raises(NotSupportedError):
        await vms.run("lab-m01-aurora-aaaaa", str(ULID()), "WASHDOWN")


async def test_fake_sim_lifecycle_and_runs(sim):
    host = "lab-mrc-mercury-aaaaa"
    await sim.install(host, "mercsc0000-v1.2")
    assert (await sim.info(host)).vm_state == "created"
    with pytest.raises(RunRefusedError):
        await sim.run(host, str(ULID()), "WASHDOWN")  # not online yet
    await sim.start(host)
    info = await sim.info(host)
    assert (info.vm_state, info.os_state, info.ip_address) == ("running", "healthy", host)

    first = str(ULID())
    await sim.run(host, first, "WASHDOWN")
    with pytest.raises(RunRefusedError):
        await sim.run(host, str(ULID()), "WASHDOWN")
    assert [r.state for r in await sim.runs(host)] == ["running"]
    done = (await sim.runs(host))[0]
    assert (done.state, done.result) == ("done", "PROCESS_DONE")
    assert done.blobs
    await sim.run(host, str(ULID()), "WASHDOWN")  # free again
    await sim.remove(host)


# --- a simulation lease through the state machine --------------------------------


async def test_sim_lease_reaches_ready_with_setup_case_and_vnc_probe(
    engine, machines, providers, settings, sim, probes
):
    sim.starting_polls = 2  # SECS host not online for the first two polls
    row = await ready_lease(engine, machines, providers, settings, setup={"plant": {"di_temp": 25}})

    assert row["state"] == InstanceState.READY.value
    assert row["provider"] == "mercury-sim"
    assert row["case_id"] == "case-42"
    assert json.loads(row["setup_json"]) == {"plant": {"di_temp": 25}}
    assert row["vm_hostname"].startswith("lab-mrc-mercury-")
    assert sim.images[row["vm_hostname"]] == "mercsc0000-v1.2"
    assert json.loads(sim.setups[row["vm_hostname"]]) == {"plant": {"di_temp": 25}}
    # Probed on the HMI's VNC port, and only once the SECS host was online.
    assert probes == [(row["vm_hostname"], 5900)]
    # The machine's own lifetime (4 h), not the settings default.
    assert row["expires_at"] - row["created_at"] == timedelta(hours=4)


async def test_setup_for_a_vm_is_refused_before_any_work(engine, machines, providers, settings):
    with pytest.raises(instance_manager.InvalidSetupError):
        await instance_manager.create_instance(
            engine=engine,
            machines=machines,
            settings=settings,
            user_id="hermann",
            machine_type="machine_1",
            setup={"x": 1},
            providers=providers,
        )
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(instances)).scalar() == 0


async def test_sim_lease_that_never_gets_online_fails(engine, machines, providers, settings, sim, probes):
    sim.starting_polls = 10**6
    settings.provisioning_timeout_seconds = 0.05
    row = await ready_lease(engine, machines, providers, settings)
    assert row["state"] == InstanceState.DESTROYED.value
    assert "os_state='starting'" in row["failure_reason"]
    assert "tcp_5900_reachable" in row["failure_reason"]
    assert await sim.list() == []


# --- runs ------------------------------------------------------------------------------


async def test_runs_one_after_another(engine, machines, providers, settings, sim, probes):
    sim.auto_advance = False
    lease = await ready_lease(engine, machines, providers, settings)
    host = lease["vm_hostname"]

    run = await instance_manager.start_run(
        engine=engine, providers=providers, instance_id=lease["id"], user_id="hermann", recipe="WASHDOWN"
    )
    assert run["state"] == "queued"
    with pytest.raises(instance_manager.InstanceNotRunnableError, match="still going"):
        await instance_manager.start_run(
            engine=engine, providers=providers, instance_id=lease["id"], user_id="hermann", recipe="WASHDOWN"
        )

    sim.set_run(host, run["id"], state="running", started_at="2026-10-31T10:00:00+00:00")
    listed = await instance_manager.list_runs(
        engine=engine, providers=providers, instance_id=lease["id"], user_id="hermann"
    )
    assert [(r["state"], r["started_at"]) for r in listed] == [("running", "2026-10-31T10:00:00+00:00")]

    sim.set_run(
        host, run["id"], state="done", ended_at="2026-10-31T10:06:00+00:00", result="PROCESS_DONE",
        blobs=["runs/x/blob.bin"],
    )
    # The janitor syncs too, without anybody reading.
    await janitor.run_once(engine=engine, providers=providers, settings=settings)
    done = runs_of(engine, lease["id"])[0]
    assert (done["state"], done["result"], json.loads(done["blobs_json"])) == (
        "done", "PROCESS_DONE", ["runs/x/blob.bin"],
    )

    second = await instance_manager.start_run(
        engine=engine, providers=providers, instance_id=lease["id"], user_id="hermann", recipe="HF_ETCH"
    )
    assert [r["id"] for r in runs_of(engine, lease["id"])] == [run["id"], second["id"]]


async def test_run_refused_by_the_host_is_recorded(engine, machines, providers, settings, sim, probes):
    lease = await ready_lease(engine, machines, providers, settings)
    sim.raise_once = RunRefusedError("Mercury busy", exit_status=1)
    with pytest.raises(instance_manager.InstanceNotRunnableError, match="Mercury busy"):
        await instance_manager.start_run(
            engine=engine, providers=providers, instance_id=lease["id"], user_id="hermann", recipe="WASHDOWN"
        )
    (failed,) = runs_of(engine, lease["id"])
    assert failed["state"] == "failed" and "Mercury busy" in failed["result"]


async def test_run_rules(engine, machines, providers, settings, sim, vms, probes):
    lease = await ready_lease(engine, machines, providers, settings)
    with pytest.raises(instance_manager.InstanceNotFoundError):
        await instance_manager.start_run(
            engine=engine, providers=providers, instance_id=lease["id"], user_id="someone", recipe="WASHDOWN"
        )
    with pytest.raises(instance_manager.InvalidRecipeError):
        await instance_manager.start_run(
            engine=engine, providers=providers, instance_id=lease["id"], user_id="hermann", recipe="a b"
        )
    await instance_manager.release_instance(engine=engine, instance_id=lease["id"], user_id="hermann")
    with pytest.raises(instance_manager.InstanceNotRunnableError, match="DESTROYING"):
        await instance_manager.start_run(
            engine=engine, providers=providers, instance_id=lease["id"], user_id="hermann", recipe="WASHDOWN"
        )

    vm = await instance_manager.create_instance(
        engine=engine, machines=machines, settings=settings, user_id="other", machine_type="machine_1"
    )
    with pytest.raises(instance_manager.RunsNotSupportedError):
        await instance_manager.start_run(
            engine=engine, providers=providers, instance_id=vm["id"], user_id="other", recipe="WASHDOWN"
        )


# --- watchdog --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fault", "reason"),
    [
        ("link_lost", "SECS link lost"),
        ("mercury_gone", "machine stopped (vm_state='exited')"),
        ("vanished", "machine gone (not on the host)"),
    ],
)
async def test_watchdog_fails_a_broken_lease_and_its_run(
    engine, machines, providers, settings, sim, probes, fault, reason
):
    sim.auto_advance = False
    lease = await ready_lease(engine, machines, providers, settings)
    host = lease["vm_hostname"]
    await instance_manager.start_run(
        engine=engine, providers=providers, instance_id=lease["id"], user_id="hermann", recipe="WASHDOWN"
    )
    if fault == "vanished":
        await sim.remove(host)
    else:
        getattr(sim, fault)(host)

    # One pass: FAILED -> CLEANUP -> DESTROYED, the run ended with the reason.
    assert await janitor.run_once(engine=engine, providers=providers, settings=settings) == 1
    row = row_of(engine, lease["id"])
    assert row["state"] == InstanceState.DESTROYED.value
    assert row["failure_reason"] == reason
    (run,) = runs_of(engine, lease["id"])
    assert run["state"] == "failed" and reason in run["result"]
    assert await sim.list() == []
    # The quota slot is free at once.
    await ready_lease(engine, machines, providers, settings, user="next")


async def test_watchdog_leaves_healthy_and_unreachable_leases_alone(
    engine, machines, providers, settings, sim, probes
):
    lease = await ready_lease(engine, machines, providers, settings)
    assert await janitor.watchdog_pass(engine=engine, providers=providers) == []
    sim.raise_once = ProviderConnectionError("host down")
    assert await janitor.watchdog_pass(engine=engine, providers=providers) == []
    assert row_of(engine, lease["id"])["state"] == InstanceState.READY.value


async def test_watchdog_does_not_touch_vm_leases(engine, machines, providers, settings, vms, monkeypatch):
    async def _open(host, port, timeout=3.0):
        return True

    monkeypatch.setattr(instance_manager, "_tcp_port_open", _open)
    created = await instance_manager.create_instance(
        engine=engine, machines=machines, settings=settings, user_id="hermann", machine_type="machine_1"
    )
    await instance_manager.provision_instance(
        engine=engine, machines=machines, providers=providers, settings=settings, instance_id=created["id"]
    )
    await vms.remove(row_of(engine, created["id"])["vm_hostname"])
    assert await janitor.watchdog_pass(engine=engine, providers=providers) == []
    assert row_of(engine, created["id"])["state"] == InstanceState.READY.value  # as before M13


# --- reconcile per provider ---------------------------------------------------------------


async def test_reconcile_lists_each_provider(engine, machines, providers, settings, sim, vms, probes):
    lease = await ready_lease(engine, machines, providers, settings)
    await sim.install("lab-mrc-mercury-zzzzz", "mercsc0000-v1.2")  # nobody's
    await vms.install("lab-m01-aurora-zzzzz", "image_1_software_1")  # nobody's
    vms.raise_once = ProviderConnectionError("VPS down")

    report = await reconcile_on_startup(engine=engine, providers=providers)

    assert report.failed_providers == ["tux2lab"]
    assert report.unknown_vms == ["lab-mrc-mercury-zzzzz"]  # tux2lab's were not listed
    assert report.missing_vms == []
    assert lease["vm_hostname"] in {vm.hostname for vm in await sim.list()}


# --- through the API -----------------------------------------------------------------------


@pytest.fixture
def api_env(monkeypatch, tmp_path, both_config):
    monkeypatch.setenv("LAB_ORCH_MACHINES_CONFIG_PATH", str(both_config))
    monkeypatch.setenv("LAB_ORCH_PROVISIONING_POLL_INTERVAL_SECONDS", "0.01")
    monkeypatch.setenv("LAB_ORCH_PROVISIONING_TIMEOUT_SECONDS", "2")
    monkeypatch.setenv("LAB_ORCH_DB_PATH", str(tmp_path / "api.db"))
    monkeypatch.setenv("LAB_ORCH_TUX2LAB_BACKEND", "fake")
    get_settings.cache_clear()
    get_engine.cache_clear()


def _wait_for(client, path, predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while True:
        body = client.get(path).json()
        if predicate(body) or time.monotonic() > deadline:
            return body
        time.sleep(0.01)


def test_sim_walkthrough_through_the_api(api_env):
    """SP4's curl walkthrough, against the fake: lease -> READY -> run ->
    done -> second run -> release -> DESTROYED."""
    from lab_orchestrator.main import create_app

    app = create_app()
    with TestClient(app) as client:
        assert isinstance(app.state.providers.get("mercury-sim"), FakeMercurySimProvider)
        created = client.post(
            "/v1/instances",
            json={
                "user": "hermann",
                "machine_type": "mercsc0000",
                "case_id": "case-42",
                "setup": {"nvm": {"HEATER_SP": 30}},
            },
        )
        assert created.status_code == 202, created.text
        lease_id = created.json()["instance_id"]
        lease = _wait_for(client, f"/v1/instances/{lease_id}", lambda b: b["state"] == "READY")
        assert lease["state"] == "READY"
        assert (lease["provider"], lease["protocol"], lease["port"], lease["case_id"]) == (
            "mercury-sim", "vnc", 5900, "case-42",
        )
        assert lease["ssh"] is None
        assert lease["hostname"].startswith("lab-mrc-mercury-")

        runs_path = f"/v1/instances/{lease_id}/runs"
        first = client.post(runs_path, json={"user": "hermann", "recipe": "WASHDOWN"})
        assert first.status_code == 202, first.text
        assert first.json()["state"] == "queued"
        assert client.post(runs_path, json={"user": "hermann", "recipe": "WASHDOWN"}).status_code == 409
        listed = _wait_for(client, runs_path, lambda b: b[0]["state"] == "done")
        assert listed[0]["state"] == "done" and listed[0]["blobs"]

        second = client.post(runs_path, json={"user": "hermann", "recipe": "WASHDOWN"})
        assert second.status_code == 202
        assert client.get(runs_path + "?user=someone").status_code == 404
        assert client.post(runs_path, json={"user": "someone", "recipe": "X"}).status_code == 404

        released = client.delete(f"/v1/instances/{lease_id}?user=hermann")
        assert released.status_code == 202
        client.portal.call(
            lambda: janitor.run_once(
                engine=app.state.db_engine, providers=app.state.providers, settings=app.state.settings
            )
        )
        assert client.get(f"/v1/instances/{lease_id}").json()["state"] == "DESTROYED"
        final = client.get(runs_path).json()
        assert [r["state"] for r in final] == ["done", "failed"]
        assert "lease ended" in final[1]["result"]


def test_api_refuses_setup_and_runs_for_vms(api_env, monkeypatch):
    async def _noop_provision(**kwargs):
        return None

    monkeypatch.setattr(instance_manager, "provision_instance", _noop_provision)
    from lab_orchestrator.main import create_app

    with TestClient(create_app()) as client:
        bad = client.post(
            "/v1/instances", json={"user": "a", "machine_type": "machine_1", "setup": {"x": 1}}
        )
        assert bad.status_code == 400
        vm = client.post("/v1/instances", json={"user": "a", "machine_type": "machine_1"}).json()
        runs_path = f"/v1/instances/{vm['instance_id']}/runs"
        assert client.post(runs_path, json={"user": "a", "recipe": "WASHDOWN"}).status_code == 400
        assert client.get(runs_path).json() == []
        got = client.get(f"/v1/instances/{vm['instance_id']}").json()
        assert (got["provider"], got["protocol"], got["port"]) == ("tux2lab", "ssh", None)


def test_only_providers_in_use_get_a_client(api_env, monkeypatch):
    monkeypatch.setenv("LAB_ORCH_MACHINES_CONFIG_PATH", str(SIM_CONFIG))
    get_settings.cache_clear()
    from lab_orchestrator.main import create_app

    app = create_app()
    with TestClient(app):
        assert "mercury-sim" in app.state.providers
        assert "tux2lab" not in app.state.providers


def test_hostnames_for_the_sim_machine_pass_the_check():
    machine = load_machine_definitions(SIM_CONFIG).get_machine("mercsc0000")
    from lab_orchestrator.adapters.provider import validate_hostname

    for _ in range(50):
        validate_hostname(naming.generate_hostname(machine))
