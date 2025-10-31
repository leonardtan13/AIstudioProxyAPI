from __future__ import annotations

import json
import logging
from typing import Dict

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from models.chat import ChatCompletionRequest

from .manager import ChildRegistry
from .routing import (
    ChildRequestError,
    broadcast_cancel,
    forward_completion,
    forward_models,
)

LOGGER = logging.getLogger("Coordinator.API")


def _relay_response(source: httpx.Response) -> Response:
    headers = {
        key: value
        for key, value in source.headers.items()
        if key.lower() not in {"content-length", "transfer-encoding", "connection"}
    }
    return Response(
        content=source.content, status_code=source.status_code, headers=headers
    )


def _no_healthy_children_error() -> HTTPException:
    return HTTPException(status_code=503, detail="No healthy child proxies available.")


def create_app(registry: ChildRegistry) -> FastAPI:
    app = FastAPI(title="AI Studio Coordinator", version="0.1.0")

    @app.on_event("startup")
    async def _startup() -> None:  # pragma: no cover - FastAPI lifecycle glue
        await registry.start_monitoring()

    @app.on_event("shutdown")
    async def _shutdown() -> None:  # pragma: no cover - FastAPI lifecycle glue
        await registry.shutdown()

    @app.get("/live")
    async def live() -> Dict[str, str]:
        return {"status": "alive"}

    def _ready_payload() -> JSONResponse:
        ready = [child.profile.name for child in registry.ready_children()]
        unhealthy = registry.unhealthy_names()
        status = "ready" if ready else "degraded"
        content = {
            "status": status,
            "ready_children": ready,
            "unhealthy_children": unhealthy,
            "total_children": len(registry.all_children()),
        }
        status_code = 200 if ready else 503
        if status_code == 503:
            LOGGER.warning(
                "Coordinator readiness failing: no healthy children. Unhealthy children: %s",
                unhealthy or "(none registered)",
            )
        return JSONResponse(status_code=status_code, content=content)

    @app.get("/ready")
    async def ready() -> JSONResponse:
        return _ready_payload()

    @app.get("/health")
    async def health() -> JSONResponse:
        response = _ready_payload()
        response.headers["X-Deprecation-Notice"] = "Use /ready instead of /health."
        return response

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Response:
        try:
            payload_dict = await request.json()
        except (
            json.JSONDecodeError
        ) as exc:  # pragma: no cover - fastapi handles most cases
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="Invalid JSON payload."
            ) from exc

        try:
            payload = ChatCompletionRequest.model_validate(payload_dict)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()) from exc

        if payload.stream:
            raise HTTPException(
                status_code=400, detail="Streaming is not supported by the coordinator."
            )

        attempted: set[str] = set()
        while True:
            child = registry.next_child()
            if child is None or child.profile.name in attempted:
                raise _no_healthy_children_error()
            attempted.add(child.profile.name)

            LOGGER.info("Routing completion request to child '%s'.", child.profile.name)
            try:
                child_response = await forward_completion(child, payload)
            except ChildRequestError as exc:
                if exc.retryable:
                    registry.mark_unhealthy(child, str(exc))
                    continue
                raise HTTPException(status_code=502, detail=str(exc)) from exc

            return _relay_response(child_response)

    @app.get("/v1/models")
    async def list_models() -> Response:
        attempted: set[str] = set()
        while True:
            child = registry.next_child()
            if child is None or child.profile.name in attempted:
                raise _no_healthy_children_error()
            attempted.add(child.profile.name)

            LOGGER.info("Routing models request to child '%s'.", child.profile.name)
            try:
                child_response = await forward_models(child)
            except ChildRequestError as exc:
                if exc.retryable:
                    registry.mark_unhealthy(child, str(exc))
                    continue
                raise HTTPException(status_code=502, detail=str(exc)) from exc

            return _relay_response(child_response)

    @app.post("/v1/cancel/{req_id}")
    async def cancel_request(req_id: str) -> JSONResponse:
        LOGGER.info("Broadcasting cancellation for request '%s'.", req_id)
        result = await broadcast_cancel(registry.all_children(), req_id)
        status_code = 200 if result.success else 404
        return JSONResponse(
            status_code=status_code,
            content={
                "success": result.success,
                "completed": result.responders,
                "failed": result.failures,
            },
        )

    return app
