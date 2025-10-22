from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from .types import ChildProcess

LOGGER = logging.getLogger("Coordinator.Health")


async def wait_for_ready(child: ChildProcess, timeout: float = 30.0) -> bool:
    """Poll the child `/health` endpoint until it reports ready or timeout elapses."""

    deadline = time.monotonic() + max(timeout, 0.0)
    url = f"http://127.0.0.1:{child.ports.api_port}/health"
    attempt = 0
    async with httpx.AsyncClient(timeout=5.0) as client:
        while time.monotonic() <= deadline:
            attempt += 1
            try:
                response = await client.get(url)
                if response.status_code != 200:
                    LOGGER.debug(
                        "Health check for %s returned %s.",
                        child.profile.name,
                        response.status_code,
                    )
                else:
                    data: dict[str, Any] = response.json()
                    if data.get("status") == "OK":
                        child.ready = True
                        LOGGER.info(
                            "Child '%s' became ready after %s attempt(s).",
                            child.profile.name,
                            attempt,
                        )
                        return True
            except (httpx.HTTPError, ValueError) as exc:
                LOGGER.debug("Health check for %s failed: %s", child.profile.name, exc)

            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(1.0)

    LOGGER.warning(
        "Timed out waiting for child '%s' to become ready after %s attempt(s).",
        child.profile.name,
        attempt,
    )
    return False
