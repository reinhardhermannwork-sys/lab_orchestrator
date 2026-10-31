"""Schema creation and machine-definitions sync.

M2 scope: a small init script rather than hand-editing the DB file or
running migrations by hand — called from main.py's lifespan startup.
Both functions are idempotent, safe to call on every boot: `create_all`
skips tables that already exist; the sync upserts rather than inserts.
"""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine

from lab_orchestrator.adapters.provider import PROVIDERS
from lab_orchestrator.core.config import MachineRegistry
from lab_orchestrator.db.models import (
    INSTANCES_ADDED_COLUMNS,
    MAX_ACTIVE_TRIGGER_PREFIX,
    MAX_GLOBAL_ACTIVE_TRIGGER,
    machine_definitions,
    max_active_trigger_sql,
    metadata,
)

# A provider's limit when the caller names none (M13: a simulated Mercury
# is CPU-bound, so one at a time until measured).
DEFAULT_PROVIDER_LIMITS = {"tux2lab": 3, "mercury-sim": 1}


def init_db(
    engine: Engine,
    max_active_instances: int | None = None,
    provider_limits: Mapping[str, int] | None = None,
) -> None:
    """Create the schema (tables, constraints, indexes) if it doesn't
    already exist, add the M13 columns to an older `instances` table, and
    (re)create one active-limit trigger per provider. Never drops an
    existing table or column.

    Limits: `provider_limits` per provider, else the defaults above;
    `max_active_instances` (the pre-M13 single setting) is tux2lab's and
    wins over both.
    """
    limits = {**DEFAULT_PROVIDER_LIMITS, **(provider_limits or {})}
    if max_active_instances is not None:
        limits["tux2lab"] = max_active_instances
    trigger_sqls = [max_active_trigger_sql(p, limits[p]) for p in PROVIDERS]

    metadata.create_all(engine, checkfirst=True)
    with engine.begin() as conn:
        existing = {row[1] for row in conn.execute(text("PRAGMA table_info(instances)"))}
        for name, definition in INSTANCES_ADDED_COLUMNS:
            if name not in existing:
                conn.execute(text(f"ALTER TABLE instances ADD COLUMN {name} {definition}"))

        stale = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type = 'trigger' AND (name = :old OR name LIKE :prefix)"),
            {"old": MAX_GLOBAL_ACTIVE_TRIGGER, "prefix": MAX_ACTIVE_TRIGGER_PREFIX + "%"},
        ).scalars().all()
        for name in stale:
            conn.execute(text(f"DROP TRIGGER IF EXISTS {name}"))
        for sql in trigger_sqls:
            conn.execute(text(sql))


def sync_machine_definitions(engine: Engine, machines: MachineRegistry) -> None:
    """Upsert config/machines.yaml's machines into `machine_definitions`,
    so `instances.machine_type` has a real FK target and admin/SQL
    tooling can join against it. Never deletes a row that's since been
    removed from the yaml — a historical instance may still reference it.
    """
    with engine.begin() as conn:
        for machine_type, definition in machines.items():
            stmt = sqlite_insert(machine_definitions).values(
                id=machine_type,
                display_name=definition.display_name,
                code=definition.code,
                codename=definition.codename,
                tux2lab_image=definition.image,
                protocol=definition.protocol,
                enabled=definition.enabled,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["id"],
                set_={
                    "display_name": stmt.excluded.display_name,
                    "code": stmt.excluded.code,
                    "codename": stmt.excluded.codename,
                    "tux2lab_image": stmt.excluded.tux2lab_image,
                    "protocol": stmt.excluded.protocol,
                    "enabled": stmt.excluded.enabled,
                },
            )
            conn.execute(stmt)
