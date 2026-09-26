"""Environment variables, paths, and loaded machine definitions.

Two independent pieces live here:

  - `Settings`: env-var-driven configuration (never hardcode a path
    elsewhere in the codebase — add a field here instead, per the
    implementation plan's ground rules). Only fields actually consumed
    so far are defined; SSH connection details for the host-side
    tux2lab wrapper are deliberately left out until M4 settles
    asyncssh's exact connection shape, rather than guessed at now.

  - `load_machine_definitions` / `MachineRegistry`: the
    config/machines.yaml loader (architecture doc §3), validated on
    load and queried in-process via `registry.get_machine(machine_type)`.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

# --- Settings ---------------------------------------------------------


class Settings(BaseSettings):
    """Env-var-driven settings, overridable via `LAB_ORCH_*` env vars or
    a `.env` file.
    """

    model_config = SettingsConfigDict(env_prefix="LAB_ORCH_", env_file=".env", extra="ignore")

    machines_config_path: Path = Path("config/machines.yaml")

    # Consumed starting M2 (data layer).
    db_path: Path = Path("orchestrator.db")

    # Consumed starting M8 (Guacamole adapter) — where the controlled copy
    # of the lab-wide VM SSH key lives (architecture doc §12).
    secrets_dir: Path = Path("/opt/lab-orchestrator/secrets")

    # SSH connection to the host-side tux2lab CLI wrapper (architecture
    # doc §11). Optional at the Settings level — the fake client and most
    # tests never need these — but SSHTux2LabClient.from_settings() (M4)
    # requires host/username/key_path to actually be set before it'll
    # construct a real client.
    tux2lab_ssh_host: str | None = None
    tux2lab_ssh_port: int = 22
    tux2lab_ssh_username: str | None = None
    tux2lab_ssh_key_path: Path | None = None  # the wrapper's own key — distinct from
    # the lab-wide VM key copied into secrets_dir (architecture doc §12), which
    # authenticates the orchestrator *to the host*, not to any VM.
    tux2lab_ssh_known_hosts_path: Path | None = None  # None => asyncssh's own
    # default host-key handling, NOT "disable checking" — see
    # adapters/tux2lab_client.py's SSHTux2LabClient.from_settings() for why
    # that distinction matters and how it's preserved.
    tux2lab_ssh_command_timeout: float = 30.0


@lru_cache
def get_settings() -> Settings:
    return Settings()


# --- Machine definitions -----------------------------------------------


class MachineDefinition(BaseModel):
    """One entry from config/machines.yaml, keyed by its machine_type id."""

    machine_type: str  # the yaml key itself, injected while loading
    display_name: str
    code: str = Field(pattern=r"^[a-z0-9]+$")
    codename: str = Field(pattern=r"^[a-z0-9]+$")
    tux2lab_image: str = Field(min_length=1)
    # SSH only for v1 (architecture doc §15: "SSH first; RDP later"). The
    # field exists for future protocols, but any other value today is a
    # config error, not something the rest of the system would handle.
    protocol: Literal["ssh"]
    enabled: bool = True


class MachineConfigError(ValueError):
    """config/machines.yaml is missing, malformed, or invalid.

    A ValueError subclass so it reads clearly in a startup traceback and
    is catchable with ordinary `except ValueError` — meant to fail fast
    at startup, not surface as an opaque KeyError/ValidationError deep
    inside request handling.
    """


class MachineRegistry:
    """In-process lookup of loaded, validated machine definitions."""

    def __init__(self, machines: dict[str, MachineDefinition]) -> None:
        self._machines = machines

    def get_machine(self, machine_type: str) -> MachineDefinition:
        try:
            return self._machines[machine_type]
        except KeyError:
            raise KeyError(f"unknown machine_type '{machine_type}'") from None

    def list_enabled(self) -> list[MachineDefinition]:
        return [m for m in self._machines.values() if m.enabled]

    def items(self):
        """(machine_type, MachineDefinition) pairs — used by db/init_db.py
        to upsert config/machines.yaml into the machine_definitions table.
        """
        return self._machines.items()

    def __contains__(self, machine_type: str) -> bool:
        return machine_type in self._machines

    def __len__(self) -> int:
        return len(self._machines)


def load_machine_definitions(path: Path | str) -> MachineRegistry:
    """Load, validate, and wrap config/machines.yaml.

    Validates: the file exists and parses as YAML with a top-level
    `machines:` map; every entry has all required fields; `code` and
    `codename` are each unique across *all* entries, including disabled
    ones (so re-enabling one later can't silently collide).

    Raises `MachineConfigError` with a specific, actionable message on
    any problem — fails fast, doesn't guess.
    """
    path = Path(path)
    if not path.is_file():
        raise MachineConfigError(f"machines config not found: {path}")

    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise MachineConfigError(f"machines config is not valid YAML ({path}): {exc}") from exc

    if not isinstance(raw, dict) or "machines" not in raw:
        raise MachineConfigError(f"machines config ({path}) must have a top-level `machines:` map")

    machines_raw = raw["machines"]
    if not isinstance(machines_raw, dict) or not machines_raw:
        raise MachineConfigError(f"machines config ({path}) defines no machines")

    machines: dict[str, MachineDefinition] = {}
    seen_codes: dict[str, str] = {}
    seen_codenames: dict[str, str] = {}

    for machine_type, entry in machines_raw.items():
        if not isinstance(entry, dict):
            raise MachineConfigError(
                f"machine '{machine_type}' in {path} must be a mapping, "
                f"got {type(entry).__name__}"
            )
        try:
            definition = MachineDefinition(machine_type=machine_type, **entry)
        except ValidationError as exc:
            raise MachineConfigError(f"machine '{machine_type}' in {path} is invalid: {exc}") from exc

        if definition.code in seen_codes:
            raise MachineConfigError(
                f"duplicate machine code '{definition.code}' in {path} "
                f"(used by both '{seen_codes[definition.code]}' and '{machine_type}')"
            )
        if definition.codename in seen_codenames:
            raise MachineConfigError(
                f"duplicate machine codename '{definition.codename}' in {path} "
                f"(used by both '{seen_codenames[definition.codename]}' and '{machine_type}')"
            )
        seen_codes[definition.code] = machine_type
        seen_codenames[definition.codename] = machine_type
        machines[machine_type] = definition

    return MachineRegistry(machines)
