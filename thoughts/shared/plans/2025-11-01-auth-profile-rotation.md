# Auth Profile Rotation Pool Implementation Plan

## Overview

Keep the coordinator running exactly four Camoufox children at any given time—even when more auth profiles are available—by rotating profiles through a fixed pool. When a child becomes unhealthy (timeout/≥500 response) or never reaches readiness, recycle its profile and launch the next staged profile on the same ports without altering deployment knobs.

## Current State Analysis

The coordinator hydrates every `active/*.json` profile at startup and immediately spawns one child per profile, so concurrency scales linearly with the profile count (`coordinator/main.py:313`). Each child is tied to a unique port triple for its lifetime, and the registry only toggles ready/unhealthy flags without respawning processes (`coordinator/main.py:330-348`, `coordinator/manager.py:24-109`). Health polling bails out after 30 s, demotes the child, and leaves it for manual recovery (`coordinator/health.py:15-52`). Request routing demotes children on timeout or ≥500, but the processes stay alive and continue consuming resources (`coordinator/api.py:113-139`, `coordinator/routing.py:24-68`). There is no notion of an inactive profile queue or pool size cap.

## Desired End State

The coordinator:
- Hydrates all profiles but only boots four children initially, keeping the remainder queued for reuse.
- Applies a 60 s readiness timeout; failure to become ready triggers the same recycle path as runtime timeouts/≥500 responses.
- Maintains two collections: four active slots bound to stable port triples, and a deque of inactive profiles that feeds replacements.
- Restarts unhealthy slots automatically on the next profile in order, re-queuing the evicted profile at the tail so profiles are reused fairly.
- Keeps `/ready` truthful by removing evicted processes immediately and promotes replacements once healthy, without surfacing new deployment/environment variables.

Verification: unit tests simulate slot eviction and rotation, integration tests exercise readiness timeout, and manual checks confirm a four-child steady state even when >4 profiles exist.

### Key Discoveries:
- `coordinator/main.py:313-383` launches one child per profile and never respawns them after failure.
- `coordinator/manager.py:24-109` handles readiness bookkeeping but not process lifecycle management.
- `coordinator/health.py:15-52` hard-codes a 30 s readiness timeout and only watches `/health`.
- `coordinator/routing.py:24-68` demotes children on timeout/≥500 but leaves the process running.
- `api_utils/routers/health.py:19-69` defines child readiness responses; coordinator polling still expects `"OK"`.

## What We're NOT Doing

- No changes to deployment configuration, environment variables, or orchestration manifests.
- No dynamic pool sizing or smart profile selection heuristics beyond simple round-robin reuse.
- No alterations to Camoufox internals or auth profile schema.
- No long-term failure analytics or metrics export beyond improved logging for rotation events.

## Implementation Approach

Introduce a slot manager that wraps child lifecycle tasks (launch, recycle, shutdown) while leaving request routing untouched. Startup seeds four slots and queues the rest. When a slot fails readiness or trips eviction rules, terminate the process, enqueue its profile, pull the next queued profile (wrapping as needed), and relaunch on the same ports. Extend health monitoring and routing to call the slot manager when a child is marked unhealthy. Ensure graceful shutdown drains both active and queued state.

## Phase 1: Boot-Time Slot Management

### Overview
Hydrate all profiles, initialize a four-slot pool, and queue any extra profiles for rotation. Raise startup readiness timeout to 60 s.

### Changes Required:

#### 1. Coordinator Slot Structures
**File**: `coordinator/types.py`  
**Changes**: Add lightweight data classes for `ProfileSlot` (port triple + optional child) and `ProfileQueue` (deque wrapper) to clarify ownership of profiles vs. live processes.

```python
@dataclass
class ProfileSlot:
    ports: ChildPorts
    profile: AuthProfile | None = None
    process: ChildProcess | None = None
```

#### 2. Startup Pool Initialization
**File**: `coordinator/main.py`  
**Changes**:
- After hydration, split the discovered profiles into the first four (seed slots) and a deque for the remainder.
- Construct `ProfileSlot` instances bound to the existing port assignments and populate active slots with the initial profiles.
- Update `parse_args` defaults to keep `--port-step` etc. unchanged; no new CLI flags.
- Increase the readiness wait from 30 s to 60 s when calling `wait_for_ready`.
- Ensure environment variable wiring (`AUTH_KEY_FILE_PATH`) remains unchanged.

```python
active_profiles = discovered[:4]
queued_profiles = deque(discovered[4:])
slots = [ProfileSlot(ports=p) for p in port_assignments[:4]]
```

#### 3. Hydration Accounting
**File**: `coordinator/profiles.py`  
**Changes**: Return ordered profile list (maintain existing deterministic ordering) so the coordinator can rely on hydration order for rotation fairness; add a log line summarizing how many will be queued vs. active.

### Success Criteria:

#### Automated Verification:
- [ ] Coordinator unit tests covering `ProfileSlot` initialization: `poetry run pytest tests/coordinator/test_main.py::test_slot_bootstrap` (new).
- [ ] Existing profile hydration tests continue to pass: `poetry run pytest tests/coordinator/test_profiles.py`.

#### Manual Verification:
- [ ] Launch coordinator with >4 local profiles; confirm logs report “Seeded 4 active profiles, queued X idle”.
- [ ] Verify only four child processes appear in `ps` despite additional JSON files.

---

## Phase 2: Runtime Rotation Logic

### Overview
Implement a slot manager that recycles profiles when a child exits or is evicted, keeping the active pool at four processes.

### Changes Required:

#### 1. SlotManager Component
**File**: `coordinator/manager.py`  
**Changes**: Introduce `SlotManager` (or extend `ChildRegistry`) to:
- Maintain active slots and the profile deque.
- Provide `allocate_slot(profile)` and `evict_child(child, reason)` APIs.
- Launch new children via existing `launch_child`, wiring logs and environment as today.
- Handle process termination with graceful shutdown before relaunch.

```python
class SlotManager:
    def __init__(..., launch_fn=launch_child):
        ...
    def evict_child(self, child, reason):
        self._terminate(child)
        self._queue.append(child.profile)
        self._activate_next(child.ports)
```

#### 2. Registry Integration
**File**: `coordinator/api.py` & `coordinator/manager.py`  
**Changes**:
- Inject `SlotManager` into `ChildRegistry` so `mark_unhealthy` can signal slot eviction.
- After replacing a child, clear its readiness state and let the registry monitor promote the replacement when healthy.
- Adjust `ChildRegistry.next_child` to skip slots without a live process.

#### 3. Graceful Shutdown
**File**: `coordinator/main.py`  
**Changes**: Update shutdown path to drain `SlotManager` slots (terminate running processes), then clear the profile deque to avoid lingering state; ensure existing `graceful_shutdown` helper reuses slot metadata instead of raw `ChildProcess` list.

### Success Criteria:

#### Automated Verification:
- [ ] Slot eviction unit test confirms a terminated process is replaced with the next queued profile: `poetry run pytest tests/coordinator/test_manager.py::test_slot_recycle` (new).
- [ ] API routing tests verify demotion triggers `SlotManager.evict_child`: `poetry run pytest tests/coordinator/test_routing.py::test_timeout_triggers_rotation`.

#### Manual Verification:
- [ ] Induce a child crash (kill PID) and confirm logs show recycle + relaunch with a new profile on the same ports.
- [ ] Watch `/ready`; it should report degraded until the replacement becomes healthy, then return to ready.

---

## Phase 3: Eviction Triggers & Monitoring

### Overview
Wire the rotation manager to all failure signals (request timeout/≥500, readiness timeout, process exit) and enhance observability.

### Changes Required:

#### 1. Health Wait Timeout
**File**: `coordinator/health.py`  
**Changes**: Increase default timeout to 60 s and return a sentinel to `SlotManager` when readiness fails, so the slot is recycled immediately.

```python
async def wait_for_ready(..., timeout: float = 60.0) -> bool:
    ...
```

#### 2. Routing Eviction Hooks
**File**: `coordinator/api.py`  
**Changes**: When catching `ChildRequestError` marked retryable, call `slot_manager.evict_child(child, str(exc))` before demoting so the process is torn down rather than left running. Ensure non-retryable errors still bubble as 502 without triggering rotation.

#### 3. Process Monitor
**File**: `coordinator/main.py`  
**Changes**: Update `_monitor_child_processes` to notify the slot manager when a child exits unexpectedly so the slot is re-seeded automatically, keeping the active count at four.

#### 4. Logging
**File**: `coordinator/manager.py`  
**Changes**: Add concise structured log messages (`LOGGER.info("Recycled profile %s onto ports %s", ...)`) to assist debugging rotation events.

### Success Criteria:

#### Automated Verification:
- [ ] New tests cover readiness timeout recycling: `poetry run pytest tests/coordinator/test_health.py::test_timeout_triggers_recycle` (new).
- [ ] Existing routing tests updated to assert `SlotManager` interactions pass: `poetry run pytest tests/coordinator/test_routing.py`.

#### Manual Verification:
- [ ] Simulate a timeout by pointing a child at an unreachable upstream; confirm the coordinator rotates the profile and logs the event.
- [ ] Observe that `/ready` flips to 503 during rotation and returns to 200 once the replacement readies.

---

## Phase 4: Testing & Operational Docs

### Overview
Backstop the new rotation behaviour with tests and operator notes explaining the four-slot pool.

### Changes Required:

#### 1. Test Suite Additions
**File**: `tests/coordinator/test_main.py` (new)  
**Changes**: Add fixtures and async tests for slot bootstrap, rotation, and shutdown flows; mock `launch_child` and `wait_for_ready` where needed.

#### 2. Docs Update
**File**: `docs/deployment-prod.md` (or existing coordinator doc)  
**Changes**: Document that only four children run simultaneously, extra profiles are rotated in on failure, and operators should monitor logs for `Recycling profile` events.

#### 3. README Link
**File**: `README.md`  
**Changes**: Add a brief note pointing to the updated deployment doc for profile rotation behaviour.

### Success Criteria:

#### Automated Verification:
- [ ] Full coordinator suite passes: `poetry run pytest tests/coordinator`.
- [ ] Static analysis/lint (if configured) passes: `poetry run ruff check coordinator`.

#### Manual Verification:
- [ ] Docs reviewed to ensure expectations around four-slot pool and rotation logs are clear.

---

## Testing Strategy

### Unit Tests:
- Verify slot initialization seeds exactly four active children and queues the rest.
- Confirm rotation replaces an unhealthy child with the next queued profile and recycles the evicted profile.
- Ensure readiness timeout path returns `False` after ~60 s and triggers rotation.

### Integration Tests:
- Spin up a fake child that never becomes ready to ensure the slot manager recycles it.
- Simulate a child returning 504 to verify the coordinator terminates and replaces it before handling subsequent requests.

### Manual Testing Steps:
1. Hydrate six profiles locally, start the coordinator, and run `ps`/`lsof` to confirm only four live children/port bindings.
2. Manually kill one child process; observe restart logs and `/ready` degradation/recovery.
3. Inject a failure by modifying one profile to point at an invalid upstream; send traffic until the coordinator rotates it out and in again.
4. Confirm shutdown stops all four children cleanly and empties the queue without zombie processes.

## Performance Considerations

- Only four children run concurrently, so CPU/RAM usage stays bounded even if dozens of profiles exist; port allocation remains unchanged (3100/3200/9222 increments) per `coordinator/config.py:6-18`.
- Rotation relaunches reuse existing ports, avoiding churn in networking or firewall rules.
- Rehydration still downloads every profile up front; document that buckets with hundreds of profiles may add startup latency even though only four run simultaneously.

## Migration Notes

- No deployment changes required; existing Docker images and orchestrators continue to interact with the coordinator on port 2048.
- Operators should be aware that at most four profiles run simultaneously; additional profiles act as spares for automatic rotation.
- Monitoring should key on the new rotation log messages to detect flapping profiles; consider adding alerting later if instability is observed.

## References

- `coordinator/main.py:313-383` – Startup lifecycle, port assignment, and process launch.
- `coordinator/manager.py:24-109` – Ready/unhealthy bookkeeping to extend with slot management.
- `coordinator/health.py:15-52` – Readiness polling path to extend to 60 s.
- `coordinator/routing.py:24-68` – Retry logic triggering demotion on timeout/≥500.
- `api_utils/routers/health.py:19-69` – Child readiness contract the coordinator polls.
