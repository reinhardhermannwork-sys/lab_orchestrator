"""GET /v1/machines: the machine types a user can request (M9).

Read straight from the registry loaded from config/machines.yaml at
startup; disabled machines are left out, matching what POST
/v1/instances accepts.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from lab_orchestrator.api.schemas import MachineResponse

router = APIRouter()


@router.get("/machines", response_model=list[MachineResponse])
async def list_machines(request: Request) -> list[MachineResponse]:
    return [
        MachineResponse(machine_type=m.machine_type, display_name=m.display_name)
        for m in request.app.state.machines.list_enabled()
    ]
