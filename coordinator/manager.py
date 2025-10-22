from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Awaitable, Callable, Iterable, List

from .health import wait_for_ready
from .types import ChildProcess

LOGGER = logging.getLogger("Coordinator.ChildRegistry")

HealthCheckFunc = Callable[[ChildProcess, float], Awaitable[bool]]


class ChildRegistry:
    """Maintain ready/unhealthy child sets and coordinate health polling."""

    def __init__(
        self,
        children: Iterable[ChildProcess],
        *,
        health_check: HealthCheckFunc = wait_for_ready,
        poll_interval: float = 5.0,
        recovery_timeout: float = 10.0,
    ) -> None:
        self._children = {child.profile.name: child for child in children}
        self._ready = deque(
            child
            for child in self._children.values()
            if child.ready and self._is_alive(child)
        )
        self._unhealthy: set[str] = {
            child.profile.name
            for child in self._children.values()
            if child.profile.name not in {c.profile.name for c in self._ready}
        }
        self._health_check = health_check
        self._poll_interval = poll_interval
        self._recovery_timeout = recovery_timeout
        self._lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._monitor_task: asyncio.Task[None] | None = None

    def mark_ready(self, child: ChildProcess) -> None:
        if not self._is_alive(child):
            LOGGER.warning(
                "Attempted to mark child '%s' ready but process is not running.",
                child.profile.name,
            )
            return

        child.ready = True
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
        self._unhealthy.add(child.profile.name)
        self._ready = deque(
            ready_child
            for ready_child in self._ready
            if ready_child.profile.name != child.profile.name
        )

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
                    if await self._health_check(child, self._recovery_timeout):
                        self.mark_ready(child)
                except Exception as exc:
                    LOGGER.debug("Health recheck for '%s' failed: %s", name, exc)

    @staticmethod
    def _is_alive(child: ChildProcess) -> bool:
        return child.process.poll() is None
