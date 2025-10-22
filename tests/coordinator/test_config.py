from pathlib import Path

import pytest

from coordinator.main import assign_ports, discover_profiles
from coordinator.types import AuthProfile, ChildPorts


def test_discover_profiles_returns_sorted_profiles(tmp_path: Path) -> None:
    (tmp_path / "beta.json").write_text("{}", encoding="utf-8")
    (tmp_path / "alpha.json").write_text("{}", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("not a profile", encoding="utf-8")

    profiles = discover_profiles(tmp_path)

    assert [p.name for p in profiles] == ["alpha", "beta"]
    assert all(isinstance(p, AuthProfile) for p in profiles)


def test_discover_profiles_missing_directory(tmp_path: Path) -> None:
    missing_dir = tmp_path / "missing"
    with pytest.raises(FileNotFoundError):
        discover_profiles(missing_dir)


def test_assign_ports_uses_increment() -> None:
    ports = assign_ports(
        count=3, base_api=1000, base_stream=2000, base_camoufox=3000, step=2
    )

    expected = [
        ChildPorts(api_port=1000, stream_port=2000, camoufox_port=3000),
        ChildPorts(api_port=1002, stream_port=2002, camoufox_port=3002),
        ChildPorts(api_port=1004, stream_port=2004, camoufox_port=3004),
    ]
    assert ports == expected


def test_assign_ports_rejects_invalid_arguments() -> None:
    with pytest.raises(ValueError):
        assign_ports(count=-1, base_api=1, base_stream=2, base_camoufox=3)
    with pytest.raises(ValueError):
        assign_ports(count=1, base_api=1, base_stream=2, base_camoufox=3, step=0)
