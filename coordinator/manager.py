from __future__ import annotations

import asyncio
import logging
import subprocess
import threading
from collections import deque
from pathlib import Path
from typing import Awaitable, Callable, Iterable, List, Mapping, Sequence

from .health import wait_for_ready
from .launcher import launch_child
from .types import AuthProfile, ChildProcess, ProfileQueue, ProfileSlot

LOGGER = logging.getLogger("Coordinator.ChildRegistry")
SLOT_LOGGER = logging.getLogger("Coordinator.SlotManager")

LaunchChildFunc = Callable[..., ChildProcess]
_SHUTDOWN_TIMEOUT = 10.0


class SlotManager:
    """Manage fixed-number profile slots and recycle profiles on eviction."""

    def __init__(
        self,
        slots: Sequence[ProfileSlot],
        *,
        profile_queue: ProfileQueue,
        headless: bool,
        log_dir: Path,
        env: Mapping[str, str] | None = None,
        launch_fn: LaunchChildFunc = launch_child,
    ) -> None:
        self._slots = list(slots)
        self._queue = profile_queue
        self._headless = headless
        self._log_dir = log_dir
        self._env = dict(env or {})
        self._launch_child = launch_fn
        self._lock = threading.Lock()

    def bootstrap(self, profiles: Sequence[AuthProfile]) -> list[ChildProcess]:
        """Launch the initial set of profiles across the configured slots."""

        launched_slots: list[ProfileSlot] = []
        children: list[ChildProcess] = []
        with self._lock:
            if len(profiles) > len(self._slots):
                raise ValueError(
                    "Cannot bootstrap more active profiles than available slots."
                )
            for slot, profile in zip(self._slots, profiles):
                try:
                    child = self._launch_into_slot(slot, profile)
                except Exception:
                    for launched in launched_slots:
                        self._terminate_slot(launched, "bootstrap failure")
                    raise
                launched_slots.append(slot)
                children.append(child)
        return children

    def live_children(self) -> list[ChildProcess]:
        with self._lock:
            return [
                slot.process for slot in self._slots if slot.process is not None
            ]

    def slots(self) -> Sequence[ProfileSlot]:
        return self._slots

    def clear_queue(self) -> None:
        with self._lock:
            self._queue.clear()

    def slot_for_child(self, child: ChildProcess) -> ProfileSlot | None:
        with self._lock:
            for slot in self._slots:
                if slot.process is child:
                    return slot
        return None

    def evict_child(self, child: ChildProcess, reason: str) -> ChildProcess | None:
        with self._lock:
            slot = next((s for s in self._slots if s.process is child), None)
            if slot is None:
                SLOT_LOGGER.warning(
                    "Received eviction for unmanaged child '%s'.",
                    child.profile.name,
                )
                return None

            current_profile = slot.profile
            if current_profile is None:
                SLOT_LOGGER.warning(
                    "Slot on ports %s/%s/%s had no profile assigned during eviction.",
                    slot.ports.api_port,
                    slot.ports.stream_port,
                    slot.ports.camoufox_port,
                )
                return None

            SLOT_LOGGER.info(
                "Evicting profile '%s' from ports %s/%s/%s: %s",
                current_profile.name,
                slot.ports.api_port,
                slot.ports.stream_port,
                slot.ports.camoufox_port,
                reason,
            )
            self._terminate_slot(slot, reason)
            self._queue.push(current_profile)

            next_profile = self._queue.pop()
            if next_profile is None:
                SLOT_LOGGER.error(
                    "No profiles available to restart slot after evicting '%s'.",
                    current_profile.name,
                )
                slot.profile = None
                slot.process = None
                return None

            try:
                replacement = self._launch_into_slot(slot, next_profile)
            except Exception as exc:
                SLOT_LOGGER.exception(
                    "Failed to launch replacement profile '%s' after evicting '%s': %s",
                    next_profile.name,
                    current_profile.name,
                    exc,
                )
                slot.profile = None
                slot.process = None
                self._queue.push_front(next_profile)
                return None

            SLOT_LOGGER.info(
                "Recycled profile '%s'; activated '%s' on ports %s/%s/%s.",
                current_profile.name,
                replacement.profile.name,
                slot.ports.api_port,
                slot.ports.stream_port,
                slot.ports.camoufox_port,
            )
            return replacement

    def shutdown(self, reason: str = "coordinator shutdown") -> None:
        with self._lock:
            for slot in self._slots:
                self._terminate_slot(slot, reason)
            self.clear_queue()

    def _launch_into_slot(
        self, slot: ProfileSlot, profile: AuthProfile
    ) -> ChildProcess:
        child = self._launch_child(
            profile,
            slot.ports,
            self._env,
            headless=self._headless,
            log_dir=self._log_dir,
        )
        slot.profile = profile
        slot.process = child
        child.ready = False
        SLOT_LOGGER.info(
            "Launched profile '%s' on ports %s/%s/%s.",
            profile.name,
            slot.ports.api_port,
            slot.ports.stream_port,
            slot.ports.camoufox_port,
        )
        return child

    def _terminate_slot(self, slot: ProfileSlot, reason: str | None = None) -> None:
        child = slot.process
        if child is None:
            return

        process = child.process
        if process.poll() is None:
            reason_suffix = f" ({reason})" if reason else ""
            SLOT_LOGGER.info(
                "Terminating child '%s'%s.",
                child.profile.name,
                reason_suffix,
            )
            process.terminate()
            try:
                process.wait(timeout=_SHUTDOWN_TIMEOUT)
            except subprocess.TimeoutExpired:
                SLOT_LOGGER.warning(
                    "Child '%s' did not exit within %.1fs; forcing kill.",
                    child.profile.name,
                    _SHUTDOWN_TIMEOUT,
                )
                process.kill()
                process.wait()
        slot.process = None
        slot.profile = None

HealthCheckFunc = Callable[[ChildProcess, float], Awaitable[bool]]


class ChildRegistry:
    """Maintain ready/unhealthy child sets and coordinate health polling."""

    def __init__(
        self,
        children: Iterable[ChildProcess],
        *,
        slot_manager: SlotManager | None = None,
        health_check: HealthCheckFunc = wait_for_ready,
        poll_interval: float = 5.0,
        recovery_timeout: float = 10.0,
    ) -> None:
        self._slot_manager = slot_manager
        self._children: dict[str, ChildProcess] = {}
        self._ready: deque[ChildProcess] = deque()
        self._unhealthy: set[str] = set()
        self._health_check = health_check
        self._poll_interval = poll_interval
        self._recovery_timeout = recovery_timeout
        self._lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._monitor_task: asyncio.Task[None] | None = None

        for child in children:
            self._add_child(child)

    def mark_ready(self, child: ChildProcess) -> None:
        if not self._is_alive(child):
            LOGGER.warning(
                "Attempted to mark child '%s' ready but process is not running.",
                child.profile.name,
            )
            return

        child.ready = True
        self._children[child.profile.name] = child
        self._unhealthy.discard(child.profile.name)
        if child not in self._ready:
            self._ready.append(child)
        LOGGER.info("Child '%s' marked healthy and available.", child.profile.name)

    def next_child(self) -> ChildProcess | None:
        while self._ready:
            child = self._ready[0]
            if self._is_alive(child) and child.ready:
                self._ready.rotate(-1)
                return child

            LOGGER.warning(
                "Ready list contained unavailable child '%s'. Demoting.",
                child.profile.name,
            )
            child.ready = False
            self._unhealthy.add(child.profile.name)
            self._ready.popleft()
        return None

    def mark_unhealthy(self, child: ChildProcess, reason: str) -> None:
        LOGGER.warning("Child '%s' marked unhealthy: %s", child.profile.name, reason)
        child.ready = False
        self._remove_from_ready(child)

        if self._slot_manager is not None:
            self._evict(child, reason)
            return

        self._unhealthy.add(child.profile.name)
        self._children[child.profile.name] = child

    def evict_child(self, child: ChildProcess, reason: str) -> ChildProcess | None:
        """Recycle a child immediately, returning the replacement if one launches."""

        child.ready = False
        return self._evict(child, reason)

    async def start_monitoring(self) -> None:
        if self._monitor_task is not None and not self._monitor_task.done():
            return
        self._stop_event.clear()
        loop = asyncio.get_running_loop()
        self._monitor_task = loop.create_task(self._monitor_unhealthy())

    async def shutdown(self) -> None:
        self._stop_event.set()
        if self._monitor_task is not None:
            await self._monitor_task
            self._monitor_task = None

    def ready_children(self) -> List[ChildProcess]:
        return [
            child
            for child in list(self._ready)
            if child.ready and self._is_alive(child)
        ]

    def all_children(self) -> List[ChildProcess]:
        return list(self._children.values())

    def unhealthy_names(self) -> List[str]:
        return sorted(self._unhealthy)

    async def _monitor_unhealthy(self) -> None:
        try:
            while not self._stop_event.is_set():
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=self._poll_interval
                    )
                except asyncio.TimeoutError:
                    pass

                if self._stop_event.is_set():
                    break

                await self._poll_once()
        except asyncio.CancelledError:
            LOGGER.info("Child registry monitor cancelled.")
        except Exception as exc:
            LOGGER.exception("Monitor loop failed: %s", exc)

    async def _poll_once(self) -> None:
        async with self._lock:
            for name in list(self._unhealthy):
                child = self._children.get(name)
                if child is None:
                    self._unhealthy.discard(name)
                    continue
                if not self._is_alive(child):
                    LOGGER.warning(
                        "Child '%s' process exited while unhealthy; leaving demoted.",
                        name,
                    )
                    continue
                try:
                    ready = await self._health_check(child, self._recovery_timeout)
                except Exception as exc:
                    LOGGER.debug("Health recheck for '%s' failed: %s", name, exc)
                    continue

                if ready:
                    self.mark_ready(child)
                else:
                    LOGGER.warning(
                        "Child '%s' failed readiness during recovery poll; scheduling recycle.",
                        name,
                    )
                    self.mark_unhealthy(child, "Readiness timeout during recovery poll")

    def _evict(self, child: ChildProcess, reason: str) -> ChildProcess | None:
        if self._slot_manager is None:
            LOGGER.debug(
                "Slot manager not configured; cannot evict child '%s'.",
                child.profile.name,
            )
            self._unhealthy.add(child.profile.name)
            self._children[child.profile.name] = child
            return None

        replacement = self._slot_manager.evict_child(child, reason)
        self._remove_child(child)
        if replacement is not None:
            self._add_child(replacement)
        return replacement

    def _add_child(self, child: ChildProcess) -> None:
        self._children[child.profile.name] = child
        if child.ready and self._is_alive(child):
            if child not in self._ready:
                self._ready.append(child)
            self._unhealthy.discard(child.profile.name)
        else:
            child.ready = False
            self._unhealthy.add(child.profile.name)

    def _remove_child(self, child: ChildProcess) -> None:
        self._children.pop(child.profile.name, None)
        self._remove_from_ready(child)
        self._unhealthy.discard(child.profile.name)

    def _remove_from_ready(self, child: ChildProcess) -> None:
        self._ready = deque(c for c in self._ready if c is not child)

    @staticmethod
    def _is_alive(child: ChildProcess) -> bool:
        return child.process.poll() is None
