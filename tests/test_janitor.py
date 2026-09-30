"""Tests for the M6 janitor cleanup pass."""

from __future__ import annotations

from datetime import timedelta

import pytest
import sqlalchemy as sa

from lab_orchestrator.adapters.tux2lab_client import (
    FakeTux2LabClient,
    Tux2LabCommandError,
)
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
            conn.execute(sa.select(instances).where(instances.c.id == instance_id))
            .mappings()
            .one()
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
