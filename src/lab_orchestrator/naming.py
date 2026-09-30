"""Hostname generation (architecture doc §5).

Format (used as the tux2lab VM identifier, the `-H` argument to every
`Tux2LabClient` call, and `instances.vm_hostname`):

    lab-<machine-code>-<codename>-<instance-suffix>
    e.g. lab-m01-aurora-7k4m2

`instance-suffix` is 5 random characters from the Crockford base32
alphabet (lowercased, so it satisfies the RFC 1123 label check in
adapters/tux2lab_client.py) -- 32^5 ~= 33.5M values, plenty for at most
3 concurrent instances plus recycling.

**Decision (recorded): no username anywhere in the name.** The hostname is
opaque by design; the `.{username}.internal` suffix that appeared in one
example of the source discussion is *not* something this module builds or
that the orchestrator persists. If tux2lab's own DNS zoning appends a
suffix, that is tux2lab's concern -- the orchestrator only ever generates
and stores this short label. Nothing here takes a user identifier as input,
so it structurally cannot leak one.
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lab_orchestrator.core.config import MachineDefinition

# Crockford base32 (no i, l, o, u -- avoids look-alike characters and
# accidental words), lowercased.
_SUFFIX_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
_SUFFIX_LENGTH = 5


def generate_hostname(machine: MachineDefinition) -> str:
    """Generate an opaque VM hostname for a new instance of `machine`."""
    suffix = "".join(secrets.choice(_SUFFIX_ALPHABET) for _ in range(_SUFFIX_LENGTH))
    return f"lab-{machine.code}-{machine.codename}-{suffix}"
