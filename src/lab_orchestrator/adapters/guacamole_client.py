"""JSON-auth token builder for Guacamole (architecture doc §13).

M8 scope — deferred until M0-M7 are solid. Builds the encrypted JSON
payload and submits it to /api/tokens. The SSH private key is injected
directly into this payload from the secrets file, never passed through
the DB or an intermediate API response.

Not implemented yet.
"""
