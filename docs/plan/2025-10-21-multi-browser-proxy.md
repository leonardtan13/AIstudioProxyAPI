# Multi-Browser Backing For Port 2048 Implementation Plan

## Overview

We will refactor the FastAPI proxy so port 2048 can multiplex a round-robin pool of Camoufox browsers, each backed by a distinct cookie storage state. The plan introduces a session manager that tracks per-browser availability, rewires request dispatch to select an idle browser before work begins, and extends the launcher so a cookie directory seeds multiple Camoufox instances automatically.

## Current State Analysis

The current code assumes exactly one Playwright page shared across the entire process.

### Key Discoveries:
- `server.py:95` exports singleton globals (`browser_instance`, `page_instance`, `request_queue`, etc.) that the rest of the application imports directly.
- `api_utils/app.py:139` reads a single `CAMOUFOX_WS_ENDPOINT`, connects to it, and caches the resulting `browser_instance`/`page_instance` on the `server` module.
- `api_utils/queue_worker.py:19` consumes a single global queue and serializes all processing through `processing_lock`, always referencing `server.page_instance`.
- `launch_camoufox.py:1044` ultimately exports only one websocket endpoint and one `ACTIVE_AUTH_JSON_PATH`, so startup never provisions more than one browser.
- `api_utils/dependencies.py:19` et al. provide FastAPI dependencies by re-importing the same module-level globals, reinforcing the one-browser assumption.

## Desired End State

Port 2048 remains the single public entrypoint, but internally the proxy maintains an N-sized pool of browsers/pages initialized from a directory of cookie JSON files. Every `/v1/chat/completions` request is dispatched to whichever browser is both ready and least recently used, with the manager guaranteeing at most one in-flight request per browser. If a browser crashes, the manager isolates the failure, marks it unhealthy, and the proxy continues serving traffic with the remaining pool. Automated tests and manual smoke checks confirm the routing logic, failover behavior, and compatibility with OpenAI-style clients.

## What We're NOT Doing

- Changing the external API surface (remains OpenAI-compatible).
- Implementing automatic recovery of corrupted cookie files; we will fail fast and surface diagnostics.
- Allowing concurrent requests per browser; we deliberately keep strict single-flight semantics.
- Introducing cross-process supervisors—everything stays in-process for this iteration.

## Implementation Approach

1. Introduce a `BrowserSession` abstraction and a `MultiBrowserManager` that manages slots, readiness, and round-robin assignment.
2. Refactor request ingestion/processing to select a session up-front, pass it through queueing/processing, and release it on completion.
3. Extend startup to load multiple storage states, launch/attach to multiple Camoufox endpoints, and feed them into the manager, all driven by a CLI-provided cookie directory.
4. Bolster observability and testing so regressions are caught early (unit, integration, manual verification, and fault-injection drills).

## Phase 1: Session Manager Foundations

### Overview
Create the data structures that represent individual browser sessions and the manager responsible for loading cookies, dialing websocket endpoints, and providing round-robin assignment with health tracking.

### Changes Required:

#### 1. Session Manager Module
**File**: `api_utils/session_manager.py` (new)  
**Changes**: Define rich data types that capture configuration and runtime lifecycle for each browser, plus the manager responsible for coordinating them.

```python
class SessionConfig(NamedTuple):
    id: str                    # stable identifier derived from cookie filename or CLI override
    cookie_path: Path          # absolute path to the storage-state JSON file
    ws_url: str                # websocket endpoint for the Camoufox instance

class SessionState(Enum):
    IDLE = "idle"
    BUSY = "busy"
    UNHEALTHY = "unhealthy"

class ManagedSession:
    """
    Runtime handle returned by lease().
    Holds references to the Playwright browser, context, and page objects,
    plus bookkeeping to ensure release() is idempotent and thread-safe.
    """
    id: str
    browser: AsyncBrowser
    page: AsyncPage
    lease_token: uuid.UUID
    state: SessionState

class MultiBrowserManager:
    async def initialize_sessions(self, configs: Sequence[SessionConfig]) -> None:
        ...

    async def lease(self, req_id: str, timeout: float = 5.0) -> ManagedSession:
        """
        Blocks until an IDLE session is available, then marks it BUSY,
        returning a ManagedSession bound to the caller.
        """
        ...

    async def release(self, session: ManagedSession, *, mark_unhealthy: bool = False) -> None:
        """
        Returns the session to the pool. When mark_unhealthy=True, transition the slot
        to UNHEALTHY and optionally trigger a reinitialization callback.
        """
        ...
```

#### 2. Global Registration Hook
**File**: `server.py`  
**Changes**: Replace singleton browser/page globals with a single `multi_browser_manager` instance and lightweight helpers for metrics and logging. Preserve compatibility for legacy imports by providing shims during the transition.

```python
# server.py
multi_browser_manager: Optional[MultiBrowserManager] = None
single_session_deprecated: Optional[BrowserSession] = None  # TODO: remove after migration
```

#### 3. Settings & Schema Extension
**File**: `config/settings.py`  
**Changes**: Add configuration accessors for the cookie directory and desired pool size; validate presence and readability of the directory.

```python
BROWSER_PROFILE_DIR = get_environment_variable("BROWSER_PROFILE_DIR", "")
MAX_BROWSER_POOL_SIZE = get_int_env("MAX_BROWSER_POOL_SIZE", 4)
```

### Success Criteria:

#### Automated Verification:
- [x] Session manager unit tests pass: `poetry run pytest tests/session_manager`
- [x] Static analysis acknowledges new exports: `poetry run pyright`

#### Manual Verification:
- [ ] Launch proxy with two mocked session configs and confirm round-robin lease/release via logging.
- [ ] Simulate a failed session (mark_unhealthy) and ensure subsequent leases skip it.

---

## Phase 2: Request Dispatch & Worker Refactor

### Overview
Rewire FastAPI dependencies, queue workers, and processing code so each request selects an available session before entering the worker, guaranteeing exclusive use of the underlying page.

### Changes Required:

#### 1. Dependency Injection Updates
**File**: `api_utils/dependencies.py`  
**Changes**: Provide a new dependency `get_session_manager()` and, temporarily, compatibility helpers that raise informative errors if deprecated singletons are used after migration.

```python
def get_session_manager() -> MultiBrowserManager:
    from server import multi_browser_manager
    if not multi_browser_manager:
        raise RuntimeError("Multi-browser manager is not initialized")
    return multi_browser_manager
```

#### 2. Router & Queue Coordination
**File**: `api_utils/routers/chat.py`  
**Changes**: Lease a session before enqueuing, include the session id in the queue item, and ensure cancellation paths release the lease.

```python
session = await session_manager.lease(req_id)
await request_queue.put({...,"session": session,...})
```

#### 3. Worker & Request Context Changes
**File**: `api_utils/queue_worker.py` / `api_utils/context_init.py` / `api_utils/request_processor.py`  
**Changes**: Accept the leased session, operate on its `page` and locks, enforce single-flight by marking the session busy before processing, and release (or quarantine) on completion/error.

```python
async def queue_worker():
    ...
    session = item["session"]
    try:
        context = await initialize_request_context(req_id, request, session)
        ...
    finally:
        await session_manager.release(session, mark_unhealthy=needs_reset)
```

#### 4. Legacy Call Sites Cleanup
Remove direct imports of `server.page_instance` throughout `browser_utils` and `api_utils`, replacing them with session-scoped objects passed via context.

### Success Criteria:

#### Automated Verification:
- [x] Updated worker tests cover lease/release semantics: `poetry run pytest tests/queue`
- [x] Type checker validates new signatures: `poetry run pyright`
- [ ] Linting (if configured) passes: `poetry run ruff check`

#### Manual Verification:
- [ ] Run the proxy with three browsers and confirm logs show deterministic round-robin rotation.
- [ ] Fire concurrent requests that exceed the pool size and confirm they queue until a session becomes available (no concurrent reuse of the same browser).
- [ ] Kill a browser mid-request and ensure the session is marked unhealthy, with the client receiving a clear error.

---

## Phase 3: Startup & CLI Integration

### Overview
Allow operators to supply a cookie directory, spin up (or attach to) a matching number of Camoufox instances, and hand their websocket endpoints to the session manager during app startup.

### Changes Required:

#### 1. CLI Arguments & Validation
**File**: `launch_camoufox.py`  
**Changes**: Add `--auth-directory` (or similar) to accept a folder containing storage-state JSON files. Validate the directory, derive the desired pool size, and iterate when launching Camoufox.

```python
parser.add_argument(
    "--auth-directory",
    type=Path,
    default=Path(os.environ.get("BROWSER_PROFILE_DIR", ACTIVE_AUTH_DIR)),
    help="Directory containing one or more cookie storage-state JSON files."
)
```

#### 2. Multi-Endpoint Export
**File**: `launch_camoufox.py`  
**Changes**: Launch/attach multiple Camoufox instances, capture their websocket endpoints, and emit a serialized manifest via an environment variable (`CAMOUFOX_WS_ENDPOINTS`) consumed by the FastAPI app.

```python
os.environ["CAMOUFOX_WS_ENDPOINTS"] = json.dumps([
    {"cookie_path": str(path), "ws": endpoint}
    for path, endpoint in collected_endpoints
])
```

#### 3. App Lifespan Wiring
**File**: `api_utils/app.py`  
**Changes**: Parse the manifest during startup, call `multi_browser_manager.initialize_sessions(...)`, and remove the legacy singleton setup. Ensure shutdown drains all sessions cleanly. Provide helpers to decode environment variables into typed configs.

```python
@classmethod
def SessionConfig.from_env(cls, env_value: str) -> list["SessionConfig"]:
    """
    Accepts a JSON string such as:
    [
      {"id": "profile-1", "cookie_path": "/abs/a.json", "ws": "ws://..."},
      {"cookie_path": "/abs/b.json", "ws": "ws://..."}  # id inferred from filename
    ]
    """
    raw = json.loads(env_value or "[]")
    configs: list[SessionConfig] = []
    for index, entry in enumerate(raw):
        configs.append(
            SessionConfig(
                id=entry.get("id") or Path(entry["cookie_path"]).stem,
                cookie_path=Path(entry["cookie_path"]),
                ws_url=entry["ws"],
            )
        )
    return configs

def get_environment_variable(key: str, default: str | None = None) -> str | None:
    """Return the raw environment string or the provided default if unset."""
    return os.environ.get(key, default)

configs = SessionConfig.from_env(
    get_environment_variable("CAMOUFOX_WS_ENDPOINTS", "[]") or "[]"
)
await server.multi_browser_manager.initialize_sessions(configs)
```

#### 4. Legacy Environment Flags Migration
Document the deprecation of `ACTIVE_AUTH_JSON_PATH` in favor of the new directory-based configuration, retaining backward compatibility with a best-effort single-session fallback.

### Success Criteria:

#### Automated Verification:
- [ ] CLI smoke tests cover manifest generation: `poetry run pytest tests/cli`
- [ ] Docker/supervisor scripts still build: `poetry run task lint-docker` (placeholder, replace with actual command)

#### Manual Verification:
- [ ] Invoke `launch_camoufox.py --auth-directory auth_profiles/active --max-browsers 2` and observe two endpoints exported.
- [ ] Start the FastAPI app directly (`LAUNCH_MODE=direct_debug_no_browser`) with a handcrafted manifest to verify attach-only workflows.
- [ ] Run a full smoke test by sending 10 chat completion requests and confirming responses rotate through browser IDs as expected.

---

## Phase 4: Observability & Hardening

### Overview
Add metrics, logging, and fault-injection hooks to surface pool health, and write docs/test cases covering operational procedures.

### Changes Required:

#### 1. Instrumentation
**File**: `logging_utils` / `server.py`  
**Changes**: Emit structured logs and, if available, Prometheus counters for leases, releases, and unhealthy transitions.

```python
logger.info("session.leased", extra={"session_id": session.id, "req_id": req_id})
```

#### 2. Documentation
**File**: `docs/architecture-guide.md` / `docs/development-guide.md`  
**Changes**: Document the session manager lifecycle, new CLI flags, and operational runbooks for replacing cookie files or draining sessions.

#### 3. Fault-Injection Tests
Add integration tests that simulate session failure (e.g., closing a page mid-request) to verify quarantine logic.

### Success Criteria:

#### Automated Verification:
- [ ] Integration suite covering failure scenarios passes: `poetry run pytest tests/integration`
- [ ] Documentation linting (if applicable): `poetry run mkdocs build`

#### Manual Verification:
- [ ] Observe metrics/logs in a live run to ensure session IDs and rotation are visible.
- [ ] Manually kill one Camoufox process and confirm the manager marks it unhealthy and continues with the remaining pool.

---

## Testing Strategy

### Unit Tests:
- Cover session allocation, round-robin ordering, and unhealthy transitions.
- Validate queue worker behavior when sessions are busy or marked unhealthy.

### Integration Tests:
- Launch multiple mock Playwright endpoints and confirm request routing and failover.
- Simulate a browser crash and ensure the manager recovers gracefully.

### Manual Testing Steps:
1. Launch with three cookie files and send sequential requests, verifying round-robin distribution via logs.
2. Fire concurrent requests exceeding pool size to ensure excess requests wait instead of overloading a browser.
3. Revoke/rename one cookie file before restart to confirm startup validation halts with a clear error.

## Performance Considerations

- Ensure session selection uses O(1) lock-free data structures wherever possible; avoid global locking that would serialize all requests.
- Pre-initialize Playwright contexts to reduce per-request overhead; reuse network interceptors per session.
- Add configurable lease timeouts to prevent hung sessions from starving the pool.

## Migration Notes

- Provide backward compatibility by recognizing legacy `ACTIVE_AUTH_JSON_PATH`; if set alongside the new directory manifest, issue a warning and default to the new behavior.
- Communicate the change via release notes so operators know to supply directories instead of single files.

## References

- Research summary: `docs/research/multi-browser.md`
- Current singleton setup: `server.py:95`
- FastAPI lifespan initialization flow: `api_utils/app.py:139`
- Queue worker serial processing: `api_utils/queue_worker.py:19`
- Launcher environment export: `launch_camoufox.py:1044`
