"""POST/GET /v1/instances (architecture doc §8).

Open API-design questions settled here, since neither was explicitly
resolved in the architecture doc (§14.6, §14.7):

- **`POST` while the user already has an active instance**: rejected
  outright (`409`, via `UserQuotaExceededError`), not returned as if it
  were the existing instance. The DB-level quota constraint (M2) already
  makes this the natural behavior — returning the existing instance
  instead would need an extra lookup this endpoint doesn't otherwise
  need, for a v1 prototype with no stated need for that convenience.
- **List/delete endpoints**: out of scope for this milestone. Not built
  here, not silently assumed unnecessary — just not part of M5's own
  done-when (the POST -> poll -> ssh curl workflow), left for whoever
  picks up that open question next.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request, status

from lab_orchestrator.api.schemas import (
    CreateInstanceRequest,
    CreateInstanceResponse,
    InstanceResponse,
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
        )
    except instance_manager.UnknownMachineTypeError:
        raise HTTPException(
            status_code=404, detail=f"unknown machine_type '{body.machine_type}'"
        ) from None
    except instance_manager.MachineDisabledError:
        raise HTTPException(
            status_code=400, detail=f"machine_type '{body.machine_type}' is disabled"
        ) from None
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
            tux2lab=app_state.tux2lab,
            settings=app_state.settings,
            instance_id=row["id"],
        )
    )
    app_state.background_tasks.add(task)
    task.add_done_callback(app_state.background_tasks.discard)

    return CreateInstanceResponse(
        instance_id=row["id"], machine_type=row["machine_type"], state=row["state"]
    )


@router.get("/instances/{instance_id}", response_model=InstanceResponse)
async def get_instance(instance_id: str, request: Request) -> InstanceResponse:
    app_state = request.app.state
    row = await instance_manager.get_instance(engine=app_state.db_engine, instance_id=instance_id)
    if row is None:
        raise HTTPException(status_code=404, detail="instance not found")

    machine = app_state.machines.get_machine(row["machine_type"])
    ssh = None
    if row["state"] in _CONNECTABLE_STATES:
        ssh = SSHConnectionInfo(username=app_state.settings.vm_ssh_username, port=22)

    return InstanceResponse(
        instance_id=row["id"],
        machine_type=row["machine_type"],
        machine_name=machine.display_name,
        state=row["state"],
        hostname=row["vm_hostname"] if row["state"] in _CONNECTABLE_STATES else None,
        ip=row["vm_ip"] if row["state"] in _CONNECTABLE_STATES else None,
        ssh=ssh,
    )
