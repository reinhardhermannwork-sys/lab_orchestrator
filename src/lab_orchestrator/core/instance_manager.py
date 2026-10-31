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
    FAILED -> CLEANUP -> DESTROYED, calling the lease's provider (M4,
    M13) and polling for readiness. Meant to run as an independent background
    task (see api/routes_instances.py for how it's kicked off and kept
    alive), not tied to the request that created the instance.

A third part came with M13 (architecture doc §18): **runs** inside a
lease -- `start_run()`, `list_runs()`, and `sync_runs()`, which copies the
provider's view of a lease's runs into the `runs` table (the janitor calls
it every pass, the API on every read).

Every DB call in this module runs through `starlette.concurrency.
run_in_threadpool`, per db/database.py's own note on why the engine is
synchronous: this is the "M5 wraps calls from async routes in a
thread-pool executor" it refers to.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Mapping
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool
from ulid import ULID

from lab_orchestrator import naming
from lab_orchestrator.adapters.provider import (
    RUN_FINISHED_STATES,
    HostCapacityError,
    InvalidIdentifierError,
    NotSupportedError,
    ProviderError,
    RunRefusedError,
    VMNotFoundError,
    encode_setup,
    validate_recipe,
)
from lab_orchestrator.core.janitor import transition_if
from lab_orchestrator.core.state_machine import Event, IllegalTransition, InstanceState, next_state
from lab_orchestrator.db.models import instances, runs, utcnow

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from lab_orchestrator.adapters.provider import MachineProvider, ProviderRegistry, RunInfo
    from lab_orchestrator.core.config import MachineRegistry, Settings

logger = logging.getLogger(__name__)

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


class InstanceNotFoundError(InstanceManagerError):
    """No such instance, or it belongs to a different user."""


class InvalidSetupError(InstanceManagerError):
    """The machine setup is not a small JSON object, or the machine's
    provider takes none."""


class RunsNotSupportedError(InstanceManagerError):
    """The lease's provider has no runs (a tux2lab VM)."""


class InstanceNotRunnableError(InstanceManagerError):
    """The lease is not READY (or connected), or its machine refused the
    run because another one is still going."""


class InvalidRecipeError(InstanceManagerError):
    """The recipe name fails the allowed-charset check."""


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
    case_id: str | None = None,
    setup: Mapping[str, Any] | None = None,
    providers: ProviderRegistry | None = None,
) -> dict[str, Any]:
    """Validate `machine_type`, enforce quota, insert a REQUESTED row.

    `case_id` and `setup` (M13) are stored on the row; a `setup` is
    checked here (a small JSON object, and with `providers` given, a
    provider that takes one), so a bad one is refused before any work.

    Raises `UnknownMachineTypeError`, `MachineDisabledError`,
    `InvalidSetupError`, `UserQuotaExceededError`, or
    `GlobalQuotaExceededError`. Returns the inserted row as a plain dict.
    """
    try:
        machine = machines.get_machine(machine_type)
    except KeyError:
        raise UnknownMachineTypeError(machine_type) from None
    if not machine.enabled:
        raise MachineDisabledError(machine_type)

    setup_json = None
    if setup is not None:
        if providers is not None and not providers.get(machine.provider).supports_setup:
            raise InvalidSetupError(f"machine_type '{machine_type}' takes no setup")
        try:
            setup_json = encode_setup(setup)
        except InvalidIdentifierError as exc:
            raise InvalidSetupError(str(exc)) from None

    now = utcnow()
    row: dict[str, Any] = {
        "id": str(ULID()),
        "user_id": user_id,
        "machine_type": machine_type,
        "provider": machine.provider,
        "case_id": case_id,
        "setup_json": setup_json,
        "state": InstanceState.REQUESTED.value,
        "created_at": now,
        "expires_at": now + timedelta(hours=machine.lease_lifetime_hours(settings)),
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
                "the maximum number of active instances is reached system-wide"
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


async def list_active_instances(*, engine: Engine, user_id: str) -> list[dict[str, Any]]:
    """`user_id`'s active (non-DESTROYED) instances, newest first. At most
    one in practice -- the per-user quota index (M2) guarantees it -- but
    returned as a list so the API shape doesn't depend on that rule.
    """
    stmt = (
        sa.select(instances)
        .where(
            instances.c.user_id == user_id,
            instances.c.state != InstanceState.DESTROYED.value,
        )
        .order_by(instances.c.created_at.desc())
    )

    def _select() -> list[dict[str, Any]]:
        with engine.connect() as conn:
            return [dict(row) for row in conn.execute(stmt).mappings()]

    return await run_in_threadpool(_select)


# --- release (early destroy, M9) --------------------------------------------

# Teardown already under way or finished: releasing again is a no-op.
_RELEASE_NOOP_STATES = {
    InstanceState.FAILED.value,
    InstanceState.CLEANUP.value,
    InstanceState.DESTROYING.value,
    InstanceState.DESTROYED.value,
}


async def release_instance(
    *, engine: Engine, instance_id: str, user_id: str
) -> dict[str, Any]:
    """End `user_id`'s lease early: USER_RELEASED -> DESTROYING. The
    janitor removes the VM on its next pass, exactly as for an expired
    lease.

    Raises `InstanceNotFoundError` if the instance doesn't exist *or*
    belongs to someone else -- the API answers 404 either way, so it
    doesn't reveal other users' instance ids. Idempotent: releasing an
    instance already in teardown returns it unchanged.

    The write is a compare-and-set (janitor.transition_if), like every
    other state write: provisioning or the janitor may move the row
    between our read and our write, in which case we re-read and decide
    again.
    """
    for _ in range(5):
        row = await get_instance(engine=engine, instance_id=instance_id)
        if row is None or row["user_id"] != user_id:
            raise InstanceNotFoundError(instance_id)
        if row["state"] in _RELEASE_NOOP_STATES:
            return row
        released = await transition_if(
            engine, instance_id, InstanceState(row["state"]), Event.USER_RELEASED
        )
        if released is not None:
            return dict(released)
    # Five lost races in a row means something is rewriting the row in a
    # tight loop -- a bug, not contention worth hiding.
    raise RuntimeError(f"could not release instance {instance_id}: state kept changing")


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
    providers: ProviderRegistry,
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
    provider = providers.get(row["provider"])
    setup = json.loads(row["setup_json"]) if row["setup_json"] is not None else None
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
                await provider.remove_idempotent(hostname)
            except ProviderError:
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

            if setup is None:
                await provider.install_idempotent(hostname, machine.image)
            else:
                await provider.install_idempotent(hostname, machine.image, setup)
            await _transition(Event.INSTALL_COMPLETE)

            await provider.start(hostname)
            await _transition(Event.START_ISSUED)

            deadline = time.monotonic() + settings.provisioning_timeout_seconds
            while True:
                info = await provider.info(hostname)
                healthy = info.vm_state == "running" and info.os_state == "healthy"
                # Probed only once healthy: a simulation's container answers
                # on its VNC port before its SECS host is online.
                reachable = healthy and bool(info.ip_address) and (
                    provider.simulated_reachability
                    or await _tcp_port_open(info.ip_address, machine.probe_port)
                )
                if healthy and reachable:
                    await _transition(
                        Event.READY_CRITERIA_MET, vm_ip=info.ip_address, ready_at=utcnow()
                    )
                    return
                if time.monotonic() >= deadline:
                    await _fail(
                        f"readiness criteria not met within {settings.provisioning_timeout_seconds}s "
                        f"(last seen: vm_state={info.vm_state!r}, os_state={info.os_state!r}, "
                        f"tcp_{machine.probe_port}_reachable={reachable})"
                    )
                    return
                await asyncio.sleep(settings.provisioning_poll_interval_seconds)

        except HostCapacityError as exc:
            # Shown to the user as-is by the frontend.
            await _fail(f"The lab is full right now, please try again later. ({exc})")
        except ProviderError as exc:
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


# --- runs inside a lease (M13, architecture doc §18) ------------------------

# Lease states in which the machine is up and may take a run.
RUNNABLE_STATES = {
    InstanceState.READY.value,
    InstanceState.CONNECTED.value,
    InstanceState.DISCONNECTED_GRACE.value,
}


async def _select_runs(engine: Engine, instance_id: str) -> list[dict[str, Any]]:
    stmt = sa.select(runs).where(runs.c.instance_id == instance_id).order_by(runs.c.id)

    def _select() -> list[dict[str, Any]]:
        with engine.connect() as conn:
            return [dict(r) for r in conn.execute(stmt).mappings()]

    return await run_in_threadpool(_select)


async def _update_run(engine: Engine, run_id: str, **values: Any) -> None:
    def _update() -> None:
        with engine.begin() as conn:
            conn.execute(runs.update().where(runs.c.id == run_id).values(**values))

    await run_in_threadpool(_update)


async def start_run(
    *,
    engine: Engine,
    providers: ProviderRegistry,
    instance_id: str,
    user_id: str,
    recipe: str,
) -> dict[str, Any]:
    """Start `recipe` on `user_id`'s lease as a new run and return its row.

    The row is written (queued) before the provider is asked, so a run the
    host accepted is never missing from the table; if the provider refuses
    or fails, the row is marked failed with the reason and the error is
    raised.

    Raises `InstanceNotFoundError` (unknown or someone else's lease),
    `RunsNotSupportedError`, `InvalidRecipeError`,
    `InstanceNotRunnableError` (lease not up, or a run still going), or
    the provider's `ProviderError`.
    """
    row = await get_instance(engine=engine, instance_id=instance_id)
    if row is None or row["user_id"] != user_id:
        raise InstanceNotFoundError(instance_id)
    provider = providers.get(row["provider"])
    if not provider.supports_runs:
        raise RunsNotSupportedError(f"machine_type '{row['machine_type']}' has no runs")
    try:
        validate_recipe(recipe)
    except InvalidIdentifierError as exc:
        raise InvalidRecipeError(str(exc)) from None
    if row["state"] not in RUNNABLE_STATES:
        raise InstanceNotRunnableError(f"instance is {row['state']}, not READY")
    # The table is the first check; the host checks again (it may know of
    # a run the table has not caught up with).
    if any(r["state"] not in RUN_FINISHED_STATES for r in await _select_runs(engine, instance_id)):
        raise InstanceNotRunnableError("another run of this instance is still going")

    run_row: dict[str, Any] = {
        "id": str(ULID()),
        "instance_id": instance_id,
        "recipe": recipe,
        "state": "queued",
        "created_at": utcnow(),
        "started_at": None,
        "ended_at": None,
        "result": None,
        "blobs_json": None,
    }

    def _insert() -> None:
        with engine.begin() as conn:
            conn.execute(runs.insert().values(**run_row))

    await run_in_threadpool(_insert)

    try:
        await provider.run(row["vm_hostname"], run_row["id"], recipe)
    except RunRefusedError as exc:
        await _update_run(engine, run_row["id"], state="failed", result=f"refused: {exc}")
        raise InstanceNotRunnableError(str(exc)) from exc
    except (ProviderError, NotSupportedError) as exc:
        await _update_run(engine, run_row["id"], state="failed", result=f"not started: {exc}")
        raise
    return run_row


def _run_values(info: RunInfo) -> dict[str, Any]:
    return {
        "state": info.state,
        "started_at": info.started_at,
        "ended_at": info.ended_at,
        "result": info.result,
        "blobs_json": json.dumps(info.blobs),
    }


async def sync_runs(*, engine: Engine, provider: MachineProvider, instance: Mapping[str, Any]) -> None:
    """Copy the provider's view of `instance`'s unfinished runs into the
    table. A lease that has left the runnable states ends its unfinished
    runs as failed ("lease ended"): the machine is gone or going, and
    nobody will report on them again.

    Provider errors are logged, not raised: the next sync tries again.
    """
    open_runs = [
        r for r in await _select_runs(engine, instance["id"]) if r["state"] not in RUN_FINISHED_STATES
    ]
    if not open_runs:
        return

    if instance["state"] not in RUNNABLE_STATES or instance["vm_hostname"] is None:
        reason = f"lease ended ({instance['state']})"
        if instance["failure_reason"]:
            reason += f": {instance['failure_reason']}"
        for run_row in open_runs:
            await _update_run(engine, run_row["id"], state="failed", ended_at=None, result=reason)
        return

    try:
        reported = {r.run_id: r for r in await provider.runs(instance["vm_hostname"])}
    except VMNotFoundError:
        return  # the watchdog fails the lease; the runs follow on the next sync
    except ProviderError:
        logger.exception("runs: could not read the runs of instance %s", instance["id"])
        return
    for run_row in open_runs:
        info = reported.get(run_row["id"])
        if info is None:
            continue  # not on the host (yet); the next sync looks again
        values = _run_values(info)
        if any(run_row[key] != value for key, value in values.items()):
            await _update_run(engine, run_row["id"], **values)
            if info.state != run_row["state"]:
                logger.info(
                    "runs: run %s (%s) of instance %s %s -> %s",
                    run_row["id"],
                    run_row["recipe"],
                    instance["id"],
                    run_row["state"],
                    info.state,
                )


async def list_runs(
    *, engine: Engine, providers: ProviderRegistry, instance_id: str, user_id: str | None
) -> list[dict[str, Any]]:
    """The runs of a lease, oldest first, freshly synced from its
    provider. With `user_id`, someone else's lease is
    `InstanceNotFoundError`.
    """
    row = await get_instance(engine=engine, instance_id=instance_id)
    if row is None or (user_id is not None and row["user_id"] != user_id):
        raise InstanceNotFoundError(instance_id)
    provider = providers.get(row["provider"])
    if provider.supports_runs:
        await sync_runs(engine=engine, provider=provider, instance=row)
    return await _select_runs(engine, instance_id)
