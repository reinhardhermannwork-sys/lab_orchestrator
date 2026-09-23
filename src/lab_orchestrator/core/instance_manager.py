"""Orchestration logic: create/get/list/destroy a LabInstance.

M5 scope: validate quota (1/user, 3/global), generate hostname via
naming.py, call Tux2LabClient.install, drive the state machine through
provisioning, poll for readiness, persist transitions to the DB.

Depends on: core.state_machine, core.config, adapters.tux2lab_client,
db.models / db.database, naming.py.

Not implemented yet.
"""
