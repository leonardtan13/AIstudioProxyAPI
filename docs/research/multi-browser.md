---
date: 2025-10-21T11:23:16-0700
researcher: Codex
git_commit: 7138ee40a36083dccb5730c8de39699080534dac
branch: main
repository: AIstudioProxyAPI
topic: "Multi-browser entrypoint and cookie startup flow"
tags: [research, entrypoints, browser, auth, cookies]
status: complete
last_updated: 2025-10-21
last_updated_by: Codex
---
# Research: Multi-browser entrypoint and cookie startup flow
**Date**: 2025-10-21T11:23:16-0700
**Researcher**: Codex
**Git Commit**: 7138ee40a36083dccb5730c8de39699080534dac
**Branch**: main
**Repository**: AIstudioProxyAPI

## Research Question
The current codebase launches a single browser and proxies port 2048 requests to it. Investigate the entrypoints and startup flow so we can imagine supporting multiple browser instances, each with its own cookie profile, while keeping a single port 2048 entrypoint backed by several browsers.

## Summary
The running system is orchestrated by `launch_camoufox.py`, which selects a single auth-state JSON from `auth_profiles/active`, starts a Camoufox browser, and launches the FastAPI app on port 2048. `server.py` holds singleton globals for the browser, page, queues, and readiness flags, and `api_utils/app.py`’s lifespan hook connects to the single Playwright websocket endpoint (`CAMOUFOX_WS_ENDPOINT`) and populates those globals. Request handling is centralized through `/v1/chat/completions`, which enqueues work for a global worker operating against the lone `page_instance`. All dependencies resolve the same process-wide objects via `api_utils/dependencies`. Cookie management hinges on `ACTIVE_AUTH_JSON_PATH`, which points to exactly one storage-state file at startup; the browser initializer loads that file into a single context. No structures currently exist for managing multiple contexts, endpoints, or cookie directories simultaneously.

## Detailed Findings

### HTTP Entrypoint and FastAPI App
- `server.py:95` defines global singletons (`browser_instance`, `page_instance`, readiness flags, locks, and caches) that every module imports for browser interactions.
- `server.py:128` instantiates the FastAPI app through `create_app` and serves it with Uvicorn on port 2048 (default) when invoked directly.
- `api_utils/app.py:200` registers the lifespan context that prepares logging, initializes globals, starts the stream proxy, connects to the browser, and schedules the request worker before requests are served.

### Application Startup & Browser Session Lifecycle
- `api_utils/app.py:139` starts Playwright, connects to the websocket endpoint supplied in `CAMOUFOX_WS_ENDPOINT`, and stores the resulting browser handle in the `server` module.
- `api_utils/app.py:160` calls `_initialize_page_logic`, retaining the returned `page_instance` and readiness flag; success triggers `_handle_initial_model_state_and_storage` and `enable_temporary_chat_mode`.
- `api_utils/app.py:171` defines `_shutdown_resources`, which tears down the queue worker, page, browser, and Playwright session, showing the lifecycle state transitions the single session follows.

### Browser Initialization and Auth Storage States
- `browser_utils/initialization.py:269` inspects `LAUNCH_MODE` to decide how to locate the cookie storage state and requires `ACTIVE_AUTH_JSON_PATH` for headless modes.
- `browser_utils/initialization.py:306` constructs a single browser context, optionally applying `storage_state` from the chosen auth JSON and attaching proxy settings from `server.PLAYWRIGHT_PROXY_SETTINGS`.
- `browser_utils/initialization.py:331` searches the context for an existing AI Studio page or opens one page (`found_page`), establishing the single Playwright `AsyncPage` that the rest of the system uses.

### Request Routing Through Worker
- `api_utils/routers/chat.py:16` accepts `/v1/chat/completions`, enqueues requests into `server.request_queue`, and waits on a future filled by the worker.
- `api_utils/queue_worker.py:19` processes the queue sequentially, protecting the single page with `processing_lock` and handing each request to `_process_request_refactored`.
- `api_utils/context_init.py:6` constructs a request context by reading the singleton objects (`page_instance`, locks, cached parameters) from `server`, illustrating that every request targets the same page object.

### Launch Orchestration and Auth Profile Selection
- `launch_camoufox.py:45` loads `.env`, locates directories, and records default ports (FastAPI default 2048, Camoufox default 9222).
- `launch_camoufox.py:808` resolves the active auth JSON either from CLI, `auth_profiles/active`, or by prompting the user in debug mode; only one path is ultimately assigned to `effective_active_auth_json_path`.
- `launch_camoufox.py:1044` exports environment variables (`CAMOUFOX_WS_ENDPOINT`, `LAUNCH_MODE`, `ACTIVE_AUTH_JSON_PATH`, etc.) for `server.py`, linking the chosen cookie file and browser endpoint to the single FastAPI process.

### Configuration & Cookie Directories
- `config/settings.py:22` defines `AUTH_PROFILES_DIR` with `active/` and `saved/` subdirectories; these are the only storage locations referenced by launcher and browser initialization.
- `auth_profiles/active` currently contains a single JSON (`emblemtravel.json` at the time of inspection), matching the assumption that exactly one profile is active.
- `docs/authentication-setup.md` (narrative) reiterates the workflow: create a cookie file via debug mode, place exactly one file under `auth_profiles/active`, and run headless mode referencing that single file.

### GUI Launcher
- `gui_launcher.py:24` loads `.env`, points to `launch_camoufox.py`, and provides an alternative method to start the same single browser+server combination by wrapping the launcher script in a GUI.

## Code References
- `server.py:95` — defines global browser/page state and readiness flags relied on by the entire service.
- `api_utils/app.py:139` — connects to the Camoufox websocket, initializes Playwright, and caches the single page.
- `browser_utils/initialization.py:269` — loads cookie storage state from `ACTIVE_AUTH_JSON_PATH` when creating the lone browser context.
- `api_utils/queue_worker.py:19` — dequeues requests and serializes access to the sole page via locks.
- `launch_camoufox.py:808` — picks one auth JSON file and exports it as `ACTIVE_AUTH_JSON_PATH` before starting FastAPI.

## Architecture Documentation
- Singletons in `server.py` centralize browser connectivity, page reference, queue, and locks; every dependency module imports from `server`, ensuring all requests target the same Playwright page.
- The lifecycle is driven by `api_utils/app.py`’s lifespan manager: start stream proxy → start Playwright → connect to Camoufox websocket → initialize one page → start queue worker. Shutdown reverses this order.
- Request handling is queue-based: `/v1/chat/completions` enqueues jobs, `queue_worker` serializes them, and `_process_request_refactored` manipulates the single page through `PageController`.
- Cookie and authentication management relies on `ACTIVE_AUTH_JSON_PATH`, chosen once at startup by `launch_camoufox.py`, and stored under `auth_profiles/active/`.
- Port 2048 is the default HTTP entrypoint, configured by `launch_camoufox.py` and respected by `server.py` if run independently; Camoufox exposes a websocket (default port 9222) consumed by Playwright.

## Historical Context (from thoughts/)
- No `thoughts/` directory is present in the repository, so there are no historical notes to reference.

## Related Research
- None found in the repository.

## Open Questions
- How should multiple Playwright websocket endpoints be represented given the current single-value `CAMOUFOX_WS_ENDPOINT` environment variable?
- What mechanism will assign incoming API requests to different cookie profiles when `ACTIVE_AUTH_JSON_PATH` currently supports only one storage-state file?
- How will queueing and locking evolve if multiple `page_instance` objects must be managed concurrently?

Key points:
- Startup revolves around `launch_camoufox.py`, which selects one cookie file, launches Camoufox, and starts FastAPI on port 2048 with `CAMOUFOX_WS_ENDPOINT` and `ACTIVE_AUTH_JSON_PATH` set for a single session.
- `server.py` and `api_utils/app.py` maintain singleton state for Playwright; every request handled by `/v1/chat/completions` ultimately manipulates the same `page_instance` retrieved from the shared globals.
- Cookie state is loaded once during `_initialize_page_logic`, sourced from `auth_profiles/active`, reinforcing the current single-browser assumption.

Let me know if you’d like follow-up research on any specific module or flow.
