---
date: 2025-10-29T11:11:12-07:00
researcher: codex
git_commit: 48b7dd99544cfdeb8ca690efc0a315e6351f06ff
branch: main
repository: AIstudioProxyAPI
topic: "Deployment impact of the multi-process coordinator"
tags: [research, deployment, docker, coordinator, aws, terraform]
status: complete
last_updated: 2025-10-29
last_updated_by: codex
---
# Research: Deployment impact of the multi-process coordinator
**Date**: 2025-10-29T11:11:12-07:00  
**Researcher**: codex  
**Git Commit**: 48b7dd99544cfdeb8ca690efc0a315e6351f06ff  
**Branch**: main  
**Repository**: AIstudioProxyAPI

## Research Question
i implemented this plan thoughts/shared/plans/2025-10-21-multi-process-coordinator.md. i want to understand how this will affect deployment. the end goal is that i still want to be able to deploy this as a docker container and then run the fastapi proxy server that will still route it to the multiple instances. think about, if we want to deploy this to aws using terraform, what do we need to know about the repo.

## Summary
The repository now contains a `coordinator` package that spawns one `launch_camoufox.py` process per auth profile, monitors health, and serves as the canonical FastAPI front-end on port 2048. Docker builds remain multi-stage images that install Playwright/Camoufox assets and launch a single `launch_camoufox.py` process through Supervisor; no container entrypoint change has been committed, so the existing image still boots the single-process proxy unless the command is overridden. The coordinator assumes it can reach every child on `127.0.0.1` with ports allocated from 3100/3200/9222 upward, writes per-profile logs under `logs/coordinator`, and reads auth profiles from `auth_profiles/active`. For AWS/Terraform usage, deployment inputs include the Python/Poetry dependency set, the requirement for mounted auth profile JSON files, the need for local persistent storage for logs/Playwright caches, outbound network access during image build (`camoufox fetch`, `playwright install firefox`), and exposure of only the coordinator/API ports (2048 and optionally 3120). Health checks still target `/health` on the public port, aligning with the coordinator’s readiness reporting.

## Detailed Findings
### Container Build & Runtime Process
- Description: `docker/Dockerfile:4-116` builds a Python 3.10 image, installs Poetry-managed dependencies, runs `camoufox fetch` and `python -m playwright install firefox`, sets environment defaults (including `SERVER_PORT=2048`), and finalizes with Supervisor as PID 1.
- Description: `supervisord.conf:1-20` defines a single program that still executes `python launch_camoufox.py` with headless mode, passing environment-derived ports (FastAPI/stream) and proxy settings. Deployment artifacts therefore continue to start exactly one proxy unless overridden.
- Description: `docker/docker-compose.yml:1-56` maps host ports 2048 and 3120 into the container, mounts `auth_profiles`, optional logs/certs/scripts, loads `.env`, and defines a health check hitting `/health` on the FastAPI port—compatible with both single-process and coordinated modes.

### Coordinator Orchestration Model
- Description: `coordinator/config.py:6-26` exposes defaults for the coordinator listener (0.0.0.0:2048) and base child ports (API 3100, stream 3200, Camoufox 9222) with a port increment of 1.
- Description: `coordinator/main.py:34-319` parses CLI flags (`--profiles`, `--base-*`, `--coordinator-*`, `--port-step`, `--log-dir`, `--no-headless`), enumerates `*.json` profiles, assigns sequential port triples, launches one child per profile, waits for readiness, and runs Uvicorn serving the coordinator FastAPI app.
- Description: `coordinator/launcher.py:48-124` forks `launch_camoufox.py` as a subprocess with the computed ports and `--active-auth-json` pointing to the profile; stdout/stderr are streamed into rotating files under `logs/coordinator`.
- Description: `coordinator/health.py:15-54` polls each child’s `/health` endpoint on its private API port, marking children ready or unhealthy within the registry.
- Description: `coordinator/api.py:40-136` mounts `/v1/chat/completions`, `/v1/models`, `/v1/cancel/{req_id}`, and `/health` on the coordinator itself. It rejects streaming requests, rotates through ready children, demotes flaky ones, and relays JSON responses transparently.
- Description: `coordinator/routing.py:24-104` uses `httpx.AsyncClient` pointing to `http://127.0.0.1:{child_port}` for forwarding and cancellation fan-out, assuming all child ports are accessible on localhost.

### Ports, Networking, and Health Checks
- Description: Children listen on per-profile FastAPI ports starting at 3100 and Camoufox debugging ports starting at 9222; these remain internal to the host/container because coordinator traffic uses `127.0.0.1`.
- Description: The coordinator itself binds to `DEFAULT_COORDINATOR_HOST=0.0.0.0` and `DEFAULT_COORDINATOR_PORT=2048` (`coordinator/config.py:6-8`), aligning with exposed Docker ports.
- Description: Streaming port settings are still provisioned (e.g., `STREAM_PORT=3120` in `docker/.env.docker:21-22`), and the compose file exposes 3120 even though coordinator responses reject `stream=true`. If external streaming is required, deployments need to keep the upstream port available for legacy clients.

### Configuration & Secrets Handling
- Description: Auth profiles are discovered from `auth_profiles/active` by default (`coordinator/main.py:34-114`), so containerized deployments must mount that directory with pre-provisioned JSON state files.
- Description: Environment control continues through `.env` files; `docker/.env.docker:8-150` documents port mappings, proxy settings, logging flags, resource limits, and script injection toggles.
- Description: Dependencies and runtime expectations are defined via Poetry (`pyproject.toml:9-28`), including Playwright, Camoufox, FastAPI, and `httpx` added for coordinator routing.
- Description: The README surfaces a coordinator launch command for manual environments: `poetry run python -m coordinator.main --profiles auth_profiles/active` (`README.md:382`), indicating the intended entrypoint when not using Supervisor.

### Testing & Documentation Footprint
- Description: Coordinator behavior is covered by tests under `tests/coordinator/`, verifying port assignment, registry rotation, routing retries, and cancellation broadcast logic (`tests/coordinator/test_config.py:1-32`, `tests/coordinator/test_manager.py:1-87`, `tests/coordinator/test_routing.py:1-306`).
- Description: The Docker documentation remains oriented around the single process workflow (`docker/README.md`, `docker/README-Docker.md`), so deployment teams should note that official docs have not yet been updated to mention the coordinator entrypoint.

## Code References
- `docker/Dockerfile:4-116` - Multi-stage image installing dependencies, Camoufox assets, and Supervisor entrypoint.
- `supervisord.conf:1-20` - Supervisor program still launching a single `launch_camoufox.py` process.
- `docker/docker-compose.yml:1-56` - Compose service exposing ports, mounting `auth_profiles`, and health checking `/health`.
- `docker/.env.docker:8-150` - Container environment variables covering ports, proxy settings, logging, and resource hints.
- `coordinator/config.py:6-26` - Default coordinator host/port and child port bases.
- `coordinator/main.py:34-319` - CLI wiring, profile discovery, port assignment, child launch, and Uvicorn startup.
- `coordinator/launcher.py:48-124` - Subprocess invocation of `launch_camoufox.py` per profile with local logging.
- `coordinator/health.py:15-54` - Readiness polling of child `/health` endpoints.
- `coordinator/api.py:40-136` - FastAPI router providing `/v1` endpoints and fan-out cancellation.
- `coordinator/routing.py:24-104` - Localhost forwarding to child processes using `httpx` with retry signalling.
- `pyproject.toml:9-28` - Poetry-managed runtime dependencies required in the container.
- `README.md:382` - Documented coordinator launch command for manual execution.
- `thoughts/shared/plans/2025-10-21-multi-process-coordinator.md:1-200` - Multi-process coordinator plan outlining scope and goals.

## Architecture Documentation
The runtime architecture now distinguishes between a top-level coordinator FastAPI service and delegated child proxies. The coordinator binds to 0.0.0.0:2048, providing the same public API surface (`/v1/chat/completions`, `/v1/models`, `/v1/cancel/{req_id}`, `/health`) that clients already expect. Each child process runs `launch_camoufox.py` with its own FastAPI server and Camoufox instance on incremented ports starting from 3100/3200/9222, remaining accessible only inside the container/host via `127.0.0.1`. The `ChildRegistry` maintains readiness state and performs round-robin selection, demoting unhealthy children and re-promoting them once health checks succeed. Log output from children is written to `logs/coordinator/{profile}.log`, complementing existing global logs. Existing Docker builds still bootstrap via Supervisor; switching to the coordinator requires overriding the command to run `python -m coordinator.main` (or introducing a new Supervisor program) while keeping volume mounts for `auth_profiles`, caches, and optional logs. Infrastructure automation must account for dependencies on GPU-less Playwright Firefox, Camoufox assets, and the need for outbound HTTP access during both image build and runtime.

## Historical Context (from thoughts/)
- `thoughts/shared/plans/2025-10-21-multi-process-coordinator.md` - Defines the intention to minimize in-process refactors by coordinating multiple `launch_camoufox.py` instances, describes port allocation strategy, endpoint surface, log expectations, and explicit rejection of streaming in the coordinator.

## Related Research
- _None_

## Open Questions
- The Supervisor configuration still targets `launch_camoufox.py`; confirming how deployment pipelines will switch containers into coordinator mode remains outstanding.
- Handling of `auth_profiles` in AWS/Terraform (e.g., EFS, secrets injection, or baked artifacts) is unspecified and may require environment-specific decisions.
- The public exposure of the legacy stream port (3120) is unchanged; determining whether clients will continue to rely on it or rely solely on coordinator responses needs clarification.
