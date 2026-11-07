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
    # The Fab Suite case the lease serves.
    case_id: str | None = Field(default=None, min_length=1, max_length=128)


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
    # M13: which provider runs the machine, how one reaches it (`ssh` is
    # only filled for protocol ssh; `port` is the machine's probe port,
    # if it has one), and the case it serves.
    provider: str
    protocol: str
    port: int | None = None
    case_id: str | None = None
    # M13 SP5: the owner, so a case's lease list (Fab Suite) can tell the
    # viewer's own lease from a colleague's.
    user: str


class MachineResponse(BaseModel):
    """GET /v1/machines: one enabled machine type a user can request."""

    machine_type: str
    display_name: str
