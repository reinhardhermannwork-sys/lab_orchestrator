"""Parsers for tux2lab's text output (M10), against the samples in
tests/fixtures/tux2lab/ (see its README for where they come from)."""

from __future__ import annotations

from pathlib import Path

import pytest

from lab_orchestrator.adapters.tux2lab_client import (
    Tux2LabCommandError,
    _error_lines,
    _parse_vm_info_ipv4,
    _parse_vm_list,
    _short_label,
)

FIXTURES = Path(__file__).parent / "fixtures" / "tux2lab"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def test_vm_list_rows_with_short_labels_and_states():
    assert _parse_vm_list(fixture("vm_list.txt")) == [
        ("lab-m01-aurora-7k4m2", "running", "healthy"),
        ("lab-m02-forge-q3x9d", "running", "SSH-Not-Ready"),
        ("golden-ref-alma10", "shut off", "[ N/A ]"),
    ]


def test_vm_list_with_no_vms_is_empty():
    header = "VM-Name  VM-State OS-State OS-Distro\n--------------------------------------\n"
    assert _parse_vm_list(header) == []


def test_vm_list_rejects_an_unexpected_line():
    with pytest.raises(Tux2LabCommandError):
        _parse_vm_list("VM-Name VM-State OS-State OS-Distro\n----\nonly-one-column\n")


def test_vm_info_healthy_gives_the_ipv4_without_prefix():
    # The tree also has "Gateway: 10.28.28.1" and IPv6 lines; neither counts.
    assert _parse_vm_info_ipv4(fixture("vm_info_healthy.txt")) == "10.28.28.42"


@pytest.mark.parametrize(
    "name", ["vm_info_ssh_not_accessible.txt", "vm_info_shut_off.txt", "vm_info_unknown.txt"]
)
def test_vm_info_without_addresses_gives_none(name):
    assert _parse_vm_info_ipv4(fixture(name)) is None


def test_error_lines_come_from_stdout_without_colors():
    assert _error_lines(fixture("vm_install_exists.txt")) == (
        '[ERROR] VM "lab-m01-aurora-7k4m2.hermann.internal" exists already.'
    )


def test_short_label_drops_the_lab_domain():
    assert _short_label("lab-m01-aurora-7k4m2.hermann.internal") == "lab-m01-aurora-7k4m2"
    assert _short_label("bare-name") == "bare-name"
