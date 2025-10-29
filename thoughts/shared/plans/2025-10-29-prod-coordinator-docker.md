# Production-Ready Coordinator Container Implementation Plan

## Overview

Build a production-ready runtime for the multi-process coordinator that sources auth profiles from S3, exposes clear liveness/readiness signals, and runs inside a new `docker/Dockerfile.prod` image that boots the coordinator directly.

## Current State Analysis

The repository still assumes auth profiles and API keys live on disk under `auth_profiles/`; the coordinator image launches only a single `launch_camoufox.py` process via Supervisor, and health checks always return HTTP 200 even when every child is broken.

### Key Discoveries:
- Local-only profile discovery blocks remote storage: `coordinator/main.py:34-139` resolves `--profiles` to `Path("auth_profiles/active")` and errors when the directory is missing.
- Cookies must be available as files: `launch_camoufox.py:809-914` resolves the `--active-auth-json` path and exits if nothing is on disk.
- API keys are read from `auth_profiles/key.txt`: `api_utils/auth_utils.py:1-32` ensures the file exists locally before loading keys.
- Container entrypoint still spawns a single process: `docker/Dockerfile:64-116` copies the repo and `CMD` runs Supervisor with `launch_camoufox.py` (`supervisord.conf:8-19`), so the coordinator never starts in the published image.
- Coordinator readiness is JSON-only: `/health` always returns 200 with status text (`coordinator/api.py:40-123`), leaving no failing status for orchestrators.

## Desired End State

Running `docker build -f docker/Dockerfile.prod .` produces an image that:
- Fetches auth profiles and the API key list from S3 (or from local disk when explicitly requested).
- Launches the coordinator (bound to `0.0.0.0:2048`), which boots one child per profile and exposes dedicated `/live` and `/ready` endpoints reflecting true health.
- Documents all required environment variables (AWS credentials, S3 bucket/prefix, coordinator tuning) so operators can run the container without additional repository changes.

### Verification
- Unit and integration tests cover S3 profile hydration, backend selection fallbacks, and new health semantics.
- Manual smoke tests validate that the container starts, fetches profiles from S3, surfaces `/live` & `/ready`, and proxies traffic through the coordinator.

## What We're NOT Doing

- No Terraform or other infrastructure provisioning.
- No automatic write-back or rotation of auth profiles to S3; the service only consumes remote state.
- No multi-region deployment orchestration or autoscaling logic.
- No refactor of the underlying single-browser child logic beyond health endpoint adjustments.

## Implementation Approach

1. Add a profile-provider abstraction that can pull auth material from S3 before coordinator startup while keeping the existing local-directory path intact.
2. Replace the Supervisor-based runtime with a Python entrypoint script that runs the sync and starts the coordinator; clearly outline how env vars drive the behaviour so future engineers can reproduce the flow without this discussion.
3. Expose `/live` and `/ready` endpoints in both the coordinator and each child FastAPI app with status codes orchestrators can rely on.
4. Produce a production README detailing configuration variables, expected S3 layout, health checks, container start commands, and troubleshooting notes.

## Phase 1: Auth Profile Provider Layer

### Overview
Introduce a pluggable provider so the coordinator can hydrate profiles/API keys from S3 (read-only) or from the existing local directory, then hand local file paths to the child launcher.

### Changes Required:

#### 1. Provider Abstraction
**File**: `coordinator/profiles.py` (new)  
**Changes**: Define a `ProfileProvider` interface and implementations:
- `LocalProfileProvider` reading the existing directory (preserve current behaviour as default; document that this matches today’s coordinator usage).
- `S3ProfileProvider` that downloads `active/*.json` and the optional `key.txt` from `AUTH_PROFILE_S3_BUCKET`/`AUTH_PROFILE_S3_PREFIX` into a scratch directory (`/tmp/auth_profiles` by default) via `boto3`. Add module-level documentation describing the expected S3 layout, required IAM permissions (`s3:GetObject`, `s3:ListBucket`), and how caching works between restarts.
- Allow configuration through environment variables (`PROFILE_BACKEND`, `AUTH_PROFILE_S3_BUCKET`, `AUTH_PROFILE_S3_PREFIX`, `AUTH_PROFILE_S3_REGION`, `AUTH_PROFILE_CACHE_DIR`) with sensible defaults for local use. Note within docstrings that additional providers can be registered later by extending the same interface.

```python
class ProfileProvider(Protocol):
    def hydrate(self) -> ProfileHydrationResult: ...

class ProfileHydrationResult(BaseModel):
    profiles_dir: Path          # directory containing hydrated *.json files
    key_file: Path | None = None  # optional hydrated key.txt for API keys
```

`hydrate()` must download / copy the remote auth payloads into a local, coordinator-readable directory and return a Pydantic `ProfileHydrationResult` describing:
- `profiles_dir`: absolute path to the directory that now holds `*.json` cookies (used to feed the child launcher).
- `key_file`: optional path to the hydrated API key file; set to `None` when not present.

Providers must ensure the directory exists, is readable by the current process, and contains only the files required for launch. Raise descriptive exceptions for missing AWS credentials, misconfigured prefixes, or empty directories so the entrypoint can fail fast. Include inline docstrings explaining these guarantees so future maintainers understand why we map remote objects to local paths. If feasible, expose a small CLI (e.g., `python -m coordinator.profiles hydrate --backend s3`) to reuse the same logic from both tests and the Docker entrypoint.

#### 2. Coordinator Wiring
**File**: `coordinator/main.py`  
**Changes**:
- Extend CLI/env parsing to accept `--profile-backend` (`local`/`s3`), bucket/prefix/region, and cache directory overrides. Document each flag in `--help`, describing how S3 parameters map to specific environment variables and emphasise required IAM permissions.
- Call `ProfileProvider.hydrate()` before `discover_profiles`, updating `args.profile_dir` to the hydrated local path returned in `ProfileHydrationResult`.
- Surface clear errors when S3 download fails or no profiles are found (e.g., “S3 backend configured but no auth profiles downloaded from s3://bucket/prefix/active/”).
- Propagate `ProfileHydrationResult.key_file` to the API layer via an environment variable (`AUTH_KEY_FILE_PATH`) so `auth_utils` can locate the hydrated key file. Fall back gracefully when the key file is absent so existing zero-auth deployments keep working.

#### 3. API Key Loading
**File**: `api_utils/auth_utils.py`  
**Changes**: Allow overriding `KEY_FILE_PATH` via env var set by the provider (`AUTH_KEY_FILE_PATH`) so the coordinator can point to the hydrated copy instead of the hard-coded repo location. Update module-level comments to clarify precedence (env var > hydrated cache > legacy repo path) and avoid silently creating empty files when the env var is supplied.

### Success Criteria:

#### Automated Verification:
- [ ] Unit tests exercising `LocalProfileProvider` and `S3ProfileProvider` logic: `poetry run pytest tests/coordinator/test_profiles.py`.
- [ ] Coordinator CLI without S3 still passes existing tests: `poetry run pytest tests/coordinator`.

#### Manual Verification:
- [ ] Start coordinator with local backend; confirm regression-free child launch.
- [ ] Point `PROFILE_BACKEND=s3` at a bucket/prefix containing sample profiles; confirm children start with downloaded files.
- [ ] Verify `auth_utils.initialize_keys()` reads the staged `key.txt`.

---

## Phase 2: Production Docker Runtime

### Overview
Create a new production Dockerfile and entrypoint that stage dependencies, perform S3 sync via the provider, and launch the coordinator rather than Supervisor.

### Changes Required:

#### 1. Entry Script
**File**: `docker/entrypoint.prod.sh` (new)  
**Changes**: Bash script to:
- Run `python -m coordinator.sync_profiles` (or reuse provider module via a small CLI) to hydrate S3 data before boot. Emit logging lines describing the chosen backend, bucket/prefix, and destination directory so operators can diagnose misconfigurations from container logs.
- Export `AUTH_KEY_FILE_PATH` for the API layer when the hydrated result includes a key file.
- Derive coordinator arguments from env vars (e.g., `COORDINATOR_HOST`, `COORDINATOR_PORT`, `BASE_API_PORT`, `BASE_STREAM_PORT`) with defaults of `0.0.0.0:2048`, 3100, 3200.
- Exec `python -m coordinator.main ...` as PID 1. If hydration fails, exit non-zero and surface the error.
- Optionally write a generated `.env.runtime` file summarising the resolved settings (documented in README) to support debugging.

#### 2. Production Dockerfile
**File**: `docker/Dockerfile.prod` (new)  
**Changes**:
- Use multi-stage build rooted in `python:3.13-slim-bookworm` (builder and final image); builder stage installs Poetry and caches dependencies, final stage keeps footprint minimal.
- Install system packages required by Playwright/Camoufox (existing list from current Dockerfile) plus AWS CLI tooling for S3 access.
- Copy project files, install Poetry deps (reuse multi-stage builder).
- Copy entrypoint script, mark executable, set `CMD ["./docker/entrypoint.prod.sh"]`.
- Inline documentation (comments) listing the key runtime environment variables: `PROFILE_BACKEND`, `AUTH_PROFILE_S3_BUCKET`, `AUTH_PROFILE_S3_PREFIX`, `AUTH_PROFILE_S3_REGION`, `AUTH_PROFILE_CACHE_DIR`, `COORDINATOR_HOST`, `COORDINATOR_PORT`, `BASE_API_PORT`, `BASE_STREAM_PORT`, optional AWS credential variables. Mention how to switch back to the local backend for development.

#### 3. Dependency Updates
**File**: `pyproject.toml`, `poetry.lock`  
**Changes**: Add `boto3` to application dependencies for S3 access. If integration tests rely on `moto` or similar, add them to `tool.poetry.group.dev.dependencies` with pinned versions and note any Python 3.13 compatibility considerations.

### Success Criteria:

#### Automated Verification:
- [ ] Image builds successfully: `docker build -f docker/Dockerfile.prod .`.
- [ ] Basic container smoke test inside CI: `docker run --rm -e PROFILE_BACKEND=local ...`.
- [ ] Hydration CLI/unit tests run against both local and mocked S3 backends (consider using `pytest` + `moto`).

#### Manual Verification:
- [ ] Run container with S3 credentials; confirm coordinator logs show hydrated profiles and list launched children.
- [ ] Confirm `/live` and `/ready` endpoints respond as expected (see Phase 3) with correct HTTP status codes.
- [ ] Launch with `PROFILE_BACKEND=local` and a bind-mounted `auth_profiles` directory to demonstrate backwards compatibility.

---

## Phase 3: Health Endpoints & Monitoring

### Overview
Expose distinct liveness/readiness endpoints in both coordinator and child FastAPI apps with appropriate HTTP status codes.

### Changes Required:

#### 1. Coordinator Endpoints
**File**: `coordinator/api.py`  
**Changes**:
- Add `@app.get("/live")` returning 200 when the coordinator process is running (simple liveness check, no child introspection).
- Replace `/health` with `/ready` that returns 200 only when at least one child is ready; otherwise 503 with diagnostic payload naming unhealthy children.
- Optionally keep `/health` as a compatibility alias that delegates to `/ready`, but document deprecation in README and logs so clients migrate.

#### 2. Child Endpoint
**File**: `api_utils/app.py` & `api_utils/routers/health.py`  
**Changes**:
- Ensure child `/health` continues to reflect internal readiness for legacy clients (keep response body but adjust status codes as necessary).
- Add `/live` (always 200) and `/ready` (mirroring current logic but setting HTTP 200/503 accordingly). Document in code comments that coordinator polling should switch to `/ready` once rolled out.

#### 3. Tests
**File**: `tests/coordinator/test_api.py` (new) & `tests/api/test_health.py` (new)  
**Changes**: Expand test suite to cover new endpoints and status codes, including scenarios where all children are unhealthy (expect coordinator `/ready` 503) and when at least one recovers (expect 200).

### Success Criteria:

#### Automated Verification:
- [ ] Updated FastAPI tests pass: `poetry run pytest tests/coordinator/test_api.py tests/api/test_health.py` (include cases for `/live`, `/ready`, and legacy `/health` alias).

#### Manual Verification:
- [ ] Hit `/live` and `/ready` on a running container; observe 200 vs 503 depending on child status (log message should also mention unhealthy children).
- [ ] Simulate child failure (kill process) and confirm coordinator `/ready` flips to 503, then returns to 200 once the child restarts.

---

## Phase 4: Documentation & Operational Notes

### Overview
Document production usage, configuration, and operational procedures for the new image.

### Changes Required:

#### 1. Deployment README
**File**: `docs/deployment-prod.md` (new)  
**Changes**: Describe:
- Required environment variables (`PROFILE_BACKEND`, `AUTH_PROFILE_S3_BUCKET`, `AUTH_PROFILE_S3_PREFIX`, `AUTH_PROFILE_S3_REGION`, optional AWS creds, coordinator host/port overrides) and how they map to the entrypoint/CLI flags.
- Expected S3 directory layout for `active/*.json` and `key.txt` (include tree diagram and note that only `active/` is read).
- Hydration process explanation (what `hydrate()` does, where files land on disk, how to inspect `/tmp/auth_profiles`).
- Health endpoint semantics (`/live`, `/ready`, legacy `/health`) and sample `curl` commands.
- Example `docker run` / `docker compose` snippets for both S3-backed and local-testing scenarios, plus troubleshooting tips for common errors (missing bucket, empty profile set, permission denied).

#### 2. Root README Reference
**File**: `README.md`  
**Changes**: Add a short section linking to the new deployment doc and pointing at `docker/Dockerfile.prod`.

### Success Criteria:

#### Automated Verification:
- [ ] `markdownlint`/CI docs checks (if configured) pass locally.

#### Manual Verification:
- [ ] Peer review confirms docs cover all required configuration and workflows.

---

## Testing Strategy

### Unit Tests:
- Profile provider edge cases (missing bucket, empty downloads, local fallback).
- Health endpoint status-code transitions under child failure scenarios.

### Integration Tests:
- End-to-end coordinator boot using a temporary S3 bucket (mocked with `moto`) validating hydration and routing.
- Container smoke tests ensuring entrypoint orchestrates S3 sync and coordinator startup.

### Manual Testing Steps:
1. Populate an S3 bucket prefix with sample cookies (`active/profile-a.json`, `active/profile-b.json`) and optional `key.txt`; run container with `PROFILE_BACKEND=s3` and observe hydration logs (`Hydrated profiles from s3://...`).
2. Call `/live`, `/ready`, and `/v1/models` on port 2048 to verify routing; document sample `curl` commands in README.
3. Temporarily revoke S3 permissions or point to a missing prefix to ensure the container exits with a clear error and does not launch children.
4. Kill a child process inside the container (`pkill -f launch_camoufox.py`) and confirm coordinator `/ready` returns 503 until the registry re-promotes a healthy child.
5. Re-run container with `PROFILE_BACKEND=local` and a bind-mounted `auth_profiles/active` directory to verify backwards compatibility for development.

## Performance Considerations

- S3 hydration runs once on startup; cache directory size scales linearly with profile count. Document recommended disk allocations for the cache directory and note that the cache can be mounted to persistent storage if desired.
- Multiple child browsers increase memory footprint; document minimum CPU/RAM in the deployment README and mention how to tune `--port-step` or profile counts to fit resource limits.
- Boto3 downloads should reuse a single session to avoid redundant TLS handshakes; expose optional environment variables for read/connect timeouts in case networks are slow.

## Migration Notes

- Legacy Docker-based workflows can continue using `docker/Dockerfile`; production environments should switch to `docker/Dockerfile.prod`. Highlight this in README so teams know both options exist.
- Operators must provision S3 objects (`active/*.json`, optional `key.txt`) before deployment; the service will exit if none are found. Document explicit error messages in troubleshooting.
- API key enforcement now depends on the hydrated path; ensure `key.txt` is present even if empty when not in use, and clarify that local development may set `PROFILE_BACKEND=local` to bypass S3 entirely.

## References

- `coordinator/main.py:34-139` – Current local-only profile discovery logic.
- `launch_camoufox.py:809-914` – Requirement for file-backed auth JSON.
- `api_utils/auth_utils.py:1-32` – Hard-coded API key file path assumptions.
- `docker/Dockerfile:64-116` & `supervisord.conf:8-19` – Legacy single-process container entrypoint.
- `coordinator/api.py:40-123` – Existing `/health` implementation needing refactor.
- `docker/Dockerfile` – Current single-process container instructions (document contrast with new prod Dockerfile).
