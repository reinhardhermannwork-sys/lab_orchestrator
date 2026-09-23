"""Schema creation and machine-definitions sync.

M2 scope: a small init script rather than hand-editing the DB file or
running migrations by hand — called from main.py's lifespan startup.
Both functions are idempotent, safe to call on every boot: `create_all`
skips tables that already exist; the sync upserts rather than inserts.
"""

from __future__ import annotations

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine

from lab_orchestrator.core.config import MachineRegistry
from lab_orchestrator.db.models import machine_definitions, metadata


def init_db(engine: Engine) -> None:
    """Create the schema (tables, constraints, indexes, trigger) if it
    doesn't already exist. Never drops or alters an existing table.
    """
    metadata.create_all(engine, checkfirst=True)


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
                tux2lab_image=definition.tux2lab_image,
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
