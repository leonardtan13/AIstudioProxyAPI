from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import cast

import pytest

from coordinator.main import PROFILE_POOL_SIZE
from coordinator.manager import ChildRegistry, SlotManager
from coordinator.types import (
    AuthProfile,
    ChildPorts,
    ChildProcess,
    ProfileQueue,
    ProfileSlot,
)


class DummyProcess:
    def __init__(self, running: bool = True) -> None:
        self._running = running
        self.returncode: int | None = None if running else 0
        self.pid = id(self)

    def poll(self) -> int | None:
        return None if self._running else 0

    def terminate(self) -> None:  # pragma: no cover - interface compatibility
        self._running = False
        self.returncode = 0

    def kill(self) -> None:  # pragma: no cover - interface compatibility
        self._running = False
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:  # pragma: no cover
        self._running = False
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


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


def test_slot_recycle_promotes_queued_profile(tmp_path: Path) -> None:
    total_profiles = PROFILE_POOL_SIZE + 3
    profiles: list[AuthProfile] = []
    for index in range(total_profiles):
        profile_path = tmp_path / f"profile-{index}.json"
        profile_path.write_text("{}", encoding="utf-8")
        profiles.append(AuthProfile(name=f"profile-{index}", path=profile_path))

    slots = [
        ProfileSlot(ports=ChildPorts(3100 + i, 3200 + i, 9222 + i))
        for i in range(PROFILE_POOL_SIZE)
    ]
    queue = ProfileQueue.from_iterable(profiles[PROFILE_POOL_SIZE:])

    launched: list[str] = []

    def fake_launch(profile: AuthProfile, ports: ChildPorts, env, *, headless, log_dir) -> ChildProcess:
        launched.append(profile.name)
        return ChildProcess(profile=profile, ports=ports, process=cast(subprocess.Popen[str], DummyProcess()))

    manager = SlotManager(
        slots,
        profile_queue=queue,
        headless=True,
        log_dir=tmp_path,
        env={},
        launch_fn=fake_launch,
    )

    children = manager.bootstrap(profiles[:PROFILE_POOL_SIZE])
    registry = ChildRegistry(children, slot_manager=manager)

    original_child = children[0]
    registry.evict_child(original_child, "forced recycle for test")

    # Slot should now host the queued profile while the evicted profile is staged for reuse.
    active_names = {slot.process.profile.name for slot in manager.slots() if slot.process}
    replacement_index = PROFILE_POOL_SIZE
    assert profiles[replacement_index].name in active_names
    expected_queue = [profile for profile in profiles[replacement_index + 1 :]]
    expected_queue.append(profiles[0])
    assert queue.snapshot() == expected_queue
    assert profiles[replacement_index].name in registry.unhealthy_names()
    assert profiles[0].name not in {
        child.profile.name for child in registry.all_children()
    }


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
