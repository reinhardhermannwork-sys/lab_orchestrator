"""Tests for the M6 janitor cleanup pass."""

from __future__ import annotations

from datetime import timedelta

import pytest
import sqlalchemy as sa

from lab_orchestrator.adapters.tux2lab_client import (
    FakeTux2LabClient,
    Tux2LabCommandError,
)
from lab_orchestrator.core import janitor
from lab_orchestrator.core.config import Settings, load_machine_definitions
from lab_orchestrator.core.janitor import run_once
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
    registry = load_machine_definitions(REAL_MACHINES_CONFIG)
    sync_machine_definitions(eng, registry)
    yield eng
    eng.dispose()


@pytest.fixture
def settings():
    return Settings(disconnect_grace_seconds=300.0)


def insert_instance(
    engine,
    *,
    instance_id: str,
    state: InstanceState,
    expires_at,
    disconnect_since=None,
    hostname=None,
):
    now = utcnow()
    row = {
        "id": instance_id,
        "user_id": f"user-{instance_id}",
        "machine_type": "machine_1",
        "vm_hostname": hostname,
        "state": state.value,
        "created_at": now - timedelta(hours=1),
        "expires_at": expires_at,
        "disconnect_since": disconnect_since,
    }
    with engine.begin() as conn:
        conn.execute(instances.insert().values(**row))


def row_of(engine, instance_id):
    with engine.connect() as conn:
        return dict(
            conn.execute(sa.select(instances).where(instances.c.id == instance_id)).mappings().one()
        )


async def test_expired_lease_is_destroyed_and_vm_removed(engine, settings):
    tux2lab = FakeTux2LabClient()
    await tux2lab.install("lab-m01-aurora-test1", "image_1_software_1")

    insert_instance(
        engine,
        instance_id="expired",
        state=InstanceState.READY,
        expires_at=utcnow() - timedelta(seconds=1),
        hostname="lab-m01-aurora-test1",
    )

    assert await run_once(engine=engine, tux2lab=tux2lab, settings=settings) == 1
    assert row_of(engine, "expired")["state"] == InstanceState.DESTROYED.value
    assert await tux2lab.list() == []


async def test_unexpired_disconnect_grace_is_left_alone(engine, settings):
    tux2lab = FakeTux2LabClient()
    insert_instance(
        engine,
        instance_id="grace",
        state=InstanceState.DISCONNECTED_GRACE,
        expires_at=utcnow() + timedelta(hours=1),
        disconnect_since=utcnow() - timedelta(seconds=299),
    )

    assert await run_once(engine=engine, tux2lab=tux2lab, settings=settings) == 0
    assert row_of(engine, "grace")["state"] == InstanceState.DISCONNECTED_GRACE.value


async def test_expired_disconnect_grace_is_destroyed(engine, settings):
    tux2lab = FakeTux2LabClient()
    insert_instance(
        engine,
        instance_id="grace-expired",
        state=InstanceState.DISCONNECTED_GRACE,
        expires_at=utcnow() + timedelta(hours=1),
        disconnect_since=utcnow() - timedelta(seconds=301),
    )

    assert await run_once(engine=engine, tux2lab=tux2lab, settings=settings) == 1
    assert row_of(engine, "grace-expired")["state"] == InstanceState.DESTROYED.value


async def test_destroying_instance_is_retried(engine, settings):
    tux2lab = FakeTux2LabClient()
    await tux2lab.install("lab-m01-aurora-retry1", "image_1_software_1")

    insert_instance(
        engine,
        instance_id="retry",
        state=InstanceState.DESTROYING,
        expires_at=utcnow() + timedelta(hours=1),
        hostname="lab-m01-aurora-retry1",
    )

    assert await run_once(engine=engine, tux2lab=tux2lab, settings=settings) == 1
    assert row_of(engine, "retry")["state"] == InstanceState.DESTROYED.value


async def test_cleanup_failure_leaves_destroying_for_retry(engine, settings):
    tux2lab = FakeTux2LabClient()
    await tux2lab.install("lab-m01-aurora-fail1", "image_1_software_1")
    tux2lab.raise_once = Tux2LabCommandError("simulated remove failure")

    insert_instance(
        engine,
        instance_id="cleanup-fails",
        state=InstanceState.READY,
        expires_at=utcnow() - timedelta(seconds=1),
        hostname="lab-m01-aurora-fail1",
    )

    assert await run_once(engine=engine, tux2lab=tux2lab, settings=settings) == 0
    assert row_of(engine, "cleanup-fails")["state"] == InstanceState.DESTROYING.value
    assert await tux2lab.info("lab-m01-aurora-fail1")


async def test_cleanup_instance_is_finished(engine, settings):
    # CLEANUP left behind by startup reconciliation (M7) or a killed
    # provisioning task, lease not yet expired.
    tux2lab = FakeTux2LabClient()
    await tux2lab.install("lab-m01-aurora-clean1", "image_1_software_1")
    insert_instance(
        engine,
        instance_id="cleanup",
        state=InstanceState.CLEANUP,
        expires_at=utcnow() + timedelta(hours=1),
        hostname="lab-m01-aurora-clean1",
    )

    assert await run_once(engine=engine, tux2lab=tux2lab, settings=settings) == 1
    row = row_of(engine, "cleanup")
    assert row["state"] == InstanceState.DESTROYED.value
    assert row["destroyed_at"] is not None
    assert await tux2lab.list() == []


async def test_cleanup_remove_failure_leaves_cleanup_for_retry(engine, settings):
    tux2lab = FakeTux2LabClient()
    await tux2lab.install("lab-m01-aurora-clean2", "image_1_software_1")
    tux2lab.raise_once = Tux2LabCommandError("simulated remove failure")
    insert_instance(
        engine,
        instance_id="cleanup-retry",
        state=InstanceState.CLEANUP,
        expires_at=utcnow() + timedelta(hours=1),
        hostname="lab-m01-aurora-clean2",
    )

    assert await run_once(engine=engine, tux2lab=tux2lab, settings=settings) == 0
    assert row_of(engine, "cleanup-retry")["state"] == InstanceState.CLEANUP.value

    assert await run_once(engine=engine, tux2lab=tux2lab, settings=settings) == 1
    assert row_of(engine, "cleanup-retry")["state"] == InstanceState.DESTROYED.value


async def test_failed_instance_is_not_touched(engine, settings):
    # FAILED is still owned by the provisioning task that's about to move
    # it to CLEANUP; only reconciliation (after a restart) takes it over.
    # Not even lease expiry applies: FAILED has no LIFETIME_EXPIRED edge.
    tux2lab = FakeTux2LabClient()
    insert_instance(
        engine,
        instance_id="failing",
        state=InstanceState.FAILED,
        expires_at=utcnow() - timedelta(seconds=1),
    )

    assert await run_once(engine=engine, tux2lab=tux2lab, settings=settings) == 0
    assert row_of(engine, "failing")["state"] == InstanceState.FAILED.value


async def test_expired_instance_without_vm_is_destroyed(engine, settings):
    tux2lab = FakeTux2LabClient()
    insert_instance(
        engine,
        instance_id="never-provisioned",
        state=InstanceState.REQUESTED,
        expires_at=utcnow() - timedelta(seconds=1),
    )

    assert await run_once(engine=engine, tux2lab=tux2lab, settings=settings) == 1
    assert row_of(engine, "never-provisioned")["state"] == InstanceState.DESTROYED.value


def stale_candidates(monkeypatch, *overrides):
    """Make the janitor see rows as they were before someone else changed
    them: each override is (instance_id, {column: stale_value})."""

    async def _stale(engine, **_kwargs):
        return [{**row_of(engine, instance_id), **cols} for instance_id, cols in overrides]

    monkeypatch.setattr(janitor, "_load_candidates", _stale)


async def test_claim_uses_fresh_row_not_stale_candidate(engine, settings, monkeypatch):
    # Provisioning wrote vm_hostname after the janitor's SELECT: the VM
    # must still be removed, not orphaned.
    tux2lab = FakeTux2LabClient()
    await tux2lab.install("lab-m01-aurora-fresh", "image_1_software_1")
    insert_instance(
        engine,
        instance_id="fresh",
        state=InstanceState.READY,
        expires_at=utcnow() - timedelta(seconds=1),
        hostname="lab-m01-aurora-fresh",
    )
    stale_candidates(monkeypatch, ("fresh", {"vm_hostname": None}))

    assert await run_once(engine=engine, tux2lab=tux2lab, settings=settings) == 1
    assert await tux2lab.list() == []


async def test_row_that_moved_on_is_left_alone(engine, settings, monkeypatch):
    tux2lab = FakeTux2LabClient()
    insert_instance(
        engine,
        instance_id="moved",
        state=InstanceState.CONNECTED,
        expires_at=utcnow() - timedelta(seconds=1),
    )
    # Janitor thinks it's still READY; the guarded UPDATE must not match.
    stale_candidates(monkeypatch, ("moved", {"state": InstanceState.READY.value}))

    assert await run_once(engine=engine, tux2lab=tux2lab, settings=settings) == 0
    assert row_of(engine, "moved")["state"] == InstanceState.CONNECTED.value


async def test_unexpected_error_on_one_row_does_not_stop_the_pass(engine, settings, monkeypatch):
    tux2lab = FakeTux2LabClient()
    for name in ("boom", "ok"):
        await tux2lab.install(f"lab-m01-aurora-{name}", "image_1_software_1")
        insert_instance(
            engine,
            instance_id=name,
            state=InstanceState.READY,
            expires_at=utcnow() - timedelta(seconds=1),
            hostname=f"lab-m01-aurora-{name}",
        )

    real_remove = tux2lab.remove_idempotent

    async def remove(hostname):
        if hostname == "lab-m01-aurora-boom":
            raise RuntimeError("not a Tux2LabError")
        await real_remove(hostname)

    monkeypatch.setattr(tux2lab, "remove_idempotent", remove)

    assert await run_once(engine=engine, tux2lab=tux2lab, settings=settings) == 1
    assert row_of(engine, "ok")["state"] == InstanceState.DESTROYED.value
    assert row_of(engine, "boom")["state"] == InstanceState.DESTROYING.value


async def test_destroyed_at_is_recorded(engine, settings):
    tux2lab = FakeTux2LabClient()
    insert_instance(
        engine,
        instance_id="stamped",
        state=InstanceState.READY,
        expires_at=utcnow() - timedelta(seconds=1),
    )
    before = utcnow()

    await run_once(engine=engine, tux2lab=tux2lab, settings=settings)

    assert row_of(engine, "stamped")["destroyed_at"] >= before
