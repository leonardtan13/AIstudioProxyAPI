from __future__ import annotations

from pathlib import Path
from typing import List

from coordinator.main import PROFILE_POOL_SIZE
from coordinator.manager import SlotManager
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


def make_profile(index: int) -> AuthProfile:
    return AuthProfile(
        name=f"profile-{index}",
        path=Path(f"/profiles/profile-{index}.json"),
    )


def make_slot(offset: int) -> ProfileSlot:
    ports = ChildPorts(
        api_port=3100 + offset,
        stream_port=3200 + offset,
        camoufox_port=9222 + offset,
    )
    return ProfileSlot(ports=ports)


def test_slot_bootstrap_uses_fixed_pool_size() -> None:
    extra_profiles = PROFILE_POOL_SIZE + 2
    profiles = [make_profile(i) for i in range(extra_profiles)]
    slots = [make_slot(i) for i in range(PROFILE_POOL_SIZE)]
    queue = ProfileQueue.from_iterable(profiles[PROFILE_POOL_SIZE:])

    launch_calls: List[str] = []

    def fake_launch(profile: AuthProfile, ports, env, *, headless, log_dir) -> ChildProcess:
        launch_calls.append(profile.name)
        return ChildProcess(
            profile=profile,
            ports=ports,
            process=DummyProcess(),
        )

    manager = SlotManager(
        slots,
        profile_queue=queue,
        headless=True,
        log_dir=Path("/logs"),
        env={},
        launch_fn=fake_launch,
    )

    children = manager.bootstrap(profiles[:PROFILE_POOL_SIZE])

    assert len(children) == PROFILE_POOL_SIZE
    assert queue.snapshot() == profiles[PROFILE_POOL_SIZE:]
    assert launch_calls == [
        profile.name for profile in profiles[:PROFILE_POOL_SIZE]
    ]
    active = manager.live_children()
    assert len(active) == PROFILE_POOL_SIZE
    assert {child.profile.name for child in active} == {
        profile.name for profile in profiles[:PROFILE_POOL_SIZE]
    }
