"""JSON-auth token builder for Guacamole (architecture doc §13).

M10 scope — after the real-host integration (M9). Builds the encrypted JSON
payload and submits it to /api/tokens. The SSH private key is injected
directly into this payload from the secrets file, never passed through
the DB or an intermediate API response.

Not implemented yet.
"""
