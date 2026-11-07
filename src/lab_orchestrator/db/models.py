"""Table definitions (architecture doc §9): machine_definitions, instances.

Two DB-level quota guarantees live here as real constraints, not app-level
`if` checks (implementation plan §M2 / architecture doc §14 point 1):

  - one active instance per user: a partial UNIQUE index on
    `instances.user_id`, covering only non-DESTROYED rows
  - at most N active instances per provider, across all users: one
    BEFORE INSERT trigger per provider that counts that provider's
    non-DESTROYED rows and aborts the insert past N
    (`LAB_ORCH_MAX_ACTIVE_INSTANCES`, default 3, for tux2lab)

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

from datetime import UTC, datetime

from sqlalchemy import (
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
    text,
)

from lab_orchestrator.adapters.provider import PROVIDERS
from lab_orchestrator.core.state_machine import InstanceState


def utcnow() -> datetime:
    """Naive UTC `datetime`, matching this module's storage convention
    (see the module docstring). Every writer of a timestamp column in
    this codebase should use this, not `datetime.utcnow()` directly
    (deprecated) or an aware `datetime.now(timezone.utc)` (would mix
    aware/naive values against everything read back from the DB).
    """
    return datetime.now(UTC).replace(tzinfo=None)


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
    Column("tux2lab_image", String, nullable=False),  # the machine's `image` (any provider)
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
    # M13 (architecture doc §18). Added to an older DB by init_db().
    # `provider` is copied from the machine at creation, so cleanup still
    # finds the right client if machines.yaml changes later.
    Column("provider", String, nullable=False, server_default=text("'tux2lab'")),
    Column("case_id", String, nullable=True),  # Fab Suite case the lease serves
    CheckConstraint(
        "state IN (" + ", ".join(repr(s.value) for s in InstanceState) + ")",
        name="ck_instances_state_valid",
    ),
)

# Columns init_db() adds to an `instances` table created before M13, as
# (name, SQLite column definition).
INSTANCES_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("provider", "VARCHAR NOT NULL DEFAULT 'tux2lab'"),
    ("case_id", "VARCHAR"),
)

# One active instance per user (architecture doc §9 / §14 point 1).
Index(
    "ux_instances_active_user",
    instances.c.user_id,
    unique=True,
    sqlite_where=text(ACTIVE_STATE_SQL),
)

# At most N active instances per provider (same section). SQLAlchemy Core
# has no native trigger construct, so this is raw SQL. N is a setting
# (sized to the host, architecture doc §17/§18), so init_db() drops and
# recreates the triggers on every boot: a changed limit takes effect on
# restart. The message keeps its pre-M13 "global active-instance quota"
# wording ("global" = across all users), which instance_manager matches.
MAX_ACTIVE_TRIGGER_PREFIX = "trg_instances_max_active_"

# The single pre-M13 trigger; init_db() drops it.
MAX_GLOBAL_ACTIVE_TRIGGER = "trg_instances_max_global_active"


def max_active_trigger_name(provider: str) -> str:
    return MAX_ACTIVE_TRIGGER_PREFIX + provider.replace("-", "_")


def max_active_trigger_sql(provider: str, limit: int) -> str:
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider {provider!r}")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError(f"max active instances must be an integer >= 1, got {limit!r}")
    return f"""
    CREATE TRIGGER {max_active_trigger_name(provider)}
    BEFORE INSERT ON instances
    WHEN NEW.provider = '{provider}' AND (
        SELECT COUNT(*) FROM instances WHERE {ACTIVE_STATE_SQL} AND provider = '{provider}'
    ) >= {limit}
    BEGIN
        SELECT RAISE(ABORT, 'global active-instance quota ({limit}) exceeded for {provider}');
    END;
    """
