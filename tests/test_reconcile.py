"""Tests for M7 startup reconciliation."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import pytest
import sqlalchemy as sa

from lab_orchestrator.adapters.tux2lab_client import FakeTux2LabClient, Tux2LabConnectionError
from lab_orchestrator.core import instance_manager, janitor
from lab_orchestrator.core.config import Settings, load_machine_definitions
from lab_orchestrator.core.reconcile import reconcile_on_startup
from lab_orchestrator.core.state_machine import InstanceState
from lab_orchestrator.db.database import create_db_engine
from lab_orchestrator.db.init_db import init_db, sync_machine_definitions
from lab_orchestrator.db.models import instances, utcnow

REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
REAL_MACHINES_CONFIG = REPO_ROOT / "config" / "machines.yaml"


@pytest.fixture
def engine(tmp_path):
    eng = create_db_engine(tmp_path / "test.db")
    init_db(eng)
    sync_machine_definitions(eng, load_machine_definitions(REAL_MACHINES_CONFIG))
    yield eng
    eng.dispose()


@pytest.fixture
def machines():
    return load_machine_definitions(REAL_MACHINES_CONFIG)


@pytest.fixture
def settings():
    return Settings()


def insert_instance(engine, *, instance_id, state, hostname=None, failure_reason=None):
    now = utcnow()
    with engine.begin() as conn:
        conn.execute(
            instances.insert().values(
                id=instance_id,
                user_id=f"user-{instance_id}",
                machine_type="machine_1",
                vm_hostname=hostname,
                state=state.value,
                created_at=now,
                expires_at=now + timedelta(hours=4),
                failure_reason=failure_reason,
            )
        )


def row_of(engine, instance_id):
    with engine.connect() as conn:
        return dict(
            conn.execute(sa.select(instances).where(instances.c.id == instance_id)).mappings().one()
        )


@pytest.mark.parametrize(
    "state",
    [
        InstanceState.REQUESTED,
        InstanceState.PROVISIONING,
        InstanceState.STARTING,
        InstanceState.WAITING_READY,
    ],
)
async def test_orphaned_in_flight_lease_is_moved_to_cleanup(engine, state):
    tux2lab = FakeTux2LabClient()
    await tux2lab.install("lab-m01-aurora-orph1", "image_1_software_1")
    insert_instance(engine, instance_id="orph", state=state, hostname="lab-m01-aurora-orph1")

    report = await reconcile_on_startup(engine=engine, tux2lab=tux2lab)

    row = row_of(engine, "orph")
    assert row["state"] == InstanceState.CLEANUP.value
    assert row["failure_reason"] == f"orchestrator restarted while instance was {state.value}"
    assert report.cleaned_up == ["orph"]
    # Reconciliation itself never removes VMs; the janitor does.
    assert [vm.hostname for vm in await tux2lab.list()] == ["lab-m01-aurora-orph1"]
    # The orphan still claims its VM, so it isn't also reported as unknown.
    assert report.unknown_vms == []


async def test_stuck_failed_lease_keeps_its_failure_reason(engine):
    insert_instance(
        engine, instance_id="failed", state=InstanceState.FAILED, failure_reason="install broke"
    )

    report = await reconcile_on_startup(engine=engine, tux2lab=FakeTux2LabClient())

    row = row_of(engine, "failed")
    assert row["state"] == InstanceState.CLEANUP.value
    assert row["failure_reason"] == "install broke"
    assert report.cleaned_up == ["failed"]


@pytest.mark.parametrize(
    "state", [InstanceState.CLEANUP, InstanceState.DESTROYING, InstanceState.DESTROYED]
)
async def test_teardown_and_destroyed_leases_are_left_alone(engine, state):
    insert_instance(engine, instance_id="x", state=state)

    report = await reconcile_on_startup(engine=engine, tux2lab=FakeTux2LabClient())

    assert row_of(engine, "x")["state"] == state.value
    assert report.cleaned_up == []


async def test_healthy_lease_with_vm_is_untouched(engine):
    tux2lab = FakeTux2LabClient()
    await tux2lab.install("lab-m01-aurora-ok", "image_1_software_1")
    insert_instance(engine, instance_id="ok", state=InstanceState.READY, hostname="lab-m01-aurora-ok")

    report = await reconcile_on_startup(engine=engine, tux2lab=tux2lab)

    assert row_of(engine, "ok")["state"] == InstanceState.READY.value
    assert (report.cleaned_up, report.missing_vms, report.unknown_vms) == ([], [], [])


@pytest.mark.parametrize(
    "state",
    [InstanceState.READY, InstanceState.CONNECTED, InstanceState.DISCONNECTED_GRACE],
)
async def test_active_lease_with_missing_vm_is_reported_not_changed(engine, state, caplog):
    insert_instance(engine, instance_id="gone", state=state, hostname="lab-m01-aurora-gone")

    with caplog.at_level(logging.WARNING, logger="lab_orchestrator.core.reconcile"):
        report = await reconcile_on_startup(engine=engine, tux2lab=FakeTux2LabClient())

    assert report.missing_vms == ["gone"]
    assert row_of(engine, "gone")["state"] == state.value
    assert "lab-m01-aurora-gone does not exist" in caplog.text


async def test_unknown_vm_is_reported_not_removed(engine, caplog):
    tux2lab = FakeTux2LabClient()
    await tux2lab.install("lab-m02-forge-stray", "image_2_software_2")
    await tux2lab.install("someone-elses-vm", "image_2_software_2")
    # A DESTROYED lease doesn't claim its old hostname.
    insert_instance(
        engine, instance_id="old", state=InstanceState.DESTROYED, hostname="lab-m02-forge-stray"
    )

    with caplog.at_level(logging.WARNING, logger="lab_orchestrator.core.reconcile"):
        report = await reconcile_on_startup(engine=engine, tux2lab=tux2lab)

    assert report.unknown_vms == ["lab-m02-forge-stray", "someone-elses-vm"]
    assert len(await tux2lab.list()) == 2
    assert "VM someone-elses-vm exists on the host" in caplog.text


async def test_tux2lab_unreachable_still_cleans_up_orphans(engine, caplog):
    tux2lab = FakeTux2LabClient()
    insert_instance(engine, instance_id="orph", state=InstanceState.PROVISIONING)
    tux2lab.raise_once = Tux2LabConnectionError("host down")

    with caplog.at_level(logging.ERROR, logger="lab_orchestrator.core.reconcile"):
        report = await reconcile_on_startup(engine=engine, tux2lab=tux2lab)

    assert report.vm_list_failed
    assert report.cleaned_up == ["orph"]
    assert row_of(engine, "orph")["state"] == InstanceState.CLEANUP.value
    assert "could not list VMs" in caplog.text


async def test_killed_mid_install_then_restart_leaves_no_stuck_or_ghost_instance(
    engine, machines, settings, monkeypatch, caplog
):
    """Implementation plan §M7 done-when: kill the process mid-provision,
    restart, and get a clear reconciliation log entry instead of a stuck
    lease or a ghost VM.

    "Killed" = the provisioning task is cancelled after tux2lab created
    the VM but before INSTALL_COMPLETE was recorded. The fake client
    stands in for the host and outlives the "process", as a real host would.
    """
    tux2lab = FakeTux2LabClient()
    vm_created = asyncio.Event()
    real_install = tux2lab.install_idempotent

    async def install_then_hang(hostname, image):
        await real_install(hostname, image)
        vm_created.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(tux2lab, "install_idempotent", install_then_hang)

    created = await instance_manager.create_instance(
        engine=engine, machines=machines, settings=settings, user_id="alice", machine_type="machine_1"
    )
    task = asyncio.create_task(
        instance_manager.provision_instance(
            engine=engine, machines=machines, tux2lab=tux2lab, settings=settings, instance_id=created["id"]
        )
    )
    await asyncio.wait_for(vm_created.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    stuck = row_of(engine, created["id"])
    assert stuck["state"] == InstanceState.PROVISIONING.value
    hostname = stuck["vm_hostname"]
    assert hostname is not None
    assert [vm.hostname for vm in await tux2lab.list()] == [hostname]

    # --- restart ---
    with caplog.at_level(logging.WARNING, logger="lab_orchestrator.core.reconcile"):
        report = await reconcile_on_startup(engine=engine, tux2lab=tux2lab)
    assert report.cleaned_up == [created["id"]]
    assert f"instance {created['id']} (user alice, VM {hostname}) was PROVISIONING" in caplog.text

    assert await janitor.run_once(engine=engine, tux2lab=tux2lab, settings=settings) == 1
    row = row_of(engine, created["id"])
    assert row["state"] == InstanceState.DESTROYED.value
    assert await tux2lab.list() == []

    # alice can lease again.
    again = await instance_manager.create_instance(
        engine=engine, machines=machines, settings=settings, user_id="alice", machine_type="machine_1"
    )
    assert again["state"] == InstanceState.PROVISIONING.value
