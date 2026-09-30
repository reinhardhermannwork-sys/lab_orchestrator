"""Tests for naming.py (architecture doc §5; username-free decision)."""

from __future__ import annotations

import re

from lab_orchestrator import naming
from lab_orchestrator.adapters.tux2lab_client import _HOSTNAME_RE
from lab_orchestrator.core.config import MachineDefinition


def _machine() -> MachineDefinition:
    return MachineDefinition(
        machine_type="machine_1",
        display_name="Machine 1",
        code="m01",
        codename="aurora",
        tux2lab_image="image_1_software_1",
        protocol="ssh",
    )


def test_hostname_matches_documented_format():
    assert re.fullmatch(r"lab-m01-aurora-[0-9a-hjkmnp-tv-z]{5}", naming.generate_hostname(_machine()))


def test_hostname_passes_the_adapter_validation():
    for _ in range(200):
        assert _HOSTNAME_RE.match(naming.generate_hostname(_machine()))


def test_hostname_has_no_dns_suffix():
    assert "." not in naming.generate_hostname(_machine())


def test_hostnames_do_not_collide_in_practice():
    names = {naming.generate_hostname(_machine()) for _ in range(2000)}
    assert len(names) == 2000


def test_generator_takes_no_user_input():
    # Structural guarantee for "no username in the hostname": the only
    # input is the machine definition.
    import inspect

    assert list(inspect.signature(naming.generate_hostname).parameters) == ["machine"]
