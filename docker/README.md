# Docker Assets

This directory contains every Docker-related asset for the AI Studio Proxy API project.

## Directory Contents

- `Dockerfile` – legacy image that mirrors the original single-process runtime.
- `Dockerfile.prod` – production-ready coordinator image that hydrates auth profiles and runs the multi-process coordinator directly.
- `docker-compose.yml` – compose stack used during development.
- `.env.docker` – sample environment file for the compose stack.
- `entrypoint.prod.sh` – startup script used by the production image.
- `README-Docker.md` – the original (Chinese) Docker deployment guide.

## Quick Start (Compose Workflow)

```bash
cd docker
cp .env.docker .env
# adjust configuration values as needed
docker compose up -d
docker compose logs -f
```

To refresh the compose stack after pulling repository updates:

```bash
cd docker
bash update.sh
```

## Production Coordinator Image (`docker/Dockerfile.prod`)

The production image hydrates authentication material (either from S3 or a bind-mounted directory) and launches the coordinator itself—no Supervisor involved.

### Required Environment Variables

| Variable | Description | Default |
| --- | --- | --- |
| `PROFILE_BACKEND` | Select the profile source: `s3` or `local`. | `local` |
| `LOCAL_PROFILE_DIR` | Directory inside the container when using the local backend. | `auth_profiles/active` |
| `AUTH_PROFILE_S3_BUCKET` | Bucket name (required when `PROFILE_BACKEND=s3`). | – |
| `AUTH_PROFILE_S3_PREFIX` | Optional prefix such as `prod/coordinator`. | – |
| `AUTH_PROFILE_S3_REGION` | AWS region for the bucket. | – |
| `AUTH_PROFILE_CACHE_DIR` | Directory where hydrated files are staged. | `/tmp/auth_profiles` |
| `COORDINATOR_HOST` / `COORDINATOR_PORT` | Coordinator bind address and port. | `0.0.0.0` / `2048` |
| `BASE_API_PORT` / `BASE_STREAM_PORT` / `BASE_CAMOUFOX_PORT` | Starting ports for child processes. | `3100` / `3200` / `9222` |
| `PORT_STEP` | Increment applied between child port allocations. | `1` |
| `HEADLESS` | `true` keeps browser children headless; set `false` to disable. | `true` |

> When using S3, grant the container role `s3:ListBucket` and `s3:GetObject` over the chosen prefix and store profiles as `prefix/active/*.json` plus `prefix/key.txt`.

### Build the Image

```bash
docker build -f docker/Dockerfile.prod -t aistudioproxy:prod .
```

### Run with Local Profiles

Assuming the host has `auth_profiles/active/*.json` and `auth_profiles/key.txt`:

```bash
docker run --rm \
  -e PROFILE_BACKEND=local \
  -e LOCAL_PROFILE_DIR=/profiles/active \
  -p 2048:2048 \
  -v "$(pwd)/auth_profiles:/profiles:ro" \
  aistudioproxy:prod
```

### Run with S3 Hydration

```bash
docker run --rm \
  -e PROFILE_BACKEND=s3 \
  -e AUTH_PROFILE_S3_BUCKET=my-auth-bucket \
  -e AUTH_PROFILE_S3_PREFIX=prod/coordinator \
  -e AUTH_PROFILE_S3_REGION=us-east-1 \
  -e AWS_ACCESS_KEY_ID=... \
  -e AWS_SECRET_ACCESS_KEY=... \
  -p 2048:2048 \
  aistudioproxy:prod
```

Provide `AWS_SESSION_TOKEN`, proxy variables, or logging overrides as needed.

### Health Checks

The coordinator exposes the following probes:

- `GET /live` – liveness probe (always returns HTTP 200 while the process is running).
- `GET /ready` – readiness probe (HTTP 200 when at least one child is healthy, otherwise 503).
- `GET /health` – legacy alias of `/ready` with an `X-Deprecation-Notice` header.

Use `/ready` for readiness probes and `/live` for liveness probes in orchestrators.

## Further Reading

- Compose workflow details: `docker/README-Docker.md`
- In-depth production deployment guide: `docs/deployment-prod.md`

## Handy Commands

```bash
# Service status (compose)
docker compose ps

# Tail logs
docker compose logs -f

# Stop / restart
docker compose down
docker compose restart

# Shell inside the compose container
docker compose exec ai-studio-proxy /bin/bash
```

## Highlights

- Centralized configuration via `.env`.
- Simple upgrades with `bash update.sh`.
- Containerized runtime isolates dependencies.
- Auth profiles and logs can be persisted via mounted volumes.

## Notes

1. Prepare auth profiles before the first boot (either populate S3 or mount local files).
2. Ensure host ports are free before binding.
3. Keep the `.env` file alongside `docker-compose.yml` so Compose picks up environment values.
4. All Docker assets live in this `docker/` directory to keep the project root tidy.
