"""Orchestration logic: create/get an instance, and drive provisioning.

Two halves, deliberately different in shape:

  - `create_instance()` is the *fast* path — validates the machine type,
    inserts a REQUESTED row, and returns. It's meant to complete within
    a request/response cycle so `POST /v1/instances` can return `202`
    immediately (architecture doc §8: "Asynchronous by design — POST
    never blocks on provisioning"). DB-level quota enforcement (M2) does
    the actual enforcing here; this function's job is just translating
    the resulting `IntegrityError` into a clear, specific exception for
    the API layer.

  - `provision_instance()` is the *slow* path — drives a REQUESTED
    instance through the full state machine (M3) to READY or to
    FAILED -> CLEANUP -> DESTROYED, calling the tux2lab adapter (M4) and
    polling for readiness. Meant to run as an independent background
    task (see api/routes_instances.py for how it's kicked off and kept
    alive), not tied to the request that created the instance.

Every DB call in this module runs through `starlette.concurrency.
run_in_threadpool`, per db/database.py's own note on why the engine is
synchronous: this is the "M5 wraps calls from async routes in a
thread-pool executor" it refers to.
"""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool
from ulid import ULID

from lab_orchestrator import naming
from lab_orchestrator.adapters.tux2lab_client import Tux2LabError
from lab_orchestrator.core.state_machine import Event, IllegalTransition, InstanceState, next_state
from lab_orchestrator.db.models import instances, utcnow

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from lab_orchestrator.adapters.tux2lab_client import Tux2LabClient
    from lab_orchestrator.core.config import MachineRegistry, Settings

# --- exceptions ----------------------------------------------------------


class InstanceManagerError(Exception):
    """Base class for every error this module raises."""


class UnknownMachineTypeError(InstanceManagerError):
    """The requested `machine_type` isn't in config/machines.yaml at all."""


class MachineDisabledError(InstanceManagerError):
    """The requested `machine_type` exists but `enabled: false`."""


class QuotaExceededError(InstanceManagerError):
    """A DB-level quota constraint (M2) rejected the insert."""


class UserQuotaExceededError(QuotaExceededError):
    """This user already has an active instance."""


class GlobalQuotaExceededError(QuotaExceededError):
    """3 instances are already active system-wide."""


class _Superseded(Exception):
    """Internal to `provision_instance()`: the row is no longer in the
    state this task last wrote, because something else (the janitor)
    took it over. Never escapes the module.
    """


# --- create (fast path) ---------------------------------------------------


async def create_instance(
    *,
    engine: Engine,
    machines: MachineRegistry,
    settings: Settings,
    user_id: str,
    machine_type: str,
) -> dict[str, Any]:
    """Validate `machine_type`, enforce quota, insert a REQUESTED row.

    Raises `UnknownMachineTypeError`, `MachineDisabledError`,
    `UserQuotaExceededError`, or `GlobalQuotaExceededError`. Returns the
    inserted row as a plain dict.
    """
    try:
        machine = machines.get_machine(machine_type)
    except KeyError:
        raise UnknownMachineTypeError(machine_type) from None
    if not machine.enabled:
        raise MachineDisabledError(machine_type)

    now = utcnow()
    row: dict[str, Any] = {
        "id": str(ULID()),
        "user_id": user_id,
        "machine_type": machine_type,
        "state": InstanceState.REQUESTED.value,
        "created_at": now,
        "expires_at": now + timedelta(hours=settings.lease_lifetime_hours),
    }

    def _insert() -> None:
        with engine.begin() as conn:
            conn.execute(instances.insert().values(**row))

    try:
        await run_in_threadpool(_insert)
    except IntegrityError as exc:
        # Both quota guarantees (M2) raise IntegrityError; distinguished
        # here only by the trigger's own message text, since that's the
        # one place they differ. A user-quota violation carries the
        # partial unique index's generic SQLite message instead.
        if "global active-instance quota" in str(exc):
            raise GlobalQuotaExceededError(
                "3 instances are already active system-wide"
            ) from exc
        raise UserQuotaExceededError(f"user '{user_id}' already has an active instance") from exc

    # Architecture doc §8: the 202 response already reports PROVISIONING,
    # not REQUESTED -- so this transition happens synchronously, still
    # within the request/response cycle, before provision_instance (the
    # background task) does any actual tux2lab work. REQUESTED still
    # exists as a real, briefly-persisted row state (next_state() still
    # validates the transition), it just doesn't outlive this function.
    new_state = next_state(InstanceState.REQUESTED, Event.START_PROVISIONING)

    def _mark_provisioning() -> None:
        with engine.begin() as conn:
            conn.execute(
                instances.update().where(instances.c.id == row["id"]).values(state=new_state.value)
            )

    await run_in_threadpool(_mark_provisioning)
    row["state"] = new_state.value

    return row


# --- get -------------------------------------------------------------------


async def get_instance(*, engine: Engine, instance_id: str) -> dict[str, Any] | None:
    """Return the current row for `instance_id`, or `None` if it doesn't
    exist. A plain dict, not an ORM object — this codebase uses
    SQLAlchemy Core throughout, not the ORM (see db/models.py).
    """

    def _select() -> dict[str, Any] | None:
        with engine.connect() as conn:
            row = conn.execute(
                sa.select(instances).where(instances.c.id == instance_id)
            ).mappings().first()
        return dict(row) if row is not None else None

    return await run_in_threadpool(_select)


# --- provision (slow path, background task) --------------------------------


async def _tcp_port_open(host: str, port: int, timeout: float = 3.0) -> bool:
    """Plain TCP-connect probe. Deliberately not part of the tux2lab
    adapter (M4) — tux2lab has no way to know how reachable a VM is from
    the orchestrator container's specific network position, so this
    belongs here, in the caller that actually has that vantage point.
    """
    try:
        _reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
    except (OSError, TimeoutError):
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return True


async def provision_instance(
    *,
    engine: Engine,
    machines: MachineRegistry,
    tux2lab: Tux2LabClient,
    settings: Settings,
    instance_id: str,
) -> None:
    """Drive `instance_id` from REQUESTED to READY, or to
    FAILED -> CLEANUP -> DESTROYED on any failure along the way.

    Every state change goes through `core.state_machine.next_state()`
    (M3) rather than writing a state string directly — an
    `IllegalTransition` here means a real bug in this function's own
    call ordering, and is deliberately *not* caught alongside the
    expected-failure paths below, so it surfaces loudly instead of being
    mistaken for a VM provisioning failure.

    Every UPDATE is a compare-and-set on the state this task last wrote.
    The janitor (M6) can move the row to DESTROYING/DESTROYED while this
    task is still running -- e.g. the lease expires mid-provisioning. When
    that happens this task stops touching the row and only removes the VM
    it created: if the janitor cleaned up while `install` was still in
    flight, its remove found nothing, and the VM only appeared afterwards
    -- so this task is the only one that knows the VM exists.
    """
    row = await get_instance(engine=engine, instance_id=instance_id)
    if row is None:
        return  # defensive only; shouldn't happen in normal operation
    machine = machines.get_machine(row["machine_type"])
    # Already PROVISIONING by the time this runs -- create_instance()
    # does the REQUESTED -> PROVISIONING transition synchronously, before
    # returning 202, so the response already reflects it (architecture
    # doc §8). This function picks up from there.
    state = row["state"]
    hostname: str | None = None

    async def _write(**values: Any) -> None:
        """Compare-and-set `values` on the row, guarded by `state`."""

        def _update() -> bool:
            with engine.begin() as conn:
                result = conn.execute(
                    instances.update()
                    .where(instances.c.id == instance_id, instances.c.state == state)
                    .values(**values)
                )
                return result.rowcount == 1

        if not await run_in_threadpool(_update):
            raise _Superseded

    async def _transition(event: Event, **extra_columns: Any) -> None:
        nonlocal state
        new_state = next_state(InstanceState(state), event)
        await _write(state=new_state.value, **extra_columns)
        state = new_state.value

    async def _remove_vm() -> None:
        if hostname is not None:
            try:
                await tux2lab.remove_idempotent(hostname)
            except Tux2LabError:
                pass  # best-effort cleanup; the real failure_reason is already recorded

    async def _fail(reason: str) -> None:
        await _transition(Event.FAILED, failure_reason=reason)
        await _transition(Event.START_CLEANUP)
        await _remove_vm()
        await _transition(Event.DESTROY_COMPLETE, destroyed_at=utcnow())

    async def _provision() -> None:
        nonlocal hostname
        try:
            # from lab_orchestrator import naming (module-level, above) so
            # tests can monkeypatch naming.generate_hostname and have this
            # call see it -- a bound `from ... import generate_hostname`
            # would capture the original function object permanently.
            hostname = naming.generate_hostname(machine)
            # Recorded *before* install, not with INSTALL_COMPLETE: if the
            # process dies mid-install, the VM may exist, and startup
            # reconciliation (M7) / the janitor can only remove it if the
            # row names it.
            await _write(vm_hostname=hostname)

            await tux2lab.install_idempotent(hostname, machine.tux2lab_image)
            await _transition(Event.INSTALL_COMPLETE)

            await tux2lab.start(hostname)
            await _transition(Event.START_ISSUED)

            deadline = time.monotonic() + settings.provisioning_timeout_seconds
            while True:
                info = await tux2lab.info(hostname)
                reachable = bool(info.ip_address) and await _tcp_port_open(info.ip_address, 22)
                if info.vm_state == "running" and info.os_state == "healthy" and reachable:
                    await _transition(
                        Event.READY_CRITERIA_MET, vm_ip=info.ip_address, ready_at=utcnow()
                    )
                    return
                if time.monotonic() >= deadline:
                    await _fail(
                        f"readiness criteria not met within {settings.provisioning_timeout_seconds}s "
                        f"(last seen: vm_state={info.vm_state!r}, os_state={info.os_state!r}, "
                        f"tcp_22_reachable={reachable})"
                    )
                    return
                await asyncio.sleep(settings.provisioning_poll_interval_seconds)

        except Tux2LabError as exc:
            await _fail(str(exc))
        except (IllegalTransition, _Superseded):
            raise  # IllegalTransition: a real bug here -- surface it loudly
        except Exception as exc:
            # Anything else unexpected: still land the row in a terminal
            # state rather than leaving it stuck mid-transition forever
            # (implementation plan §3: "a failed provisioning attempt should
            # land the instance in FAILED, not leave it stuck"), but
            # re-raise so the exception is still visible/logged, not silently
            # swallowed.
            await _fail(f"unexpected error: {exc}")
            raise

    try:
        await _provision()
    except _Superseded:
        # The janitor owns the row now; leave it alone, but don't orphan
        # the VM (see docstring).
        await _remove_vm()
