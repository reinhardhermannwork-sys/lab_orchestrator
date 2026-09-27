"""Tests for core/instance_manager.py (M5).

naming.py is still blocked (architecture doc §5) — every test here that
needs provisioning to actually proceed past PROVISIONING monkeypatches
`lab_orchestrator.naming.generate_hostname` with a stand-in, via the
module reference (not a rebound import), matching how instance_manager
itself calls it. A couple of tests deliberately *don't* patch it, to
prove the real, still-blocked function fails the way M5's error handling
expects it to.
"""

from __future__ import annotations

import itertools

import pytest
import sqlalchemy as sa

from lab_orchestrator import naming
from lab_orchestrator.adapters.tux2lab_client import FakeTux2LabClient, Tux2LabCommandError
from lab_orchestrator.core import instance_manager
from lab_orchestrator.core.config import Settings, load_machine_definitions
from lab_orchestrator.core.state_machine import IllegalTransition, InstanceState
from lab_orchestrator.db.database import create_db_engine
from lab_orchestrator.db.init_db import init_db, sync_machine_definitions
from lab_orchestrator.db.models import instances

REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
REAL_MACHINES_CONFIG = REPO_ROOT / "config" / "machines.yaml"

_hostname_counter = itertools.count()


@pytest.fixture
def engine(tmp_path):
    eng = create_db_engine(tmp_path / "test.db")
    init_db(eng)
    registry = load_machine_definitions(REAL_MACHINES_CONFIG)
    sync_machine_definitions(eng, registry)
    yield eng
    eng.dispose()


@pytest.fixture
def machines():
    return load_machine_definitions(REAL_MACHINES_CONFIG)


@pytest.fixture
def tux2lab():
    return FakeTux2LabClient()


@pytest.fixture
def settings():
    # Short poll interval/timeout so the failure-path tests (which poll
    # until timeout) don't make the suite slow.
    return Settings(
        provisioning_poll_interval_seconds=0.01,
        provisioning_timeout_seconds=0.2,
    )


@pytest.fixture
def working_naming(monkeypatch):
    """Unblock naming.py for tests that need provisioning to actually
    proceed. Patches the module attribute (not a rebound import) so
    instance_manager's `naming.generate_hostname(...)` call sees it.
    """

    def _generate(machine):
        return f"lab-{machine.code}-{machine.codename}-t{next(_hostname_counter)}"

    monkeypatch.setattr(naming, "generate_hostname", _generate)


@pytest.fixture
def always_reachable(monkeypatch):
    """FakeTux2LabClient.start() sets a fixed fake IP (10.28.28.100)
    that isn't actually reachable from this sandbox, so the real TCP
    check would always fail. Stub it out for happy-path tests.
    """

    async def _open(host, port, timeout=3.0):
        return True

    monkeypatch.setattr(instance_manager, "_tcp_port_open", _open)


def row_of(engine, instance_id) -> dict:
    with engine.connect() as conn:
        return dict(
            conn.execute(sa.select(instances).where(instances.c.id == instance_id))
            .mappings()
            .one()
        )


# --- create_instance -------------------------------------------------------


async def test_create_instance_returns_provisioning_state(engine, machines, settings):
    """Architecture doc §8: the 202 response (and so this function's
    return value) already shows PROVISIONING, not REQUESTED -- the
    REQUESTED -> PROVISIONING transition happens synchronously in
    create_instance(), before provision_instance (the background task)
    does any actual work. See instance_manager.py for why.
    """
    row = await instance_manager.create_instance(
        engine=engine, machines=machines, settings=settings, user_id="alice", machine_type="machine_1"
    )
    assert row["state"] == InstanceState.PROVISIONING.value
    assert row["user_id"] == "alice"
    assert row["machine_type"] == "machine_1"
    db_row = row_of(engine, row["id"])
    assert db_row["state"] == InstanceState.PROVISIONING.value


async def test_create_instance_unknown_machine_type_raises(engine, machines, settings):
    with pytest.raises(instance_manager.UnknownMachineTypeError):
        await instance_manager.create_instance(
            engine=engine, machines=machines, settings=settings, user_id="alice", machine_type="nope"
        )


async def test_create_instance_disabled_machine_raises(engine, machines, settings, tmp_path):
    disabled_config = tmp_path / "machines.yaml"
    disabled_config.write_text(
        REAL_MACHINES_CONFIG.read_text().replace("enabled: true", "enabled: false")
    )
    disabled_registry = load_machine_definitions(disabled_config)
    with pytest.raises(instance_manager.MachineDisabledError):
        await instance_manager.create_instance(
            engine=engine,
            machines=disabled_registry,
            settings=settings,
            user_id="alice",
            machine_type="machine_1",
        )


async def test_create_instance_user_quota_exceeded_raises(engine, machines, settings):
    await instance_manager.create_instance(
        engine=engine, machines=machines, settings=settings, user_id="alice", machine_type="machine_1"
    )
    with pytest.raises(instance_manager.UserQuotaExceededError):
        await instance_manager.create_instance(
            engine=engine, machines=machines, settings=settings, user_id="alice", machine_type="machine_2"
        )


async def test_create_instance_global_quota_exceeded_raises(engine, machines, settings):
    for i, user in enumerate(["alice", "bob", "carol"]):
        machine_type = f"machine_{(i % 3) + 1}"
        await instance_manager.create_instance(
            engine=engine, machines=machines, settings=settings, user_id=user, machine_type=machine_type
        )
    with pytest.raises(instance_manager.GlobalQuotaExceededError):
        await instance_manager.create_instance(
            engine=engine, machines=machines, settings=settings, user_id="dave", machine_type="machine_1"
        )


# --- get_instance ------------------------------------------------------


async def test_get_instance_returns_row(engine, machines, settings):
    created = await instance_manager.create_instance(
        engine=engine, machines=machines, settings=settings, user_id="alice", machine_type="machine_1"
    )
    fetched = await instance_manager.get_instance(engine=engine, instance_id=created["id"])
    assert fetched["id"] == created["id"]


async def test_get_instance_unknown_returns_none(engine):
    assert await instance_manager.get_instance(engine=engine, instance_id="does-not-exist") is None


# --- provision_instance --------------------------------------------------


async def test_provision_instance_happy_path_reaches_ready(
    engine, machines, tux2lab, settings, working_naming, always_reachable
):
    created = await instance_manager.create_instance(
        engine=engine, machines=machines, settings=settings, user_id="alice", machine_type="machine_1"
    )
    await instance_manager.provision_instance(
        engine=engine, machines=machines, tux2lab=tux2lab, settings=settings, instance_id=created["id"]
    )
    row = row_of(engine, created["id"])
    assert row["state"] == InstanceState.READY.value
    assert row["vm_hostname"] is not None
    assert row["vm_ip"] == "10.28.28.100"
    assert row["ready_at"] is not None


async def test_provision_instance_naming_still_blocked_fails_cleanly(
    engine, machines, tux2lab, settings
):
    """Doesn't patch naming.py -- proves the real, still-blocked
    generate_hostname produces a clean DESTROYED row with a clear
    failure_reason, not an unhandled crash.
    """
    created = await instance_manager.create_instance(
        engine=engine, machines=machines, settings=settings, user_id="alice", machine_type="machine_1"
    )
    await instance_manager.provision_instance(
        engine=engine, machines=machines, tux2lab=tux2lab, settings=settings, instance_id=created["id"]
    )
    row = row_of(engine, created["id"])
    assert row["state"] == InstanceState.DESTROYED.value
    assert "architecture doc §5" in row["failure_reason"]
    assert row["destroyed_at"] is not None


async def test_provision_instance_install_failure_fails_cleanly(
    engine, machines, tux2lab, settings, working_naming
):
    tux2lab.raise_once = Tux2LabCommandError("simulated install failure")
    created = await instance_manager.create_instance(
        engine=engine, machines=machines, settings=settings, user_id="alice", machine_type="machine_1"
    )
    await instance_manager.provision_instance(
        engine=engine, machines=machines, tux2lab=tux2lab, settings=settings, instance_id=created["id"]
    )
    row = row_of(engine, created["id"])
    assert row["state"] == InstanceState.DESTROYED.value
    assert "simulated install failure" in row["failure_reason"]


async def test_provision_instance_readiness_timeout_fails_cleanly(
    engine, machines, tux2lab, settings, working_naming
):
    # Deliberately does NOT patch _tcp_port_open -- FakeTux2LabClient's
    # fixed fake IP (10.28.28.100) is genuinely unreachable from this
    # sandbox, so readiness criteria never pass and settings' short
    # provisioning_timeout_seconds (0.2s) is what ends the poll loop.
    created = await instance_manager.create_instance(
        engine=engine, machines=machines, settings=settings, user_id="alice", machine_type="machine_1"
    )
    await instance_manager.provision_instance(
        engine=engine, machines=machines, tux2lab=tux2lab, settings=settings, instance_id=created["id"]
    )
    row = row_of(engine, created["id"])
    assert row["state"] == InstanceState.DESTROYED.value
    assert "readiness criteria not met" in row["failure_reason"]


async def test_provision_instance_removes_vm_on_failure_after_install(
    engine, machines, tux2lab, settings, working_naming
):
    """A failure *after* install succeeded should still clean up the VM
    tux2lab thinks exists (best-effort remove_idempotent in _fail()).
    """
    created = await instance_manager.create_instance(
        engine=engine, machines=machines, settings=settings, user_id="alice", machine_type="machine_1"
    )
    await instance_manager.provision_instance(
        engine=engine, machines=machines, tux2lab=tux2lab, settings=settings, instance_id=created["id"]
    )
    row = row_of(engine, created["id"])
    assert row["vm_hostname"] is not None
    # If cleanup ran, the fake client's in-memory VM dict no longer has it.
    assert await tux2lab.list() == []


async def test_provision_instance_illegal_transition_propagates_not_swallowed(
    engine, machines, tux2lab, settings, working_naming
):
    """A row already in an inconsistent state (here: READY, which has no
    START_PROVISIONING transition) should raise IllegalTransition
    straight out of provision_instance, not get quietly treated as a VM
    failure and driven to DESTROYED.
    """
    created = await instance_manager.create_instance(
        engine=engine, machines=machines, settings=settings, user_id="alice", machine_type="machine_1"
    )
    with engine.begin() as conn:
        conn.execute(
            instances.update().where(instances.c.id == created["id"]).values(state="READY")
        )
    with pytest.raises(IllegalTransition):
        await instance_manager.provision_instance(
            engine=engine, machines=machines, tux2lab=tux2lab, settings=settings, instance_id=created["id"]
        )


async def test_provision_instance_unknown_id_is_a_safe_noop(engine, machines, tux2lab, settings):
    # Defensive path only -- shouldn't happen in normal operation.
    await instance_manager.provision_instance(
        engine=engine,
        machines=machines,
        tux2lab=tux2lab,
        settings=settings,
        instance_id="does-not-exist",
    )
