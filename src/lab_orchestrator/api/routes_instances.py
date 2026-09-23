"""POST/GET /v1/instances (architecture doc §8).

M5 scope:
  POST /v1/instances   -> 202 Accepted, {instance_id, machine_type, state}
  GET  /v1/instances/{id} -> current state; hostname/ip/ssh once READY

Open questions to settle before/while implementing (architecture doc §14):
  - POST on an existing active instance: reject outright, or return the
    existing instance? (§14.6, unresolved)
  - GET /v1/instances (list) and DELETE /v1/instances/{id}: in scope for
    v1 or not? (§14.7, unresolved)

Not implemented yet.
"""
