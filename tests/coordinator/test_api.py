from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from coordinator.api import create_app
from coordinator.manager import ChildRegistry
from coordinator.types import AuthProfile, ChildPorts, ChildProcess


class _StubProcess:
    def __init__(self, running: bool = True) -> None:
        self._running = running

    def poll(self) -> int | None:
        return None if self._running else 1


def _make_child(tmp_path: Path, name: str, *, ready: bool = False) -> ChildProcess:
    profile_path = tmp_path / f"{name}.json"
    profile_path.write_text("{}", encoding="utf-8")
    profile = AuthProfile(name=name, path=profile_path)
    ports = ChildPorts(api_port=8000, stream_port=0, camoufox_port=0)
    return ChildProcess(profile=profile, ports=ports, process=_StubProcess(), ready=ready)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_live_endpoint_reports_alive(tmp_path: Path) -> None:
    registry = ChildRegistry([])
    app = create_app(registry)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/live")
    assert response.status_code == 200
    assert response.json()["status"] == "alive"


@pytest.mark.anyio
async def test_ready_endpoint_returns_503_when_no_children_ready(tmp_path: Path) -> None:
    child = _make_child(tmp_path, "alpha", ready=False)
    registry = ChildRegistry([child])
    app = create_app(registry)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["ready_children"] == []
    assert body["unhealthy_children"] == ["alpha"]


@pytest.mark.anyio
async def test_ready_endpoint_returns_200_when_child_ready(tmp_path: Path) -> None:
    child = _make_child(tmp_path, "beta", ready=True)
    registry = ChildRegistry([child])
    app = create_app(registry)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["ready_children"] == ["beta"]


@pytest.mark.anyio
async def test_health_alias_delegates_to_ready(tmp_path: Path) -> None:
    child = _make_child(tmp_path, "gamma", ready=False)
    registry = ChildRegistry([child])
    app = create_app(registry)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        ready_response = await client.get("/ready")
        health_response = await client.get("/health")
    assert health_response.status_code == ready_response.status_code
    assert health_response.json() == ready_response.json()
    assert health_response.headers.get("X-Deprecation-Notice") == "Use /ready instead of /health."
