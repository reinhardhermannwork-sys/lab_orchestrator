"""Environment variables, paths, and loaded machine definitions.

M1 scope (see implementation plan §M1 / architecture doc §3):
  - Settings model (env vars only — no hardcoded paths/secrets):
      DB path, tux2lab SSH connection details, secrets directory path.
  - Loader for config/machines.yaml, validated on load:
      unique `code` / `codename` across all entries, required fields
      present, `enabled` respected. Fails fast with a clear error on
      invalid config.
  - `get_machine(machine_type: str) -> MachineDefinition` lookup.

Not implemented yet.
"""
