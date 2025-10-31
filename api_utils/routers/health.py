from __future__ import annotations

from asyncio import Queue
from typing import Any, Dict

from fastapi import Depends
from fastapi.responses import JSONResponse

from config import get_environment_variable

from ..dependencies import get_request_queue, get_server_state, get_worker_task


def _build_status_payload(
    server_state: Dict[str, Any],
    *,
    worker_running: bool,
    queue_length: int,
    launch_mode: str,
) -> tuple[Dict[str, Any], int]:
    browser_page_critical = launch_mode != "direct_debug_no_browser"
    core_ready = [not server_state["is_initializing"], server_state["is_playwright_ready"]]
    if browser_page_critical:
        core_ready.extend(
            [server_state["is_browser_connected"], server_state["is_page_ready"]]
        )

    status_ok = all(core_ready) and worker_running
    message_bits: list[str] = []

    if server_state["is_initializing"]:
        message_bits.append("初始化进行中")
    if not server_state["is_playwright_ready"]:
        message_bits.append("Playwright 未就绪")
    if browser_page_critical:
        if not server_state["is_browser_connected"]:
            message_bits.append("浏览器未连接")
        if not server_state["is_page_ready"]:
            message_bits.append("页面未就绪")
    if not worker_running:
        message_bits.append("Worker 未运行")

    status = {
        "status": "OK" if status_ok else "Error",
        "message": "",
        "details": {
            **server_state,
            "workerRunning": worker_running,
            "queueLength": queue_length,
            "launchMode": launch_mode,
            "browserAndPageCritical": browser_page_critical,
        },
    }

    if status_ok:
        status["message"] = f"服务运行中;队列长度: {queue_length}。"
        return status, 200

    status["message"] = (
        f"服务不可用;问题: {(', '.join(message_bits) or '未知原因')}. 队列长度: {queue_length}."
    )
    return status, 503


async def live_check() -> JSONResponse:
    """Simple liveness endpoint; returns 200 while the process is alive."""
    return JSONResponse(status_code=200, content={"status": "alive"})


async def ready_check(
    server_state: Dict[str, Any] = Depends(get_server_state),
    worker_task=Depends(get_worker_task),
    request_queue: Queue = Depends(get_request_queue),
) -> JSONResponse:
    launch_mode = get_environment_variable("LAUNCH_MODE", "unknown")
    worker_running = bool(worker_task and not worker_task.done())
    queue_length = request_queue.qsize() if request_queue else -1
    payload, status_code = _build_status_payload(
        server_state,
        worker_running=worker_running,
        queue_length=queue_length,
        launch_mode=launch_mode,
    )
    return JSONResponse(content=payload, status_code=status_code)


async def health_check(
    server_state: Dict[str, Any] = Depends(get_server_state),
    worker_task=Depends(get_worker_task),
    request_queue: Queue = Depends(get_request_queue),
) -> JSONResponse:
    """Compatibility shim mapping /health to /ready semantics."""
    response = await ready_check(server_state, worker_task, request_queue)
    response.headers["X-Deprecation-Notice"] = "Use /ready instead of /health."
    return response
