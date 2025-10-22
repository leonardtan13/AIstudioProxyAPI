from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any, Dict, cast

import httpx
import pytest

from coordinator.api import create_app
from coordinator.manager import ChildRegistry
from coordinator.routing import (
    ChildRequestError,
    broadcast_cancel,
    forward_completion,
    forward_models,
)
from coordinator.types import AuthProfile, CancelResult, ChildPorts, ChildProcess
from models.chat import ChatCompletionRequest, Message


class DummyProcess:
    def __init__(self, running: bool = True) -> None:
        self._running = running
        self.returncode: int | None = None if running else 0

    def poll(self) -> int | None:
        return None if self._running else 0

    def terminate(self) -> None:  # pragma: no cover - interface compatibility
        self._running = False
        self.returncode = 0


def make_child(tmp_path: Path, name: str, api_port: int = 9000) -> ChildProcess:
    profile_path = tmp_path / f"{name}.json"
    profile_path.write_text("{}", encoding="utf-8")
    profile = AuthProfile(name=name, path=profile_path)
    ports = ChildPorts(api_port=api_port, stream_port=0, camoufox_port=0)
    process = cast(subprocess.Popen[str], DummyProcess())
    return ChildProcess(profile=profile, ports=ports, process=process)


def make_request_payload() -> ChatCompletionRequest:
    return ChatCompletionRequest(
        messages=[Message(role="user", content="hello")],
        stream=False,
    )


class StubAsyncClient:
    def __init__(self, *, responses: list[Any], exc: Exception | None = None) -> None:
        self._responses = responses
        self._exc = exc
        self.calls: list[Dict[str, Any]] = []

    async def __aenter__(self) -> "StubAsyncClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def post(
        self, url: str, json: Dict[str, Any] | None = None
    ) -> httpx.Response:
        self.calls.append({"method": "POST", "url": url, "json": json})
        if self._exc:
            raise self._exc
        return self._responses.pop(0)

    async def get(self, url: str) -> httpx.Response:
        self.calls.append({"method": "GET", "url": url})
        if self._exc:
            raise self._exc
        return self._responses.pop(0)


def patch_async_client(
    monkeypatch: pytest.MonkeyPatch, client: StubAsyncClient
) -> None:
    class _Factory:
        def __init__(self, stub: StubAsyncClient) -> None:
            self._stub = stub

        def __call__(self, *args, **kwargs) -> StubAsyncClient:
            return self._stub

    monkeypatch.setattr(httpx, "AsyncClient", _Factory(client))


def test_forward_completion_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    child = make_child(tmp_path, "primary")
    payload = make_request_payload()
    response = httpx.Response(status_code=200, json={"ok": True})
    patch_async_client(monkeypatch, StubAsyncClient(responses=[response]))

    result = asyncio.run(forward_completion(child, payload))

    assert result.status_code == 200


def test_forward_completion_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    child = make_child(tmp_path, "primary")
    payload = make_request_payload()
    exc = httpx.ConnectError("boom", request=httpx.Request("POST", "http://test"))
    patch_async_client(monkeypatch, StubAsyncClient(responses=[], exc=exc))

    with pytest.raises(ChildRequestError):
        asyncio.run(forward_completion(child, payload))


def test_forward_models_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    child = make_child(tmp_path, "primary")
    response = httpx.Response(status_code=200, json={"data": []})
    patch_async_client(monkeypatch, StubAsyncClient(responses=[response]))

    result = asyncio.run(forward_models(child))

    assert result.status_code == 200


def test_broadcast_cancel_collects_results(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    child_ok = make_child(tmp_path, "ok", api_port=9100)
    child_fail = make_child(tmp_path, "fail", api_port=9200)
    responses = [
        httpx.Response(status_code=200, json={"success": True}),
        httpx.Response(status_code=500),
    ]
    patch_async_client(monkeypatch, StubAsyncClient(responses=responses))

    result = asyncio.run(broadcast_cancel([child_ok, child_fail], "abc123"))

    assert isinstance(result, CancelResult)
    assert result.success is True
    assert result.responders == ["ok"]
    assert result.failures == ["fail"]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def build_registry(children: list[ChildProcess]) -> ChildRegistry:
    async def fake_health(child: ChildProcess, timeout: float) -> bool:
        await asyncio.sleep(0)
        return child.ready

    registry = ChildRegistry(children, health_check=fake_health, poll_interval=0.05)
    for child in children:
        registry.mark_ready(child)
    return registry


@pytest.mark.anyio("asyncio")
async def test_chat_completion_routes_to_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    child = make_child(tmp_path, "solo")
    registry = await build_registry([child])

    async def fake_forward_completion(
        child_process: ChildProcess, payload: ChatCompletionRequest
    ) -> httpx.Response:
        assert child_process.profile.name == "solo"
        return httpx.Response(
            status_code=200, json={"child": child_process.profile.name}
        )

    monkeypatch.setattr(
        "coordinator.api.forward_completion",
        fake_forward_completion,
    )

    app = create_app(registry)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 200
    assert response.json() == {"child": "solo"}


@pytest.mark.anyio("asyncio")
async def test_chat_completion_retries_on_unhealthy_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    child_a = make_child(tmp_path, "a", api_port=9300)
    child_b = make_child(tmp_path, "b", api_port=9400)
    registry = await build_registry([child_a, child_b])

    async def fake_forward_completion(
        child_process: ChildProcess, payload: ChatCompletionRequest
    ) -> httpx.Response:
        if child_process.profile.name == "a":
            raise ChildRequestError(child_process, "boom")
        return httpx.Response(
            status_code=200, json={"child": child_process.profile.name}
        )

    monkeypatch.setattr(
        "coordinator.api.forward_completion",
        fake_forward_completion,
    )

    app = create_app(registry)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 200
    assert response.json() == {"child": "b"}
    assert registry.unhealthy_names() == ["a"]


@pytest.mark.anyio("asyncio")
async def test_chat_completion_no_children(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    child = make_child(tmp_path, "solo")
    registry = ChildRegistry([child], health_check=lambda *_: asyncio.sleep(0))

    app = create_app(registry)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 503


@pytest.mark.anyio("asyncio")
async def test_models_route(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    child = make_child(tmp_path, "solo")
    registry = await build_registry([child])

    async def fake_forward_models(child_process: ChildProcess) -> httpx.Response:
        return httpx.Response(
            status_code=200, json={"data": [child_process.profile.name]}
        )

    monkeypatch.setattr("coordinator.api.forward_models", fake_forward_models)

    app = create_app(registry)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/v1/models")

    assert response.status_code == 200
    assert response.json() == {"data": ["solo"]}


@pytest.mark.anyio("asyncio")
async def test_cancel_broadcast(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    child = make_child(tmp_path, "solo")
    registry = await build_registry([child])

    async def fake_broadcast(children, req_id: str) -> CancelResult:
        assert req_id == "abc"
        return CancelResult(success=True, responders=["solo"], failures=[])

    monkeypatch.setattr("coordinator.api.broadcast_cancel", fake_broadcast)

    app = create_app(registry)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/v1/cancel/abc")

    assert response.status_code == 200
    assert response.json() == {"success": True, "completed": ["solo"], "failed": []}


@pytest.mark.anyio("asyncio")
async def test_streaming_request_rejected(tmp_path: Path) -> None:
    child = make_child(tmp_path, "solo")
    registry = await build_registry([child])
    app = create_app(registry)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hi"}], "stream": True},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "Streaming is not supported by the coordinator."
