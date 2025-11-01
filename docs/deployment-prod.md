# Production Coordinator Deployment

This guide describes how to build and operate the production-ready coordinator container introduced in `docker/Dockerfile.prod`. The image hydrates authentication profiles from S3 (or a bind-mounted directory), exports liveness/readiness endpoints, and runs the multi-process coordinator directly without Supervisor.

## Build the Image

```bash
# From the repository root
docker build -f docker/Dockerfile.prod -t aistudioproxy:prod .
```

Use a tag naming convention that matches your registry workflow (e.g. `ghcr.io/<org>/aistudioproxy:prod`).

## Runtime Configuration

All behaviour is controlled through environment variables. The entrypoint (`docker/entrypoint.prod.sh`) resolves these values, hydrates profiles, and launches `python -m coordinator.main`.

| Variable | Description | Default |
|----------|-------------|---------|
| `PROFILE_BACKEND` | Profile source: `s3` or `local`. | `local` |
| `LOCAL_PROFILE_DIR` | Directory containing `*.json` profiles when `PROFILE_BACKEND=local`. Mount your host path here. | `auth_profiles/active` |
| `AUTH_PROFILE_S3_BUCKET` | S3 bucket with auth data when using the S3 backend. | – |
| `AUTH_PROFILE_S3_PREFIX` | Optional prefix under the bucket (e.g. `prod/coordinator`). | – |
| `AUTH_PROFILE_S3_REGION` | AWS region for the bucket. Falls back to boto3 defaults. | – |
| `AUTH_PROFILE_CACHE_DIR` | Scratch directory for hydrated profiles. | `/tmp/auth_profiles` |
| `COORDINATOR_HOST` / `COORDINATOR_PORT` | Coordinator binding host/port. | `0.0.0.0` / `2048` |
| `BASE_API_PORT` / `BASE_STREAM_PORT` / `BASE_CAMOUFOX_PORT` | Starting ports for child processes. | `3100` / `3200` / `9222` |
| `PORT_STEP` | Increment applied between child port allocations. | `1` |
| `HEADLESS` | `true` to run browsers headless, `false` to disable headless mode. | `true` |
| `COORDINATOR_LOG_DIR` | Optional override for coordinator-managed child logs. | `logs/coordinator` |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN` | Supply AWS credentials when the container cannot assume a role automatically. | – |

### Expected S3 Layout

```
s3://<AUTH_PROFILE_S3_BUCKET>/<AUTH_PROFILE_S3_PREFIX>/
├── active/
│   ├── profile-a.json
│   ├── profile-b.json
│   └── …
└── key.txt
```

- Only objects in `active/` are launched as children.
- `key.txt` is optional; when present its newline-delimited keys feed API authentication.
- The task role needs `s3:ListBucket` (for the selected prefix) and `s3:GetObject`.

## Hydration Workflow

1. `entrypoint.prod.sh` runs `python -m coordinator.profiles` with the selected backend.
2. Profiles and `key.txt` are staged under `${AUTH_PROFILE_CACHE_DIR}` (default `/tmp/auth_profiles`). Hydrated JSON files are always written to `${AUTH_PROFILE_CACHE_DIR}/active`.
3. When a key file is downloaded, the entrypoint exports `AUTH_KEY_FILE_PATH`, allowing the API layer to load keys without touching the repository tree.
4. After hydration the script forces `PROFILE_BACKEND=local` so the coordinator boots against the staged directory without re-downloading from S3.

Inspect staged files directly inside the container:

```bash
docker exec -it aistudioproxy ls -R /tmp/auth_profiles
```

Mount a persistent volume to `AUTH_PROFILE_CACHE_DIR` if you want to reuse downloads between container restarts.

## Health Endpoints

The coordinator and each child FastAPI app expose:

| Endpoint | Description | Success Criteria |
|----------|-------------|------------------|
| `/live` | Process-level liveness probe. | Always returns `200`. |
| `/ready` | Readiness probe. | Returns `200` when at least one child is healthy; otherwise `503`. |
| `/health` | Backwards-compatible alias of `/ready`. | Mirrors `/ready` response and adds `X-Deprecation-Notice`. |

Sample checks:

```bash
curl -sf http://localhost:2048/live
curl -sf http://localhost:2048/ready || echo "Coordinator not ready"
```

### Profile Rotation Pool

- The coordinator maintains a fixed pool of two Camoufox child processes. Any additional hydrated profiles are staged in a rotation queue.
- When a child exits, times out during readiness (60 s ceiling), or returns a retryable ≥500 error, the slot manager terminates the process and activates the next queued profile on the same port triple.
- Evicted profiles are appended to the back of the queue so every profile eventually receives runtime and no additional ports are consumed.

## Running the Container

### S3-Backed Deployment

```bash
docker run --rm \
  --name aistudioproxy \
  -p 2048:2048 \
  -e PROFILE_BACKEND=s3 \
  -e AUTH_PROFILE_S3_BUCKET=my-auth-bucket \
  -e AUTH_PROFILE_S3_PREFIX=prod/coordinator \
  -e AUTH_PROFILE_S3_REGION=us-east-1 \
  -e AWS_ACCESS_KEY_ID=... \
  -e AWS_SECRET_ACCESS_KEY=... \
  aistudioproxy:prod
```

Attach any additional environment variables for Camoufox/Playwright (proxy, logging, etc.) as needed.

> **Optional**: include `-e AWS_SESSION_TOKEN=...` when using temporary AWS credentials.

### Local Testing with Bind-Mounted Profiles

```bash
docker run --rm \
  --name aistudioproxy-dev \
  -p 2048:2048 \
  -e PROFILE_BACKEND=local \
  -e LOCAL_PROFILE_DIR=/profiles/active \
  -v /path/to/auth_profiles:/profiles:ro \
  aistudioproxy:prod
```

Ensure the host directory contains both `active/*.json` and (optionally) `key.txt` at its root.

### Docker Compose Snippet

```yaml
services:
  coordinator:
    image: ghcr.io/your-org/aistudioproxy:prod
    ports:
      - "2048:2048"
    environment:
      PROFILE_BACKEND: s3
      AUTH_PROFILE_S3_BUCKET: my-auth-bucket
      AUTH_PROFILE_S3_PREFIX: prod/coordinator
      AUTH_PROFILE_S3_REGION: us-east-1
      COORDINATOR_PORT: 2048
      BASE_API_PORT: 3100
      BASE_STREAM_PORT: 3200
    secrets:
      - aws_credentials
    volumes:
      - cache:/tmp/auth_profiles

volumes:
  cache:
```

Provide AWS credentials via secrets, IAM roles, or environment variables depending on your platform.

## Troubleshooting

- **Hydration fails immediately**: Verify `PROFILE_BACKEND` matches your configuration and that the required S3 variables are set when using the remote backend.
- **No profiles downloaded**: Ensure the bucket prefix contains `active/*.json` files. An empty directory causes startup to abort with `No auth profiles found`.
- **API key validation failing**: Confirm `key.txt` exists in S3 or the bind-mounted directory. The coordinator will expose `AUTH_KEY_FILE_PATH` in logs when the file is detected.
- **Credentials errors**: Check IAM permissions (`s3:ListBucket`, `s3:GetObject`) and the AWS credentials supplied to the container.
- **Port conflicts**: Adjust `COORDINATOR_PORT`, `BASE_API_PORT`, `BASE_STREAM_PORT`, or `PORT_STEP` so child processes do not collide with existing services.
- **Persistent cache required**: Bind-mount `AUTH_PROFILE_CACHE_DIR` to a volume if you want hydrated profiles to survive restarts (e.g. `-v cache:/tmp/auth_profiles`).

For additional operational guidance or troubleshooting tips, open an issue with the relevant logs from the entrypoint and coordinator startup.
