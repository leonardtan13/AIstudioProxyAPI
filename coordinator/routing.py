from __future__ import annotations

import logging
from typing import Iterable

import httpx

from models.chat import ChatCompletionRequest

from .types import CancelResult, ChildProcess

LOGGER = logging.getLogger("Coordinator.Routing")


class ChildRequestError(Exception):
    """Raised when a request to a child backend fails."""

    def __init__(self, child: ChildProcess, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.child = child
        self.retryable = retryable


async def forward_completion(
    child: ChildProcess,
    payload: ChatCompletionRequest,
    *,
    timeout: float = 60.0,
) -> httpx.Response:
    """Forward a completion request to the specified child."""

    url = f"http://127.0.0.1:{child.ports.api_port}/v1/chat/completions"
    data = payload.model_dump(mode="json")
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json=data)
    except httpx.HTTPError as exc:
        raise ChildRequestError(child, f"Request failed: {exc}") from exc

    if response.status_code >= 500:
        raise ChildRequestError(
            child,
            f"Child responded with {response.status_code}",
        )

    return response


async def forward_models(
    child: ChildProcess,
    *,
    timeout: float = 15.0,
) -> httpx.Response:
    """Forward a /v1/models request to the specified child."""

    url = f"http://127.0.0.1:{child.ports.api_port}/v1/models"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        raise ChildRequestError(child, f"Request failed: {exc}") from exc

    if response.status_code >= 500:
        raise ChildRequestError(
            child,
            f"Child responded with {response.status_code}",
        )

    return response


async def broadcast_cancel(
    children: Iterable[ChildProcess],
    req_id: str,
    *,
    timeout: float = 10.0,
) -> CancelResult:
    """Send a cancellation event to every known child process."""

    responders: list[str] = []
    failures: list[str] = []
    async with httpx.AsyncClient(timeout=timeout) as client:
        for child in children:
            url = f"http://127.0.0.1:{child.ports.api_port}/v1/cancel/{req_id}"
            try:
                response = await client.post(url)
            except httpx.HTTPError as exc:
                LOGGER.debug(
                    "Cancellation for '%s' failed on '%s': %s",
                    req_id,
                    child.profile.name,
                    exc,
                )
                failures.append(child.profile.name)
                continue

            if response.status_code == 200:
                responders.append(child.profile.name)
            else:
                failures.append(child.profile.name)

    return CancelResult(
        success=bool(responders), responders=responders, failures=failures
    )
