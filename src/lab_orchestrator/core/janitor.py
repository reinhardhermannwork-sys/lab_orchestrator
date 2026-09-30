"""Background lease cleanup (M6, architecture doc §7).

Every `janitor_poll_interval_seconds`, find instances whose lease has run
out (`now >= expires_at`) or whose disconnect grace period has elapsed
(`disconnect_since` older than `disconnect_grace_seconds`), move them to
DESTROYING, remove the VM through tux2lab, and record DESTROYED.

The janitor never writes a state string of its own: every change goes
through `core.state_machine.next_state()` (LIFETIME_EXPIRED,
GRACE_EXPIRED, DESTROY_COMPLETE).

Every write is a compare-and-set on (id, observed state). Provisioning
and API work run concurrently with the janitor, so a row read at the
start of a pass may be stale by the time we act on it; if the guarded
UPDATE matches nothing, somebody else moved the row and we leave it
alone. The claim uses `RETURNING` so cleanup acts on the row as it was
at claim time (e.g. a `vm_hostname` written after our SELECT), never on
the stale candidate read.

Rows left in DESTROYING -- a failed tux2lab remove, or a crash between
claim and completion -- are picked up again on the next pass.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from starlette.concurrency import run_in_threadpool

from lab_orchestrator.adapters.tux2lab_client import Tux2LabError, VMNotFoundError
from lab_orchestrator.core.state_machine import (
    Event,
    IllegalTransition,
    InstanceState,
    next_state,
)
from lab_orchestrator.db.models import instances, utcnow

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from lab_orchestrator.adapters.tux2lab_client import Tux2LabClient
    from lab_orchestrator.core.config import Settings

logger = logging.getLogger(__name__)

Row = Mapping[str, Any]


def _states_accepting(event: Event) -> tuple[InstanceState, ...]:
    """Every state with a legal transition on `event`.

    Derived from the state machine rather than copied, so the janitor's
    query can't drift from the transition table.
    """
    accepting = []
    for state in InstanceState:
        try:
            next_state(state, event)
        except IllegalTransition:
            continue
        accepting.append(state)
    return tuple(accepting)


_LEASE_EXPIRABLE_STATES = _states_accepting(Event.LIFETIME_EXPIRED)


async def _transition_if(
    engine: Engine,
    instance_id: str,
    current: InstanceState,
    event: Event,
    **extra_columns: Any,
) -> Row | None:
    """Apply `event` only if the row is still in `current`.

    Returns the updated row, or None if the row had already moved on.
    """
    values = {"state": next_state(current, event).value, **extra_columns}
    stmt = (
        instances.update()
        .where(instances.c.id == instance_id, instances.c.state == current.value)
        .values(**values)
        .returning(*instances.c)
    )

    def _update() -> Row | None:
        with engine.begin() as conn:
            return conn.execute(stmt).mappings().one_or_none()

    return await run_in_threadpool(_update)


def _expiry_event(row: Row, *, now: datetime) -> Event:
    """Why `row` is being cleaned up. Lease expiry wins if both apply."""
    if row["expires_at"] <= now:
        return Event.LIFETIME_EXPIRED
    return Event.GRACE_EXPIRED


async def _load_candidates(engine: Engine, *, now: datetime, grace_cutoff: datetime) -> list[Row]:
    stmt = sa.select(instances).where(
        sa.or_(
            instances.c.state == InstanceState.DESTROYING.value,
            sa.and_(
                instances.c.state.in_([s.value for s in _LEASE_EXPIRABLE_STATES]),
                instances.c.expires_at <= now,
            ),
            sa.and_(
                instances.c.state == InstanceState.DISCONNECTED_GRACE.value,
                instances.c.disconnect_since <= grace_cutoff,
            ),
        )
    )

    def _select() -> list[Row]:
        with engine.connect() as conn:
            return list(conn.execute(stmt).mappings())

    return await run_in_threadpool(_select)


async def _claim(engine: Engine, row: Row, *, now: datetime) -> Row | None:
    """Move `row` into DESTROYING, or return it as-is if it already is."""
    state = InstanceState(row["state"])
    if state is InstanceState.DESTROYING:
        return row  # retry of an earlier pass
    event = _expiry_event(row, now=now)
    claimed = await _transition_if(engine, row["id"], state, event)
    if claimed is not None:
        logger.info(
            "janitor: instance %s %s -> DESTROYING (%s)", row["id"], state.value, event.value
        )
    return claimed


async def _destroy(engine: Engine, tux2lab: Tux2LabClient, row: Row) -> bool:
    """Remove the VM, then record DESTROYED. False means retry next pass.

    VMNotFoundError counts as success: the VM being gone is exactly the
    outcome we want.
    """
    hostname = row["vm_hostname"]
    if hostname is not None:
        try:
            await tux2lab.remove_idempotent(hostname)
        except VMNotFoundError:
            logger.info("janitor: VM %s was already gone", hostname)
        except Tux2LabError:
            logger.exception(
                "janitor: removing VM %s for instance %s failed; will retry",
                hostname,
                row["id"],
            )
            return False

    destroyed = await _transition_if(
        engine,
        row["id"],
        InstanceState.DESTROYING,
        Event.DESTROY_COMPLETE,
        destroyed_at=utcnow(),
    )
    if destroyed is None:
        logger.warning("janitor: instance %s left DESTROYING during cleanup", row["id"])
        return False
    logger.info("janitor: instance %s DESTROYED", row["id"])
    return True


async def run_once(*, engine: Engine, tux2lab: Tux2LabClient, settings: Settings) -> int:
    """Run one cleanup pass and return the number of instances destroyed.

    Each candidate is handled independently: an unexpected error on one
    row is logged and does not stop the rest of the pass.
    """
    now = utcnow()
    grace_cutoff = now - timedelta(seconds=settings.disconnect_grace_seconds)
    destroyed = 0

    for candidate in await _load_candidates(engine, now=now, grace_cutoff=grace_cutoff):
        try:
            row = await _claim(engine, candidate, now=now)
            if row is not None and await _destroy(engine, tux2lab, row):
                destroyed += 1
        except Exception:
            logger.exception("janitor: unexpected error cleaning up instance %s", candidate["id"])

    return destroyed


async def janitor_loop(*, engine: Engine, tux2lab: Tux2LabClient, settings: Settings) -> None:
    """Run cleanup passes until cancelled by application shutdown."""
    logger.info("janitor started (interval %.0fs)", settings.janitor_poll_interval_seconds)
    while True:
        try:
            await run_once(engine=engine, tux2lab=tux2lab, settings=settings)
        except Exception:
            logger.exception("janitor pass failed; will retry on the next pass")
        await asyncio.sleep(settings.janitor_poll_interval_seconds)
