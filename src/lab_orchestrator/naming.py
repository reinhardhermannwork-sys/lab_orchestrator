"""Hostname generation (architecture doc §5).

Format: lab-<machine-code>-<codename>-<instance-suffix>
Example: lab-m01-aurora-7k4m2

`instance-suffix` is a short random/Crockford-base32-style identifier,
intentionally opaque — no username in the VM's own hostname component.

BLOCKED on an explicit decision before implementation (architecture doc
§5 / §14.5, and implementation plan §M5): does the full FQDN legitimately
include the username via tux2lab's DNS zoning (e.g.
`lab-m01-aurora-7k4m2.hermann.internal`), or should the username be kept
out of the hostname entirely, contradicting that one example? This
wasn't reconciled in the design conversation. Get an explicit answer on
record before writing this module — don't bake in a silent assumption.

Not implemented yet.
"""
