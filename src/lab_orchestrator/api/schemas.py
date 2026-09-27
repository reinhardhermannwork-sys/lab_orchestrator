"""Pydantic request/response models for the /v1/instances API
(architecture doc §8).
"""

from __future__ import annotations

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
    """GET /v1/instances/{id}. `hostname`/`ip`/`ssh` are `None` until the
    instance reaches READY — matching the implementation plan's "includes
    hostname/ip/ssh once READY" wording literally, not just once-set.
    """

    instance_id: str
    machine_type: str
    machine_name: str
    state: str
    hostname: str | None = None
    ip: str | None = None
    ssh: SSHConnectionInfo | None = None
