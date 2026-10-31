"""/v1/instances: create, get, list by user, release (architecture doc §8).

Open API-design questions settled here, since neither was explicitly
resolved in the architecture doc (§14.6, §14.7):

- **`POST` while the user already has an active instance**: rejected
  outright (`409`, via `UserQuotaExceededError`), not returned as if it
  were the existing instance. The DB-level quota constraint (M2) already
  makes this the natural behavior — returning the existing instance
  instead would need an extra lookup this endpoint doesn't otherwise
  need, for a v1 prototype with no stated need for that convenience.
- **List/delete endpoints** (§14.7, decided for M9): `GET
  /v1/instances?user=` lists that user's active instances, and `DELETE
  /v1/instances/{id}?user=` releases one early. Both require `user`:
  there is no unfiltered listing. A release by anyone but the owner is a
  404, indistinguishable from an unknown id. The API is still
  unauthenticated and internal-only (§17); these checks back up the web
  frontend's own, they don't replace authentication.

M13 (architecture doc §18) adds runs inside a lease: `POST
/v1/instances/{id}/runs` starts a recipe on the lease's machine (202,
the run queued), `GET /v1/instances/{id}/runs` lists them, freshly synced
from the provider. Both need the owner's `user`, as release does.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status

from lab_orchestrator.adapters.provider import ProviderError
from lab_orchestrator.api.schemas import (
    CreateInstanceRequest,
    CreateInstanceResponse,
    CreateRunRequest,
    InstanceResponse,
    RunResponse,
    SSHConnectionInfo,
)
from lab_orchestrator.core import instance_manager
from lab_orchestrator.core.state_machine import InstanceState

router = APIRouter()

# States in which hostname/ip/ssh are meaningful to return: READY itself,
# plus whatever comes after it in the happy path while the VM is still
# alive and reachable. Matches the implementation plan's "includes
# hostname/ip/ssh once READY" literally -- not shown during
# STARTING/WAITING_READY even though vm_hostname is already set on the
# row by then (see instance_manager.provision_instance).
_CONNECTABLE_STATES = {
    InstanceState.READY.value,
    InstanceState.CONNECTED.value,
    InstanceState.DISCONNECTED_GRACE.value,
}


@router.post(
    "/instances",
    response_model=CreateInstanceResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_instance(body: CreateInstanceRequest, request: Request) -> CreateInstanceResponse:
    app_state = request.app.state
    try:
        row = await instance_manager.create_instance(
            engine=app_state.db_engine,
            machines=app_state.machines,
            settings=app_state.settings,
            user_id=body.user,
            machine_type=body.machine_type,
            case_id=body.case_id,
            setup=body.setup,
            providers=app_state.providers,
        )
    except instance_manager.UnknownMachineTypeError:
        raise HTTPException(
            status_code=404, detail=f"unknown machine_type '{body.machine_type}'"
        ) from None
    except instance_manager.MachineDisabledError:
        raise HTTPException(
            status_code=400, detail=f"machine_type '{body.machine_type}' is disabled"
        ) from None
    except instance_manager.InvalidSetupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except instance_manager.QuotaExceededError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None

    # Kicked off independently of this request/response -- POST returns
    # 202 immediately (architecture doc §8) while provisioning continues.
    # Tracked in app.state.background_tasks so the task object isn't
    # garbage-collected mid-flight (asyncio only holds a weak reference
    # to a task with no other referent) and so main.py can await
    # in-flight tasks at shutdown.
    task = asyncio.create_task(
        instance_manager.provision_instance(
            engine=app_state.db_engine,
            machines=app_state.machines,
            providers=app_state.providers,
            settings=app_state.settings,
            instance_id=row["id"],
        )
    )
    app_state.background_tasks.add(task)
    task.add_done_callback(app_state.background_tasks.discard)

    return CreateInstanceResponse(
        instance_id=row["id"], machine_type=row["machine_type"], state=row["state"]
    )


def _instance_response(row: dict[str, Any], app_state: Any) -> InstanceResponse:
    machine = app_state.machines.get_machine(row["machine_type"])
    connectable = row["state"] in _CONNECTABLE_STATES
    ssh = None
    if connectable and machine.protocol == "ssh":
        ssh = SSHConnectionInfo(username=app_state.settings.vm_ssh_username, port=machine.probe_port)

    return InstanceResponse(
        instance_id=row["id"],
        machine_type=row["machine_type"],
        machine_name=machine.display_name,
        state=row["state"],
        hostname=row["vm_hostname"] if connectable else None,
        ip=row["vm_ip"] if connectable else None,
        ssh=ssh,
        expires_at=_as_utc(row["expires_at"]),
        failure_reason=row["failure_reason"],
        provider=row["provider"],
        protocol=machine.protocol,
        port=machine.probe_port if connectable else None,
        case_id=row["case_id"],
    )


def _as_utc(value: datetime) -> datetime:
    # Stored naive-UTC (db/models.py); tag it so the JSON carries "+00:00"
    # and a browser doesn't read it as local time.
    return value.replace(tzinfo=UTC)


@router.get("/instances", response_model=list[InstanceResponse])
async def list_instances(
    request: Request, user: str = Query(min_length=1)
) -> list[InstanceResponse]:
    app_state = request.app.state
    rows = await instance_manager.list_active_instances(engine=app_state.db_engine, user_id=user)
    return [_instance_response(row, app_state) for row in rows]


@router.get("/instances/{instance_id}", response_model=InstanceResponse)
async def get_instance(
    instance_id: str, request: Request, user: str | None = Query(default=None, min_length=1)
) -> InstanceResponse:
    """With `user`, a lease owned by someone else is a 404 (the web
    frontend always passes it); without, any lease -- the original M5
    behavior, kept for operators and the curl workflow.
    """
    app_state = request.app.state
    row = await instance_manager.get_instance(engine=app_state.db_engine, instance_id=instance_id)
    if row is None or (user is not None and row["user_id"] != user):
        raise HTTPException(status_code=404, detail="instance not found")
    return _instance_response(row, app_state)


@router.delete(
    "/instances/{instance_id}",
    response_model=InstanceResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def release_instance(
    instance_id: str, request: Request, user: str = Query(min_length=1)
) -> InstanceResponse:
    """Early release: 202 with the instance in DESTROYING (or already in
    teardown); the janitor removes the VM on its next pass.
    """
    app_state = request.app.state
    try:
        row = await instance_manager.release_instance(
            engine=app_state.db_engine, instance_id=instance_id, user_id=user
        )
    except instance_manager.InstanceNotFoundError:
        raise HTTPException(status_code=404, detail="instance not found") from None
    return _instance_response(row, app_state)


# --- runs inside a lease (M13) ---------------------------------------------


def _run_response(row: dict[str, Any]) -> RunResponse:
    return RunResponse(
        run_id=row["id"],
        instance_id=row["instance_id"],
        recipe=row["recipe"],
        state=row["state"],
        created_at=_as_utc(row["created_at"]),
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        result=row["result"],
        blobs=json.loads(row["blobs_json"]) if row["blobs_json"] else [],
    )


@router.post(
    "/instances/{instance_id}/runs",
    response_model=RunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_run(instance_id: str, body: CreateRunRequest, request: Request) -> RunResponse:
    """Start a recipe on the lease's machine: 202 with the run queued.
    400 for a machine without runs or a bad recipe name, 409 while the
    lease is not READY or another run is going, 502 when the host fails.
    """
    app_state = request.app.state
    try:
        row = await instance_manager.start_run(
            engine=app_state.db_engine,
            providers=app_state.providers,
            instance_id=instance_id,
            user_id=body.user,
            recipe=body.recipe,
        )
    except instance_manager.InstanceNotFoundError:
        raise HTTPException(status_code=404, detail="instance not found") from None
    except (instance_manager.RunsNotSupportedError, instance_manager.InvalidRecipeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except instance_manager.InstanceNotRunnableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=f"the machine host failed: {exc}") from None
    return _run_response(row)


@router.get("/instances/{instance_id}/runs", response_model=list[RunResponse])
async def list_runs(
    instance_id: str, request: Request, user: str | None = Query(default=None, min_length=1)
) -> list[RunResponse]:
    """The lease's runs, oldest first. `user` as for GET of the instance."""
    app_state = request.app.state
    try:
        rows = await instance_manager.list_runs(
            engine=app_state.db_engine,
            providers=app_state.providers,
            instance_id=instance_id,
            user_id=user,
        )
    except instance_manager.InstanceNotFoundError:
        raise HTTPException(status_code=404, detail="instance not found") from None
    return [_run_response(row) for row in rows]
