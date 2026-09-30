"""Startup reconciliation (M7, architecture doc §14 point 2).

State surviving a restart (SQLite) isn't the same as state being correct
after one. Before the app serves traffic or starts the janitor, this
compares the `instances` table against `tux2lab vm list` and handles the
three kinds of drift implementation plan §M7 names:

1. **Lease stuck mid-transition** (REQUESTED / PROVISIONING / STARTING /
   WAITING_READY, or FAILED): the task driving it died with the previous
   process, and nothing else would ever move it forward -- it would hold
   its user's (and a global) quota slot until lease expiry, or forever
   for FAILED. Moved to CLEANUP via `next_state()` with a
   `failure_reason`; the janitor then removes the VM and records
   DESTROYED, retrying on failure. This runs even if tux2lab is
   unreachable, since it only touches the DB.
2. **Active lease whose VM is gone** (READY / CONNECTED /
   DISCONNECTED_GRACE with no matching VM): logged only. The janitor
   still destroys it at lease expiry.
3. **VM on the host that no active lease claims**: logged only, never
   removed -- the host may run VMs the orchestrator doesn't own, so
   deleting one is a human decision.

Leases already in CLEANUP or DESTROYING are left to the janitor, which
retries both.

Every write is a compare-and-set through `janitor.transition_if`, the
same guard the janitor itself uses.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from starlette.concurrency import run_in_threadpool

from lab_orchestrator.adapters.tux2lab_client import Tux2LabError
from lab_orchestrator.core.janitor import transition_if
from lab_orchestrator.core.state_machine import Event, InstanceState
from lab_orchestrator.db.models import instances

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from lab_orchestrator.adapters.tux2lab_client import Tux2LabClient

logger = logging.getLogger(__name__)

Row = Mapping[str, Any]

# Leases with in-flight work that only a (now dead) provisioning task
# would have advanced.
_ORPHANED_STATES = (
    InstanceState.REQUESTED,
    InstanceState.PROVISIONING,
    InstanceState.STARTING,
    InstanceState.WAITING_READY,
    InstanceState.FAILED,
)

# Leases that should have a live VM behind them.
_VM_EXPECTED_STATES = (
    InstanceState.READY,
    InstanceState.CONNECTED,
    InstanceState.DISCONNECTED_GRACE,
)


@dataclass
class ReconcileReport:
    """What reconciliation found, for logging and tests."""

    cleaned_up: list[str] = field(default_factory=list)  # instance ids moved to CLEANUP
    missing_vms: list[str] = field(default_factory=list)  # instance ids whose VM is gone
    unknown_vms: list[str] = field(default_factory=list)  # hostnames no active lease claims
    vm_list_failed: bool = False  # tux2lab unreachable: checks 2 and 3 skipped


async def _load_active(engine: Engine) -> list[Row]:
    stmt = sa.select(instances).where(instances.c.state != InstanceState.DESTROYED.value)

    def _select() -> list[Row]:
        with engine.connect() as conn:
            return list(conn.execute(stmt).mappings())

    return await run_in_threadpool(_select)


async def _clean_up_orphan(engine: Engine, row: Row) -> bool:
    """Move an orphaned lease to CLEANUP. False if the row moved on."""
    state = InstanceState(row["state"])
    if state is not InstanceState.FAILED:
        reason = f"orchestrator restarted while instance was {state.value}"
        failed = await transition_if(engine, row["id"], state, Event.FAILED, failure_reason=reason)
        if failed is None:
            return False
    return await transition_if(
        engine, row["id"], InstanceState.FAILED, Event.START_CLEANUP
    ) is not None


async def reconcile_on_startup(*, engine: Engine, tux2lab: Tux2LabClient) -> ReconcileReport:
    """Reconcile the DB against tux2lab. Call before serving traffic.

    Never raises for tux2lab being unreachable: the DB-only step still
    runs, and the skipped VM comparison is logged at ERROR.
    """
    report = ReconcileReport()
    active = await _load_active(engine)

    for row in active:
        if InstanceState(row["state"]) not in _ORPHANED_STATES:
            continue
        if await _clean_up_orphan(engine, row):
            report.cleaned_up.append(row["id"])
            logger.warning(
                "reconcile: instance %s (user %s, VM %s) was %s when the orchestrator "
                "stopped; moved to CLEANUP, janitor will remove the VM",
                row["id"],
                row["user_id"],
                row["vm_hostname"] or "<none recorded>",
                row["state"],
            )

    try:
        vms = await tux2lab.list()
    except Tux2LabError:
        report.vm_list_failed = True
        logger.exception(
            "reconcile: could not list VMs from tux2lab; skipped checking for "
            "missing and unknown VMs"
        )
    else:
        on_host = {vm.hostname for vm in vms}
        claimed = {row["vm_hostname"] for row in active if row["vm_hostname"]}

        for row in active:
            if InstanceState(row["state"]) in _VM_EXPECTED_STATES and (
                row["vm_hostname"] not in on_host
            ):
                report.missing_vms.append(row["id"])
                logger.warning(
                    "reconcile: instance %s (user %s) is %s but its VM %s does not exist; "
                    "left as-is, janitor destroys it at lease expiry (%s)",
                    row["id"],
                    row["user_id"],
                    row["state"],
                    row["vm_hostname"],
                    row["expires_at"],
                )

        for hostname in sorted(on_host - claimed):
            report.unknown_vms.append(hostname)
            logger.warning(
                "reconcile: VM %s exists on the host but no active instance claims it; "
                "not removing it",
                hostname,
            )

    drift = report.cleaned_up or report.missing_vms or report.unknown_vms
    logger.log(
        logging.WARNING if drift or report.vm_list_failed else logging.INFO,
        "reconcile: done -- %d cleaned up, %d missing VMs, %d unknown VMs%s",
        len(report.cleaned_up),
        len(report.missing_vms),
        len(report.unknown_vms),
        " (VM list unavailable)" if report.vm_list_failed else "",
    )
    return report
