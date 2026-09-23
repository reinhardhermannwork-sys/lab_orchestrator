"""Table definitions (architecture doc §9): machine_definitions, instances.

Two DB-level quota guarantees live here as real constraints, not app-level
`if` checks (implementation plan §M2 / architecture doc §14 point 1):

  - one active instance per user: a partial UNIQUE index on
    `instances.user_id`, covering only non-DESTROYED rows
  - at most 3 active instances globally: a BEFORE INSERT trigger that
    counts non-DESTROYED rows and aborts the insert past 3

Both rely on SQLite's ordinary single-writer locking to make the
check-and-insert atomic (see db/database.py for why that's deliberate).

"Active" == `state != 'DESTROYED'`. That's only a safe definition of
"active" because DESTROYED is a true dead end with no outgoing
transition (core/state_machine.py) — nothing ever UPDATEs a row's state
back out of DESTROYED, so the only way the active-row count can grow is
via INSERT, which is exactly what the trigger guards. The per-user
partial index doesn't need the same INSERT-only reasoning: SQLite
re-validates a unique index (partial or not) on UPDATE too, so it stays
correct regardless.

`machine_definitions.id` intentionally holds the machine_type key from
config/machines.yaml (e.g. "machine_1"), not a surrogate integer —
`instances.machine_type` is a plain FK to it, so the same string is used
throughout config, API, and DB. FK enforcement is off by default in
SQLite; db/database.py turns it on per-connection via PRAGMA.

All timestamps are stored as naive UTC datetimes (no tzinfo). SQLite has
no native timezone-aware datetime type, and mixing aware/naive values is
a classic footgun — every reader/writer in this codebase should treat a
datetime read from this DB as UTC and never attach tzinfo to it.
"""

from __future__ import annotations

from sqlalchemy import (
    DDL,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    MetaData,
    String,
    Table,
    Text,
    event,
    text,
)

from lab_orchestrator.core.state_machine import InstanceState

metadata = MetaData()

# Single source of truth for what "active" means at the DB layer, reused
# verbatim by both the partial index and the trigger below.
ACTIVE_STATE_SQL = "state != 'DESTROYED'"

machine_definitions = Table(
    "machine_definitions",
    metadata,
    Column("id", String, primary_key=True),  # the machine_type key, e.g. "machine_1"
    Column("display_name", String, nullable=False),
    Column("code", String, nullable=False, unique=True),
    Column("codename", String, nullable=False, unique=True),
    Column("tux2lab_image", String, nullable=False),
    Column("protocol", String, nullable=False),
    Column("enabled", Boolean, nullable=False, default=True),
)

instances = Table(
    "instances",
    metadata,
    Column("id", String, primary_key=True),  # ULID, assigned by the app (M5)
    Column("user_id", String, nullable=False),
    Column("machine_type", String, ForeignKey("machine_definitions.id"), nullable=False),
    Column("vm_hostname", String, nullable=True),
    Column("vm_ip", String, nullable=True),
    Column("state", String, nullable=False, default=InstanceState.REQUESTED.value),
    Column("created_at", DateTime, nullable=False),
    Column("ready_at", DateTime, nullable=True),
    Column("expires_at", DateTime, nullable=False),
    Column("disconnect_since", DateTime, nullable=True),
    Column("guacamole_connection_id", String, nullable=True),
    Column("destroyed_at", DateTime, nullable=True),
    Column("failure_reason", Text, nullable=True),
    CheckConstraint(
        "state IN (" + ", ".join(repr(s.value) for s in InstanceState) + ")",
        name="ck_instances_state_valid",
    ),
)

# One active instance per user (architecture doc §9 / §14 point 1).
Index(
    "ux_instances_active_user",
    instances.c.user_id,
    unique=True,
    sqlite_where=text(ACTIVE_STATE_SQL),
)

# At most 3 active instances globally (same section). SQLAlchemy Core has
# no native trigger construct, so this is raw DDL attached to fire right
# after the table itself is created — idempotent (IF NOT EXISTS) so a
# repeat `create_all()` on every boot is harmless.
_max_global_active_trigger = DDL(
    f"""
    CREATE TRIGGER IF NOT EXISTS trg_instances_max_global_active
    BEFORE INSERT ON instances
    WHEN (SELECT COUNT(*) FROM instances WHERE {ACTIVE_STATE_SQL}) >= 3
    BEGIN
        SELECT RAISE(ABORT, 'global active-instance quota (3) exceeded');
    END;
    """
)
event.listen(instances, "after_create", _max_global_active_trigger)
