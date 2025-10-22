# Multi-Process Coordinator For AI Studio Proxy

## Overview

Introduce a standalone coordinator that launches one `launch_camoufox.py` process per cookie profile, monitors their health, and exposes a simplified FastAPI front-end on port 2048 that round-robins `/v1/chat/completions` and `/v1/models` while broadcasting request cancellations. This avoids deep in-process refactors and leverages the existing single-browser assumption inside each child proxy.

## Current State Analysis

The current application serves all traffic through a single Playwright page and queue housed in one FastAPI process.

### Key Discoveries:
- `launch_camoufox.py:520-578` already accepts `--server-port`, `--stream-port`, `--camoufox-debug-port`, and `--active-auth-json`, so we can spin up independent processes with unique port assignments per cookie file.
- `launch_camoufox.py:809-914` resolves the active storage-state JSON when `--active-auth-json` is provided, which is ideal for deterministically binding a child to a specific profile.
- `server.py:95-138` and `api_utils/queue_worker.py:1-358` show each process maintains a single global queue, page, and worker; running multiple processes is the safest way to gain concurrency.
- `api_utils/app.py:316-332` registers `/v1/chat/completions`, `/v1/models`, and `/v1/cancel/{req_id}`; the coordinator only needs to proxy these plus `/health` for readiness checks.
- `api_utils/routers/chat.py:16-47` generates unique `req_id` values and enqueues work, so cancellations must fan out to every child because the coordinator cannot infer which backend owns a request.

## Desired End State

Running the coordinator on port 2048 launches one child proxy per `*.json` auth file, assigns sequential port triples (FastAPI, Camoufox debug, stream proxy), and maintains a healthy pool. `/v1/chat/completions` requests are rejected with 400 when `stream=true`; otherwise they are distributed round-robin among healthy children and the JSON responses are relayed transparently. `/v1/models` always responds via the next healthy child. `/v1/cancel/{req_id}` broadcasts to all children and reports success when any backend confirms cancellation. CLI operators can inspect coordinator logs to see child status, and a graceful shutdown tears down every subprocess.

Success is verified through automated unit tests around process orchestration and routing, plus manual smoke tests that demonstrate multi-profile handling.

## What We're NOT Doing

- No in-process multi-browser refactors or shared request queue changes inside `server.py`.
- No SSE/streaming proxying; `stream=true` is rejected explicitly.
- No automatic WebSocket (`/ws/logs`) or auxiliary endpoints; clients use the coordinator only for `/v1/chat/completions`, `/v1/models`, and `/v1/cancel/{req_id}`.
- No automatic restart of persistently crashing children in the first iteration (we will surface unhealthy status instead).

## Implementation Approach

1. Build a `coordinator` package with a typed CLI (`main.py`) that enumerates cookie files, calculates port assignments, and spawns child processes using `launch_camoufox.py`.
2. Track subprocess metadata, poll `/health` until each child is ready, and maintain a deque of healthy backends with automatic demotion on failure.
3. Expose a FastAPI app on port 2048 that rejects streaming requests, forwards `/v1/chat/completions` and `/v1/models` via `httpx.AsyncClient`, and broadcasts `/v1/cancel/{req_id}` to every child.
4. Add targeted tests for process selection, failure handling, request routing, and cancellation fan-out; document usage and fallback behavior.

## Phase 1: Coordinator Skeleton & Process Launching

### Overview
Create the `coordinator` package, define typed configuration models, and implement deterministic child spawning with sequential port offsets.

### Changes Required:

#### 1. Coordinator Package Structure
**File**: `coordinator/__init__.py`, `coordinator/config.py`, `coordinator/types.py` (new)\
**Changes**: Define `AuthProfile`, `ChildPorts`, and `ChildProcess` dataclasses (with type hints) plus CLI configuration defaults.

```python
@dataclass(frozen=True)
class AuthProfile:
    name: str
    path: Path

@dataclass(frozen=True)
class ChildPorts:
    api_port: int
    stream_port: int
    camoufox_port: int

@dataclass
class ChildProcess:
    profile: AuthProfile
    ports: ChildPorts
    process: subprocess.Popen[str]
    ready: bool = False
```

#### 2. CLI and Profile Enumeration
**File**: `coordinator/main.py` (new)\
**Changes**: Implement `parse_args()` with typed return, `discover_profiles(directory: Path) -> list[AuthProfile]`, and `assign_ports(count: int, base_api: int, base_stream: int, base_camoufox: int) -> list[ChildPorts]`.

#### 3. Process Launch Helper
**File**: `coordinator/launcher.py` (new)\
**Changes**: Provide `launch_child(profile: AuthProfile, ports: ChildPorts, env: Mapping[str, str]) -> ChildProcess`, ensuring we pass `--headless`, `--active-auth-json`, `--server-port`, `--stream-port`, and `--camoufox-debug-port`. Capture stdout/stderr into rotating log files under `logs/coordinator`.

#### 4. Graceful Shutdown Hook
**File**: `coordinator/main.py`\
**Changes**: Register signal handlers that iterate over tracked `ChildProcess` objects and terminate them sequentially with timeouts before sending `kill`.

### Success Criteria:

#### Automated Verification:
- [x] New unit tests for `discover_profiles` and `assign_ports`: `poetry run pytest tests/coordinator/test_config.py`.

#### Manual Verification:
- [ ] Run `python -m coordinator.main --profiles auth_profiles/active` and confirm children spawn with expected ports and log files.
- [ ] Interrupt with `Ctrl+C` and verify all child processes terminate.

---

## Phase 2: Health & Lifecycle Management

### Overview
Implement readiness polling against `/health`, maintain healthy/ unhealthy sets, and surface status via coordinator logging.

### Changes Required:

#### 1. Health Poller
**File**: `coordinator/health.py` (new)\
**Changes**: Define `async def wait_for_ready(child: ChildProcess, timeout: float) -> bool` using `httpx.AsyncClient`. Parse the JSON status and set `child.ready = True` when `"status" == "OK"`.

#### 2. Child Registry & Monitoring
**File**: `coordinator/manager.py` (new)\
**Changes**: Provide `class ChildRegistry` with typed methods:

```python
class ChildRegistry:
    def __init__(self, children: list[ChildProcess]):
        self._ready: deque[ChildProcess] = deque()
        self._unhealthy: set[str] = set()

    def mark_ready(self, child: ChildProcess) -> None: ...
    def next_child(self) -> ChildProcess | None: ...
    def mark_unhealthy(self, child: ChildProcess, reason: str) -> None: ...
```

Log status changes with coordinates for troubleshooting.

#### 3. Periodic Liveness Checks
**File**: `coordinator/manager.py`\
**Changes**: Schedule an asyncio task that periodically re-polls unhealthy children; if they recover, re-add them to the ready deque.

### Success Criteria:

#### Automated Verification:
- [x] Registry behavior covered by unit tests: `poetry run pytest tests/coordinator/test_manager.py`.

#### Manual Verification:
- [ ] Start the coordinator; stop one child manually and confirm it is demoted and subsequent requests skip it.
- [ ] Restore the child (restart command) and ensure it re-enters the pool after health recovery.

---

## Phase 3: Request Routing Layer

### Overview
Expose the coordinator FastAPI app on port 2048, implement round-robin routing for `/v1/chat/completions`, `/v1/models`, and broadcast cancellations, rejecting streaming requests.

### Changes Required:

#### 1. FastAPI Application
**File**: `coordinator/api.py` (new)\
**Changes**: Create `create_app(registry: ChildRegistry) -> FastAPI` with three endpoints:
- `POST /v1/chat/completions`: Validate `stream` flag; on `True` return `HTTPException(status_code=400)`. Otherwise call `await forward_completion(...)`.
- `GET /v1/models`: Call `await forward_models(...)`.
- `POST /v1/cancel/{req_id}`: Broadcast to every child until one returns 200.

#### 2. Forwarding Helpers
**File**: `coordinator/routing.py` (new)\
**Changes**: Implement typed async functions:

```python
async def forward_completion(child: ChildProcess, payload: ChatCompletionRequest) -> Response: ...
async def forward_models(child: ChildProcess) -> Response: ...
async def broadcast_cancel(children: Iterable[ChildProcess], req_id: str) -> CancelResult: ...
```

Use `httpx.AsyncClient` with timeouts; on failure mark the child unhealthy and retry with the next one.

#### 3. Coordinator Entrypoint
**File**: `coordinator/main.py`\
**Changes**: Wire together CLI parsing, child launches, registry creation, FastAPI app creation, and serve via `uvicorn.run(app, port=2048)`.

### Success Criteria:

#### Automated Verification:
- [x] Routing unit tests using respx/httpx mocks: `poetry run pytest tests/coordinator/test_routing.py`.

#### Manual Verification:
- [ ] Launch coordinator with at least two profiles; send sequential `/v1/chat/completions` and observe round-robin port usage in logs.
- [ ] Issue a `stream=true` request and confirm a 400 response.
- [ ] Call `/v1/models` repeatedly and ensure responses succeed even if the first child is unhealthy.
- [ ] Call `/v1/cancel/{req_id}` during a pending request and verify the child logs a cancellation.

---

## Phase 4: Testing & Documentation

### Overview
Add integration smoke tests and document coordinator usage for developers.

### Changes Required:

#### 1. Integration Smoke Test
**File**: `tests/integration/test_coordinator_end_to_end.py` (new)\
**Changes**: Spawn a lightweight fake backend (using `httpx.MockTransport` or temporary FastAPI apps) to simulate child proxies and validate round-robin + cancellation semantics.

#### 2. Documentation Update
**File**: `docs/plan/README.md` or new `docs/coordinator.md`\
**Changes**: Document CLI options, port allocation formula, health behavior, and known limitations (no streaming, limited endpoints).

#### 3. Tooling
**File**: `pyproject.toml` / `poetry.lock`\
**Changes**: Add `httpx`, `respx`, or other dependencies if not already available for coordinator testing.

### Success Criteria:

#### Automated Verification:
- [ ] End-to-end coordinator test passes: `poetry run pytest tests/integration/test_coordinator_end_to_end.py`.
- [ ] Formatting & linting succeed (reuse existing tooling, e.g., `poetry run ruff check coordinator` if configured).

#### Manual Verification:
- [ ] Follow the new doc instructions to launch coordinator + children with real profiles and confirm successful request flow.

---

## Testing Strategy

- **Unit Tests**: Cover port assignment, profile discovery, registry logic, and routing behavior using mocks with typed payloads.
- **Integration Tests**: Simulate multiple child proxies to verify round-robin behavior, cancellation broadcast, and unhealthy child handling.
- **Manual Tests**: Launch with real cookie profiles, submit concurrent requests, intentionally kill a child, and validate fallback.

## Performance Considerations

- Child processes are isolated, so CPU/memory scale linearly with profile count; document recommended limits.
- Sequential round-robin ensures at most one active request per child, maintaining compatibility with the single-page constraint.
- `httpx` timeouts and health demotion prevent the coordinator from blocking on hung children.

## Migration Notes

- Existing single-proxy workflows can continue launching `launch_camoufox.py` directly; the coordinator is an optional wrapper.
- The coordinator requires specifying a profile directory; if none is found it exits early with a clear error.
- Update deployment scripts to start `python -m coordinator.main --profiles <dir>` instead of `launch_camoufox.py` when multi-profile support is desired.

## References

- `launch_camoufox.py:520-578` – CLI options for per-process port configuration.
- `launch_camoufox.py:809-914` – Active auth JSON resolution logic.
- `server.py:95-138` – Single-process global state and page lifecycle.
- `api_utils/app.py:316-332` – Existing endpoints the coordinator must proxy.
- `api_utils/routers/chat.py:16-47` – Request enqueueing and `req_id` generation details.
- `api_utils/routers/models.py:1-41` – `/v1/models` handler behavior.
- `api_utils/routers/queue.py:30-62` – Cancellation expectations necessitating broadcast.
