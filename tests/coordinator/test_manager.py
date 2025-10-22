from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import cast

import pytest

from coordinator.manager import ChildRegistry
from coordinator.types import AuthProfile, ChildPorts, ChildProcess


class DummyProcess:
    def __init__(self, running: bool = True) -> None:
        self._running = running
        self.returncode: int | None = None if running else 0

    def poll(self) -> int | None:
        return None if self._running else 0

    def terminate(self) -> None:  # pragma: no cover - interface compatibility
        self._running = False
        self.returncode = 0


def make_child(tmp_path: Path, name: str) -> ChildProcess:
    profile_path = tmp_path / f"{name}.json"
    profile_path.write_text("{}", encoding="utf-8")
    profile = AuthProfile(name=name, path=profile_path)
    ports = ChildPorts(api_port=8000, stream_port=8100, camoufox_port=8200)
    process = cast(subprocess.Popen[str], DummyProcess())
    return ChildProcess(profile=profile, ports=ports, process=process)


def test_next_child_round_robin(tmp_path: Path) -> None:
    child_a = make_child(tmp_path, "a")
    child_b = make_child(tmp_path, "b")
    registry = ChildRegistry([child_a, child_b])
    registry.mark_ready(child_a)
    registry.mark_ready(child_b)

    assert registry.next_child() is child_a
    assert registry.next_child() is child_b
    assert registry.next_child() is child_a


def test_mark_unhealthy_removes_from_ready(tmp_path: Path) -> None:
    child_a = make_child(tmp_path, "a")
    child_b = make_child(tmp_path, "b")
    registry = ChildRegistry([child_a, child_b])
    registry.mark_ready(child_a)
    registry.mark_ready(child_b)

    registry.mark_unhealthy(child_a, "manual demotion")
    assert registry.next_child() is child_b
    assert registry.next_child() is child_b  # child_b rotates as only healthy child

    registry.mark_unhealthy(child_b, "also demoted")
    assert registry.next_child() is None


@pytest.mark.anyio("asyncio")
async def test_monitor_promotes_recovered_child(tmp_path: Path) -> None:
    child = make_child(tmp_path, "solo")
    recovered = asyncio.Event()

    async def fake_health(check_child: ChildProcess, timeout: float) -> bool:
        await asyncio.sleep(0)
        if recovered.is_set():
            check_child.ready = True
            return True
        return False

    registry = ChildRegistry(
        [child],
        health_check=fake_health,
        poll_interval=0.05,
        recovery_timeout=0.01,
    )
    await registry.start_monitoring()
    registry.mark_unhealthy(child, "start unhealthy")

    await asyncio.sleep(0.1)
    assert registry.next_child() is None

    recovered.set()
    await asyncio.sleep(0.1)
    assert registry.next_child() is child

    await registry.shutdown()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
