"""Tests for core/instance_manager.py (M5).

Most tests here monkeypatch `lab_orchestrator.naming.generate_hostname`
with a deterministic stand-in (via the module reference, not a rebound
import, matching how instance_manager itself calls it) so hostnames are
predictable. One test deliberately doesn't patch it, to exercise the real
generator end to end.
"""

from __future__ import annotations

import asyncio
import itertools
import re
from datetime import timedelta

import pytest
import sqlalchemy as sa

from lab_orchestrator import naming
from lab_orchestrator.adapters.tux2lab_client import FakeTux2LabClient, Tux2LabCommandError
from lab_orchestrator.core import instance_manager, janitor
from lab_orchestrator.core.config import Settings, load_machine_definitions
from lab_orchestrator.core.state_machine import IllegalTransition, InstanceState
from lab_orchestrator.db.database import create_db_engine
from lab_orchestrator.db.init_db import init_db, sync_machine_definitions
from lab_orchestrator.db.models import instances, utcnow

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


async def test_provision_instance_with_real_naming_reaches_ready(
    engine, machines, tux2lab, settings, always_reachable
):
    """Doesn't patch naming.py -- the real generator drives provisioning."""
    created = await instance_manager.create_instance(
        engine=engine, machines=machines, settings=settings, user_id="alice", machine_type="machine_1"
    )
    await instance_manager.provision_instance(
        engine=engine, machines=machines, tux2lab=tux2lab, settings=settings, instance_id=created["id"]
    )
    row = row_of(engine, created["id"])
    assert row["state"] == InstanceState.READY.value
    assert re.fullmatch(r"lab-m01-aurora-[0-9a-z]{5}", row["vm_hostname"])
    assert "alice" not in row["vm_hostname"]


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


# --- concurrent janitor (M6) ----------------------------------------------


def expire_lease(engine, instance_id):
    with engine.begin() as conn:
        conn.execute(
            instances.update()
            .where(instances.c.id == instance_id)
            .values(expires_at=utcnow() - timedelta(seconds=1))
        )


async def wait_for_state(engine, instance_id, state):
    for _ in range(200):
        if row_of(engine, instance_id)["state"] == state.value:
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"{instance_id} never reached {state.value}")


async def test_janitor_destroy_during_readiness_polling_is_not_overwritten(
    engine, machines, tux2lab, working_naming, monkeypatch
):
    """Lease expires while provisioning is still polling for readiness:
    the janitor's DESTROYED row must not be rewritten through
    FAILED -> CLEANUP -> DESTROYED by the provisioning task.
    """

    async def _never_reachable(host, port, timeout=3.0):
        return False

    monkeypatch.setattr(instance_manager, "_tcp_port_open", _never_reachable)
    settings = Settings(provisioning_poll_interval_seconds=0.01, provisioning_timeout_seconds=5)
    created = await instance_manager.create_instance(
        engine=engine, machines=machines, settings=settings, user_id="alice", machine_type="machine_1"
    )
    task = asyncio.create_task(
        instance_manager.provision_instance(
            engine=engine, machines=machines, tux2lab=tux2lab, settings=settings, instance_id=created["id"]
        )
    )
    await wait_for_state(engine, created["id"], InstanceState.WAITING_READY)

    expire_lease(engine, created["id"])
    assert await janitor.run_once(engine=engine, tux2lab=tux2lab, settings=settings) == 1
    destroyed = row_of(engine, created["id"])

    await asyncio.wait_for(task, timeout=2)

    row = row_of(engine, created["id"])
    assert row["state"] == InstanceState.DESTROYED.value
    assert row["failure_reason"] is None
    assert row["destroyed_at"] == destroyed["destroyed_at"]
    assert await tux2lab.list() == []


async def test_lease_expiring_during_install_does_not_orphan_vm(
    engine, machines, tux2lab, settings, working_naming, monkeypatch
):
    """Lease expires before install finishes: the janitor's remove runs
    before the VM exists, so provisioning must remove the VM itself once
    install completes, and leave the row alone.
    """
    install_started = asyncio.Event()
    release_install = asyncio.Event()
    real_install = tux2lab.install_idempotent

    async def slow_install(hostname, image):
        install_started.set()
        await release_install.wait()
        await real_install(hostname, image)

    monkeypatch.setattr(tux2lab, "install_idempotent", slow_install)

    created = await instance_manager.create_instance(
        engine=engine, machines=machines, settings=settings, user_id="alice", machine_type="machine_1"
    )
    task = asyncio.create_task(
        instance_manager.provision_instance(
            engine=engine, machines=machines, tux2lab=tux2lab, settings=settings, instance_id=created["id"]
        )
    )
    await asyncio.wait_for(install_started.wait(), timeout=2)

    expire_lease(engine, created["id"])
    assert await janitor.run_once(engine=engine, tux2lab=tux2lab, settings=settings) == 1

    release_install.set()
    await asyncio.wait_for(task, timeout=2)

    row = row_of(engine, created["id"])
    assert row["state"] == InstanceState.DESTROYED.value
    assert row["vm_hostname"] is not None  # recorded before install (M7)
    assert await tux2lab.list() == []


async def test_hostname_is_recorded_before_install(
    engine, machines, tux2lab, settings, working_naming, always_reachable, monkeypatch
):
    """M7: a crash mid-install must leave a row that names the VM, so
    reconciliation and the janitor can remove it.
    """
    seen_during_install = {}
    real_install = tux2lab.install_idempotent

    async def checking_install(hostname, image):
        row = row_of(engine, created["id"])
        seen_during_install.update(state=row["state"], vm_hostname=row["vm_hostname"])
        await real_install(hostname, image)

    monkeypatch.setattr(tux2lab, "install_idempotent", checking_install)

    created = await instance_manager.create_instance(
        engine=engine, machines=machines, settings=settings, user_id="alice", machine_type="machine_1"
    )
    await instance_manager.provision_instance(
        engine=engine, machines=machines, tux2lab=tux2lab, settings=settings, instance_id=created["id"]
    )

    row = row_of(engine, created["id"])
    assert row["state"] == InstanceState.READY.value
    assert seen_during_install == {
        "state": InstanceState.PROVISIONING.value,
        "vm_hostname": row["vm_hostname"],
    }
    assert row["vm_hostname"] is not None

