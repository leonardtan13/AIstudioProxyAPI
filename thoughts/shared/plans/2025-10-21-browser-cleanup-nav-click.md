# Browser Cleanup Navigation Click Implementation Plan

## Overview

Enhance the post-request browser cleanup so it primarily resets the chat by clicking the global “Chat” navigation link, falling back to the existing dialog-driven clear flow when that link is unavailable. This keeps the page in a ready state and still re-enables Temporary Chat mode.

## Current State Analysis

Cleanup runs in `api_utils/queue_worker.py:322-336`, constructing a `PageController` and calling `clear_chat_history` after each request. `clear_chat_history` (`browser_utils/page_controller.py:535-706`) handles Run button halting, clicks the “New chat” button, confirms the discard dialog, dismisses overlays, verifies chat removal, and finally calls `enable_temporary_chat_mode` (`browser_utils/initialization.py:650-678`). Selectors for this flow reside in `config/selectors.py`. No logic currently targets the top navigation link `a.playground-link[href="/prompts/new_chat"]`.

## Desired End State

After each request, the worker should:
- Try clicking the navigation “Chat” link to reset the conversation.
- Log a clear English warning if that click fails, then fall back to the existing dialog-based clear sequence.
- Ensure Temporary Chat mode is re-enabled regardless of the path taken.

### Key Discoveries:
- Cleanup is centralized through `PageController.clear_chat_history` called from the worker (`api_utils/queue_worker.py:322-336`).
- The dialog sequence lives in `browser_utils/page_controller.py:535-706`; selectors live in `config/selectors.py:7-24`.
- Temporary Chat mode is re-applied inside cleanup by `enable_temporary_chat_mode` (`browser_utils/initialization.py:650-678`).

## What We're NOT Doing

- Changing the queue worker’s cleanup trigger or concurrency model.
- Modifying non-browser resource cleanup in `api_utils/request_processor.py:411-441`.
- Altering how Temporary Chat mode works beyond keeping the existing call.

## Implementation Approach

Augment `PageController.clear_chat_history` to include a navigation-link reset path before the dialog flow, with structured logging and fallback.

## Phase 1: Selector & Nav Reset Support

### Overview
Add a selector for the nav “Chat” link and implement a helper to click it safely with logging.

### Changes Required:

#### 1. Selectors
**File**: `config/selectors.py`
**Changes**: Add `NAV_CHAT_LINK_SELECTOR = 'a.playground-link[href="/prompts/new_chat"]'` near other button selectors.

#### 2. Page Controller Helper
**File**: `browser_utils/page_controller.py`
**Changes**: 
- Import the new selector.
- Add an async method `_navigate_via_chat_link` that:
  - Locates the nav link with the selector.
  - Waits for visibility and clicks it with small timeout.
  - Waits briefly for navigation stabilization (e.g., new URL or content reset).
  - Logs success.
  - Catches exceptions, logs a warn-level message in English including the reason, and returns `False` instead of raising.

```python
async def _navigate_via_chat_link(self, check_client_disconnected: Callable) -> bool:
    # Try clicking navigation Chat link
    # return True on success, False if not available or any failure (with warning logged)
```

- [x] Phase 1 implementation (selector + helper) completed.

### Success Criteria:

#### Automated Verification:
- [ ] Formatting/lint passes: `make lint` (target missing in repo as of run).
- [ ] Unit tests pass: `make test` (target missing in repo as of run).

#### Manual Verification:
- [ ] In a dev environment, trigger cleanup and confirm the nav link click occurs (observe logs).
- [ ] Confirm failure logs appear when the link is intentionally hidden/disabled.

---

## Phase 2: Integrate Nav-First Cleanup with Fallback

### Overview
Update `clear_chat_history` to attempt the nav click first, falling back to existing dialog sequence. Ensure Temporary Chat re-enabling always runs.

### Changes Required:

#### 1. `clear_chat_history` Flow
**File**: `browser_utils/page_controller.py`
**Changes**: 
- At the start (after disconnect check), invoke `_navigate_via_chat_link`; if it returns `True`, skip the Run button/dialg sequence but still call `enable_temporary_chat_mode`.
- If it returns `False`, proceed with current Run button logic and `_execute_chat_clear` as fallback.
- Ensure fallback path logs the warning from helper.
- Wrap both branches so `enable_temporary_chat_mode` runs even when fallback fails, with existing exception handling.

```python
nav_reset_successful = await self._navigate_via_chat_link(check_client_disconnected)
if not nav_reset_successful:
    # existing Run button + dialog logic
await enable_temporary_chat_mode(self.page)
```

- Update logging messages to clarify which path executed (e.g., “Chat navigation link click succeeded; skipping dialog clear”).

- [x] Phase 2 implementation (nav-first cleanup) completed.

### Success Criteria:

#### Automated Verification:
- [ ] `make lint`
- [ ] `make test`

#### Manual Verification:
- [ ] Confirm nav-first cleanup path works and shortens the reset cycle.
- [ ] Force nav helper failure (e.g., remove link in dev tools) and verify fallback executes with English warning.
- [ ] Confirm Temporary Chat toggle remains activated after both paths.

---

## Testing Strategy

### Unit Tests:
- No direct unit tests (Playwright interactions are hard to unit-test here), but consider adding async mocks around `PageController` methods if infrastructure exists.

### Integration Tests:
- If automation exists, add an end-to-end Playwright test covering nav click success/failure; otherwise rely on manual verification.

### Manual Testing Steps:
1. Run the proxy, complete a request, observe that cleanup logs show nav click and immediate reset.
2. Temporarily alter DOM to hide nav link; repeat request to see fallback dialog path and warning.
3. Confirm Temporary Chat toggle indicates active state post-cleanup in both cases.

## Performance Considerations

- Nav click should be faster than dialog flow; ensure we don’t introduce long awaits. Keep timeouts short.

## Migration Notes

- No database or persistent state migrations required.

## References

- Original research: `docs/research/cleanup.md`
- Cleanup call site: `api_utils/queue_worker.py:322-336`
- Current dialog flow: `browser_utils/page_controller.py:535-706`
- Temporary chat toggle: `browser_utils/initialization.py:650-678`
