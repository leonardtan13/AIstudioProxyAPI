from __future__ import annotations

import asyncio
import json

import pytest

from api_utils.routers.health import health_check, live_check, ready_check


class _StubTask:
    def __init__(self, *, done: bool) -> None:
        self._done = done

    def done(self) -> bool:
        return self._done


def _base_server_state() -> dict[str, object]:
    return {
        "is_initializing": False,
        "is_playwright_ready": True,
        "is_browser_connected": True,
        "is_page_ready": True,
    }


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_live_check_returns_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LAUNCH_MODE", raising=False)
    response = await live_check()
    assert response.status_code == 200
    payload = json.loads(response.body)
    assert payload == {"status": "alive"}


@pytest.mark.anyio
async def test_ready_check_reports_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAUNCH_MODE", "standard")
    queue: asyncio.Queue[int] = asyncio.Queue()
    queue.put_nowait(42)
    server_state = _base_server_state()
    response = await ready_check(
        server_state=server_state,
        worker_task=_StubTask(done=False),
        request_queue=queue,
    )
    assert response.status_code == 200
    payload = json.loads(response.body)
    assert payload["status"] == "OK"
    assert payload["details"]["workerRunning"] is True
    assert payload["details"]["queueLength"] == 1


@pytest.mark.anyio
async def test_ready_check_reports_unhealthy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAUNCH_MODE", "standard")
    queue: asyncio.Queue[int] = asyncio.Queue()
    server_state = _base_server_state()
    server_state["is_playwright_ready"] = False
    response = await ready_check(
        server_state=server_state,
        worker_task=_StubTask(done=True),
        request_queue=queue,
    )
    assert response.status_code == 503
    payload = json.loads(response.body)
    assert payload["status"] == "Error"
    assert "Playwright 未就绪" in payload["message"]


@pytest.mark.anyio
async def test_health_check_delegates_to_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAUNCH_MODE", "standard")
    queue: asyncio.Queue[int] = asyncio.Queue()
    server_state = _base_server_state()
    ready_response = await ready_check(
        server_state=server_state,
        worker_task=_StubTask(done=False),
        request_queue=queue,
    )
    health_response = await health_check(
        server_state=server_state,
        worker_task=_StubTask(done=False),
        request_queue=queue,
    )
    assert health_response.status_code == ready_response.status_code
    assert health_response.body == ready_response.body
    assert (
        health_response.headers.get("X-Deprecation-Notice")
        == "Use /ready instead of /health."
    )
