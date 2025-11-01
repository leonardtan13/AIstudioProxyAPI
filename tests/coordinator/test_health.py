from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from coordinator.manager import ChildRegistry, SlotManager
from coordinator.types import (
    AuthProfile,
    ChildPorts,
    ChildProcess,
    ProfileQueue,
    ProfileSlot,
)


class DummyProcess:
    def __init__(self) -> None:
        self._running = True
        self.pid = id(self)
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return None if self._running else self.returncode

    def terminate(self) -> None:  # pragma: no cover - compatibility
        self._running = False
        self.returncode = 0

    def kill(self) -> None:  # pragma: no cover - compatibility
        self._running = False
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:  # pragma: no cover
        self._running = False
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


def make_profile(tmp_path: Path, index: int) -> AuthProfile:
    path = tmp_path / f"profile-{index}.json"
    path.write_text("{}", encoding="utf-8")
    return AuthProfile(name=f"profile-{index}", path=path)


def make_slot(offset: int) -> ProfileSlot:
    ports = ChildPorts(
        api_port=3100 + offset,
        stream_port=3200 + offset,
        camoufox_port=9222 + offset,
    )
    return ProfileSlot(ports=ports)


def fake_launch(profile: AuthProfile, ports: ChildPorts, env, *, headless, log_dir) -> ChildProcess:
    return ChildProcess(
        profile=profile,
        ports=ports,
        process=DummyProcess(),
    )


@pytest.mark.anyio("asyncio")
async def test_timeout_triggers_recycle(tmp_path: Path) -> None:
    profiles = [make_profile(tmp_path, idx) for idx in range(2)]
    slots = [make_slot(0)]
    queue = ProfileQueue.from_iterable(profiles[1:])

    manager = SlotManager(
        slots,
        profile_queue=queue,
        headless=True,
        log_dir=tmp_path,
        env={},
        launch_fn=fake_launch,
    )
    children = manager.bootstrap([profiles[0]])

    attempts = 0

    async def flaky_health(child: ChildProcess, timeout: float) -> bool:
        nonlocal attempts
        await asyncio.sleep(0)
        attempts += 1
        # First poll fails, subsequent polls succeed to simulate one timeout.
        return attempts > 1

    registry = ChildRegistry(
        children,
        slot_manager=manager,
        health_check=flaky_health,
        poll_interval=0.01,
        recovery_timeout=0.01,
    )
    await registry.start_monitoring()

    try:
        # Initial demotion seeds the queued profile into the slot.
        registry.mark_unhealthy(children[0], "initial failure")
        await asyncio.sleep(0.05)

        active_profile = manager.slots()[0].profile
        assert active_profile is not None
        assert active_profile.name == profiles[0].name
        # The previously promoted profile is now queued for reuse.
        assert queue.snapshot() == [profiles[1]]
        assert registry.unhealthy_names() == []
    finally:
        await registry.shutdown()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
