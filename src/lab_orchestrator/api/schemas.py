"""Pydantic request/response models for the /v1 API (architecture doc §8).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class CreateInstanceRequest(BaseModel):
    """POST /v1/instances body. `user` is a plain string by design
    (architecture doc §4) — swapping it for identity extracted from an
    authenticated request later is a call-site change, not a schema one.
    """

    machine_type: str
    user: str = Field(min_length=1)


class CreateInstanceResponse(BaseModel):
    """202 Accepted — returned immediately, before provisioning runs."""

    instance_id: str
    machine_type: str
    state: str


class SSHConnectionInfo(BaseModel):
    username: str
    port: int


class InstanceResponse(BaseModel):
    """One instance, as returned by GET/DELETE /v1/instances/{id} and the
    per-user list. `hostname`/`ip`/`ssh` are `None` until the instance
    reaches READY — matching the implementation plan's "includes
    hostname/ip/ssh once READY" wording literally, not just once-set.
    `expires_at` (UTC) and `failure_reason` were added for the web
    frontend's status screen (M9).
    """

    instance_id: str
    machine_type: str
    machine_name: str
    state: str
    hostname: str | None = None
    ip: str | None = None
    ssh: SSHConnectionInfo | None = None
    expires_at: datetime
    failure_reason: str | None = None


class MachineResponse(BaseModel):
    """GET /v1/machines: one enabled machine type a user can request."""

    machine_type: str
    display_name: str
