"""Background lease cleanup for M6.

The janitor is deliberately a small coordinator around the existing state
machine and tux2lab adapter. It never invents lifecycle transitions:
expiration uses LIFETIME_EXPIRED, disconnect grace uses GRACE_EXPIRED, and
successful teardown uses DESTROY_COMPLETE.

A cleanup claim is a conditional UPDATE on both instance id and the state
that was observed. This matters because provisioning/API work can run at
the same time as the janitor; a stale read must never overwrite a newer
state transition.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import TYPE_CHECKING

import sqlalchemy as sa
from starlette.concurrency import run_in_threadpool

from lab_orchestrator.adapters.tux2lab_client import Tux2LabError, VMNotFoundError
from lab_orchestrator.core.state_machine import Event, InstanceState, next_state
from lab_orchestrator.db.models import instances, utcnow

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from lab_orchestrator.adapters.tux2lab_client import Tux2LabClient
    from lab_orchestrator.core.config import Settings

logger = logging.getLogger(__name__)

_CLEANUP_RETRY_STATE = InstanceState.DESTROYING
_EXPIRING_STATES = (
    InstanceState.REQUESTED,
    InstanceState.PROVISIONING,
    InstanceState.STARTING,
    InstanceState.WAITING_READY,
    InstanceState.READY,
    InstanceState.CONNECTED,
    InstanceState.DISCONNECTED_GRACE,
)


async def _claim_transition(
    *,
    engine: Engine,
    instance_id: str,
    current_state: InstanceState,
    event: Event,
) -> bool:
    """Atomically claim an instance for the transition implied by event."""
    new_state = next_state(current_state, event)

    def _update() -> bool:
        with engine.begin() as conn:
            result = conn.execute(
                instances.update()
                .where(
                    sa.and_(
                        instances.c.id == instance_id,
                        instances.c.state == current_state.value,
                    )
                )
                .values(state=new_state.value)
            )
            return result.rowcount == 1

    return await run_in_threadpool(_update)


async def _mark_destroyed(*, engine: Engine, instance_id: str) -> bool:
    """Record DESTROY_COMPLETE only if this janitor still owns DESTROYING."""
    destroyed_at = utcnow()

    def _update() -> bool:
        with engine.begin() as conn:
            result = conn.execute(
                instances.update()
                .where(
                    sa.and_(
                        instances.c.id == instance_id,
                        instances.c.state == _CLEANUP_RETRY_STATE.value,
                    )
                )
                .values(state=InstanceState.DESTROYED.value, destroyed_at=destroyed_at)
            )
            return result.rowcount == 1

    return await run_in_threadpool(_update)


async def _cleanup_one(
    *,
    engine: Engine,
    tux2lab: Tux2LabClient,
    row: dict,
) -> bool:
    """Remove the VM, then mark the DB row DESTROYED.

    VMNotFoundError is safe here: the desired external state is already
    true. Other tux2lab failures leave the row in DESTROYING so a later
    janitor pass can retry it.
    """
    hostname = row["vm_hostname"]
    if hostname is not None:
        try:
            await tux2lab.remove_idempotent(hostname)
        except VMNotFoundError:
            logger.info("VM %s was already absent during janitor cleanup", hostname)
        except Tux2LabError:
            logger.exception(
                "tux2lab cleanup failed for instance %s (VM %s); "
                "leaving instance in DESTROYING for retry",
                row["id"],
                hostname,
            )
            return False

    return await _mark_destroyed(engine=engine, instance_id=row["id"])


async def run_once(*, engine: Engine, tux2lab: Tux2LabClient, settings: Settings) -> int:
    """Run one cleanup pass and return the number of instances destroyed."""
    now = utcnow()
    grace_cutoff = now - timedelta(seconds=settings.disconnect_grace_seconds)

    expiring = [state.value for state in _EXPIRING_STATES]

    def _load_candidates() -> list[dict]:
        with engine.connect() as conn:
            rows = conn.execute(
                sa.select(instances).where(
                    sa.or_(
                        instances.c.state == InstanceState.DESTROYING.value,
                        sa.and_(
                            instances.c.state.in_(expiring),
                            instances.c.expires_at <= now,
                        ),
                        sa.and_(
                            instances.c.state == InstanceState.DISCONNECTED_GRACE.value,
                            instances.c.disconnect_since.is_not(None),
                            instances.c.disconnect_since <= grace_cutoff,
                        ),
                    )
                )
            ).mappings()
            return [dict(row) for row in rows]

    candidates = await run_in_threadpool(_load_candidates)
    destroyed = 0

    for row in candidates:
        state = InstanceState(row["state"])

        if state == InstanceState.DESTROYING:
            claimed = True
        else:
            event = (
                Event.LIFETIME_EXPIRED
                if row["expires_at"] <= now
                else Event.GRACE_EXPIRED
            )
            claimed = await _claim_transition(
                engine=engine,
                instance_id=row["id"],
                current_state=state,
                event=event,
            )

        if not claimed:
            # Another task changed the row after our SELECT. Never use the
            # stale row to perform a destructive DB transition.
            continue

        if await _cleanup_one(engine=engine, tux2lab=tux2lab, row=row):
            destroyed += 1

    return destroyed


async def janitor_loop(*, engine: Engine, tux2lab: Tux2LabClient, settings: Settings) -> None:
    """Run cleanup passes until cancelled by application shutdown."""
    while True:
        try:
            await run_once(engine=engine, tux2lab=tux2lab, settings=settings)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("janitor pass failed; will retry on the next pass")
        await asyncio.sleep(settings.janitor_poll_interval_seconds)
