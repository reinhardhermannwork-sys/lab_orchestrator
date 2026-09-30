"""JSON-auth token builder for Guacamole (architecture doc §13).

M11 scope — after the real-host integration (M10). Builds the encrypted JSON
payload and submits it to /api/tokens. The SSH private key is injected
directly into this payload from the secrets file, never passed through
the DB or an intermediate API response.

Not implemented yet.
"""
