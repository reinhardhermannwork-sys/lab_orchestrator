"""Tests for the machines.yaml loader/validator (M1 done-when criteria)."""

from pathlib import Path

import pytest

from lab_orchestrator.core.config import MachineConfigError, load_machine_definitions

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_CONFIG = REPO_ROOT / "config" / "machines.yaml"


def write_yaml(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "machines.yaml"
    p.write_text(text)
    return p


# --- the real config file, as a regression check --------------------------


def test_real_machines_yaml_is_valid():
    registry = load_machine_definitions(REAL_CONFIG)
    assert len(registry) == 3
    m1 = registry.get_machine("machine_1")
    assert m1.display_name == "Machine 1"
    assert m1.code == "m01"
    assert m1.codename == "aurora"
    assert m1.tux2lab_image == "image_1_software_1"
    assert m1.protocol == "ssh"
    assert m1.enabled is True


def test_get_machine_unknown_type_raises_keyerror():
    registry = load_machine_definitions(REAL_CONFIG)
    with pytest.raises(KeyError):
        registry.get_machine("machine_99")


# --- enabled handling -------------------------------------------------


def test_list_enabled_excludes_disabled_but_still_queryable(tmp_path):
    path = write_yaml(
        tmp_path,
        """
machines:
  machine_1:
    display_name: "Machine 1"
    code: "m01"
    codename: "aurora"
    tux2lab_image: "image_1"
    protocol: "ssh"
    enabled: true
  machine_2:
    display_name: "Machine 2"
    code: "m02"
    codename: "forge"
    tux2lab_image: "image_2"
    protocol: "ssh"
    enabled: false
""",
    )
    registry = load_machine_definitions(path)
    assert len(registry) == 2
    assert "machine_2" in registry
    assert registry.get_machine("machine_2").enabled is False
    assert [m.machine_type for m in registry.list_enabled()] == ["machine_1"]


def test_enabled_defaults_true_when_omitted(tmp_path):
    path = write_yaml(
        tmp_path,
        """
machines:
  machine_1:
    display_name: "Machine 1"
    code: "m01"
    codename: "aurora"
    tux2lab_image: "image_1"
    protocol: "ssh"
""",
    )
    registry = load_machine_definitions(path)
    assert registry.get_machine("machine_1").enabled is True


# --- failure modes: each must fail fast with a clear, specific error -----


def test_missing_file_raises():
    with pytest.raises(MachineConfigError, match="not found"):
        load_machine_definitions("/no/such/file.yaml")


def test_malformed_yaml_raises(tmp_path):
    path = write_yaml(tmp_path, "machines: [this, is, not, a, map")
    with pytest.raises(MachineConfigError, match="not valid YAML"):
        load_machine_definitions(path)


def test_missing_machines_key_raises(tmp_path):
    path = write_yaml(tmp_path, "not_machines: {}")
    with pytest.raises(MachineConfigError, match="top-level `machines:` map"):
        load_machine_definitions(path)


def test_empty_machines_map_raises(tmp_path):
    path = write_yaml(tmp_path, "machines: {}")
    with pytest.raises(MachineConfigError, match="defines no machines"):
        load_machine_definitions(path)


def test_missing_required_field_raises(tmp_path):
    path = write_yaml(
        tmp_path,
        """
machines:
  machine_1:
    display_name: "Machine 1"
    code: "m01"
    codename: "aurora"
    protocol: "ssh"
""",  # tux2lab_image missing
    )
    with pytest.raises(MachineConfigError, match="machine_1"):
        load_machine_definitions(path)


def test_unsupported_protocol_raises(tmp_path):
    path = write_yaml(
        tmp_path,
        """
machines:
  machine_1:
    display_name: "Machine 1"
    code: "m01"
    codename: "aurora"
    tux2lab_image: "image_1"
    protocol: "rdp"
""",
    )
    with pytest.raises(MachineConfigError, match="machine_1"):
        load_machine_definitions(path)


def test_invalid_code_charset_raises(tmp_path):
    path = write_yaml(
        tmp_path,
        """
machines:
  machine_1:
    display_name: "Machine 1"
    code: "M01!"
    codename: "aurora"
    tux2lab_image: "image_1"
    protocol: "ssh"
""",
    )
    with pytest.raises(MachineConfigError, match="machine_1"):
        load_machine_definitions(path)


def test_duplicate_code_raises(tmp_path):
    path = write_yaml(
        tmp_path,
        """
machines:
  machine_1:
    display_name: "Machine 1"
    code: "m01"
    codename: "aurora"
    tux2lab_image: "image_1"
    protocol: "ssh"
  machine_2:
    display_name: "Machine 2"
    code: "m01"
    codename: "forge"
    tux2lab_image: "image_2"
    protocol: "ssh"
""",
    )
    with pytest.raises(MachineConfigError, match="duplicate machine code"):
        load_machine_definitions(path)


def test_duplicate_codename_raises(tmp_path):
    path = write_yaml(
        tmp_path,
        """
machines:
  machine_1:
    display_name: "Machine 1"
    code: "m01"
    codename: "aurora"
    tux2lab_image: "image_1"
    protocol: "ssh"
  machine_2:
    display_name: "Machine 2"
    code: "m02"
    codename: "aurora"
    tux2lab_image: "image_2"
    protocol: "ssh"
""",
    )
    with pytest.raises(MachineConfigError, match="duplicate machine codename"):
        load_machine_definitions(path)
