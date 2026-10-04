"""Tests for the data layer (M2 done-when criteria).

Every constraint here is verified against the real SQLite engine, not
mocked — these are DB-level guarantees, so the DB is what has to prove
them.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from ulid import ULID

from lab_orchestrator.core.config import load_machine_definitions
from lab_orchestrator.db.database import create_db_engine
from lab_orchestrator.db.init_db import init_db, sync_machine_definitions
from lab_orchestrator.db.models import instances, machine_definitions

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_MACHINES_CONFIG = REPO_ROOT / "config" / "machines.yaml"


def utcnow() -> datetime:
    """Naive UTC `datetime`, matching the storage convention in db/models.py."""
    return datetime.now(UTC).replace(tzinfo=None)


@pytest.fixture
def engine(tmp_path):
    eng = create_db_engine(tmp_path / "test.db")
    init_db(eng)
    registry = load_machine_definitions(REAL_MACHINES_CONFIG)
    sync_machine_definitions(eng, registry)
    yield eng
    eng.dispose()


def instance_row(**overrides) -> dict:
    """A minimal, valid `instances` row. Every test that needs a row
    should override at least `id` and usually `user_id`.
    """
    now = utcnow()
    row = {
        "id": str(ULID()),
        "user_id": "alice",
        "machine_type": "machine_1",
        "state": "REQUESTED",
        "created_at": now,
        "expires_at": now + timedelta(hours=4),
    }
    row.update(overrides)
    return row


def insert(engine, **overrides) -> None:
    with engine.begin() as conn:
        conn.execute(instances.insert().values(**instance_row(**overrides)))


# --- schema + sync ------------------------------------------------------


def test_init_db_creates_both_tables(engine):
    table_names = set(sa.inspect(engine).get_table_names())
    assert {"machine_definitions", "instances"} <= table_names


def test_init_db_is_idempotent(engine):
    # Calling it again (as every boot does) must not error or drop data.
    insert(engine, id="keep-me")
    init_db(engine)
    with engine.connect() as conn:
        row = conn.execute(
            sa.select(instances.c.id).where(instances.c.id == "keep-me")
        ).first()
    assert row is not None


def test_sync_machine_definitions_matches_yaml(engine):
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(machine_definitions).order_by(machine_definitions.c.id)
        ).mappings().all()
    assert [r["id"] for r in rows] == ["machine_1", "machine_2", "machine_3"]
    m1 = rows[0]
    assert m1["display_name"] == "Machine 1"
    assert m1["code"] == "m01"
    assert m1["codename"] == "aurora"
    assert m1["protocol"] == "ssh"
    assert m1["enabled"] is True


def test_sync_machine_definitions_upserts_on_rerun(engine, tmp_path):
    # Change display_name in a second yaml, re-sync, expect the update
    # applied rather than a duplicate/conflicting row.
    changed = tmp_path / "machines2.yaml"
    changed.write_text(
        REAL_MACHINES_CONFIG.read_text().replace('"Machine 1"', '"Machine One Renamed"')
    )
    registry = load_machine_definitions(changed)
    sync_machine_definitions(engine, registry)
    with engine.connect() as conn:
        row = conn.execute(
            sa.select(machine_definitions.c.display_name).where(
                machine_definitions.c.id == "machine_1"
            )
        ).scalar_one()
    assert row == "Machine One Renamed"


# --- FK + CHECK constraints ----------------------------------------------


def test_foreign_key_enforced(engine):
    with pytest.raises(IntegrityError):
        insert(engine, id="bad-fk", user_id="bob", machine_type="no_such_machine")


def test_state_check_constraint_rejects_unknown_state(engine):
    with pytest.raises(IntegrityError):
        insert(engine, id="bad-state", user_id="bob", state="BOGUS")


# --- quota constraints: one active per user -------------------------------


def test_second_active_instance_same_user_rejected(engine):
    insert(engine, id="a1", user_id="alice")
    with pytest.raises(IntegrityError):
        insert(engine, id="a2", user_id="alice")


def test_new_active_instance_allowed_after_previous_destroyed(engine):
    insert(engine, id="a1", user_id="alice")
    with engine.begin() as conn:
        conn.execute(
            instances.update().where(instances.c.id == "a1").values(state="DESTROYED")
        )
    # Should not raise: alice's only active row is gone.
    insert(engine, id="a2", user_id="alice")


def test_different_users_each_get_one_active_instance(engine):
    insert(engine, id="a1", user_id="alice")
    insert(engine, id="b1", user_id="bob")  # must not raise


# --- quota constraints: max 3 active globally -----------------------------


def test_fourth_active_instance_globally_rejected(engine):
    insert(engine, id="u1", user_id="u1")
    insert(engine, id="u2", user_id="u2")
    insert(engine, id="u3", user_id="u3")
    with pytest.raises(IntegrityError, match="global active-instance quota"):
        insert(engine, id="u4", user_id="u4")


def test_global_limit_follows_the_setting_after_a_restart(engine):
    # Boot with a limit of 1: a second active instance is refused.
    init_db(engine, max_active_instances=1)
    insert(engine, id="u1", user_id="u1")
    with pytest.raises(IntegrityError, match=r"global active-instance quota \(1\)"):
        insert(engine, id="u2", user_id="u2")
    # "Restart" with a limit of 5 on the same DB: the trigger is replaced.
    init_db(engine, max_active_instances=5)
    for n in range(2, 6):
        insert(engine, id=f"u{n}", user_id=f"u{n}")
    with pytest.raises(IntegrityError, match=r"global active-instance quota \(5\)"):
        insert(engine, id="u6", user_id="u6")


@pytest.mark.parametrize("bad_limit", [0, -1, True, "3"])
def test_global_limit_must_be_a_positive_integer(engine, bad_limit):
    with pytest.raises(ValueError):
        init_db(engine, max_active_instances=bad_limit)


def test_global_slot_frees_up_after_destroy(engine):
    insert(engine, id="u1", user_id="u1")
    insert(engine, id="u2", user_id="u2")
    insert(engine, id="u3", user_id="u3")
    with engine.begin() as conn:
        conn.execute(
            instances.update().where(instances.c.id == "u1").values(state="DESTROYED")
        )
    insert(engine, id="u4", user_id="u4")  # must not raise, a slot freed up


# --- concurrency: the actual M2 done-when criteria ------------------------


def _fire_concurrent_inserts(engine, rows: list[dict]) -> list[Exception | None]:
    """Fire `len(rows)` inserts from separate threads, lined up on a
    Barrier so they genuinely contend for SQLite's write lock rather than
    running sequentially by accident. Returns one result per row: None on
    success, the exception on failure.
    """
    barrier = threading.Barrier(len(rows))
    results: list[Exception | None] = [None] * len(rows)

    def worker(i: int, row: dict) -> None:
        barrier.wait()
        try:
            with engine.begin() as conn:
                conn.execute(instances.insert().values(**row))
        except Exception as exc:  # noqa: BLE001 - captured for the assertion, not swallowed
            results[i] = exc

    threads = [
        threading.Thread(target=worker, args=(i, row)) for i, row in enumerate(rows)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def test_concurrent_creates_same_user_only_one_succeeds(engine):
    rows = [instance_row(id=f"race-{i}", user_id="alice") for i in range(5)]
    results = _fire_concurrent_inserts(engine, rows)

    successes = [r for r in results if r is None]
    failures = [r for r in results if r is not None]
    assert len(successes) == 1, f"expected exactly 1 success, got {len(successes)}"
    assert len(failures) == 4
    assert all(isinstance(e, IntegrityError) for e in failures)


def test_concurrent_creates_global_cap_holds(engine):
    # 6 distinct users firing at once; only 3 should ever get an active row.
    rows = [instance_row(id=f"race-{i}", user_id=f"user-{i}") for i in range(6)]
    results = _fire_concurrent_inserts(engine, rows)

    successes = [r for r in results if r is None]
    failures = [r for r in results if r is not None]
    assert len(successes) == 3, f"expected exactly 3 successes, got {len(successes)}"
    assert len(failures) == 3
    assert all(isinstance(e, IntegrityError) for e in failures)
